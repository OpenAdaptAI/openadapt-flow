"""Unit + conformance tests for the FreeRDPBackend (RDP over a transport).

No live RDP server: a scripted :class:`FakeRDPTransport` stands in for a real
RDP client — it serves a framebuffer image and records every pointer / key /
wheel event the backend sends. This is the RDP analog of ``test_windows_backend``'s
mock WAA server; it proves the adapter shape (framebuffer -> PNG, clicks/keys
-> transport events) without any RDP dependency, exactly the way CI must run.

The conformance test drives the UNMODIFIED Recorder -> compiler -> Replayer
stack over the FreeRDPBackend against a stateful fake desktop, proving the
4-method Backend protocol needs zero compiler/replayer changes for RDP.
"""

from __future__ import annotations

import io
import json

import cv2
import numpy as np
import pytest
from PIL import Image

from openadapt_flow.backend import Backend, IdentityBackend, StructuralBackend
from openadapt_flow.backends.rdp_backend import (
    FreeRDPBackend,
    RDPTransport,
    normalize_chord,
)

VIEWPORT = (1280, 800)

# Synthetic desktop app (drawn with cv2, mirroring test_windows_backend).
BUTTON = (560, 400, 160, 48)  # x, y, w, h
BUTTON_CENTER = (BUTTON[0] + BUTTON[2] // 2, BUTTON[1] + BUTTON[3] // 2)
BANNER_LOADED = "Chart Loaded Ok"
BANNER_SAVED = "Encounter Saved Successfully"
NOTE_VALUE = "confidential follow up note"


def blank() -> np.ndarray:
    return np.full((VIEWPORT[1], VIEWPORT[0], 3), 245, dtype=np.uint8)


def draw_button(img: np.ndarray, x: int, y: int, w: int, h: int, label: str) -> None:
    cv2.rectangle(img, (x, y), (x + w, y + h), (205, 205, 205), -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (70, 70, 70), 2)
    cv2.putText(
        img, label, (x + 12, y + h // 2 + 8),
        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2, cv2.LINE_AA,
    )


def draw_text(img: np.ndarray, x: int, y: int, text: str) -> None:
    cv2.putText(
        img, text, (x, y),
        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2, cv2.LINE_AA,
    )


def app_screens() -> list[Image.Image]:
    """The 4 states of the synthetic desktop app, as RGB PIL images."""
    s0 = blank()
    draw_text(s0, 520, 84, "MockMed Desktop")
    draw_button(s0, *BUTTON, "Open Chart")

    s1 = s0.copy()
    draw_text(s1, 420, 244, BANNER_LOADED)

    s2 = s1.copy()
    draw_text(s2, 560, 470, NOTE_VALUE)

    s3 = s2.copy()
    draw_text(s3, 420, 320, BANNER_SAVED)
    # cv2 arrays are BGR; convert to RGB PIL images (transport-native form).
    return [Image.fromarray(cv2.cvtColor(s, cv2.COLOR_BGR2RGB)) for s in
            [s0, s1, s2, s3]]


class FakeRDPTransport:
    """Scripted, event-recording stand-in for a real RDP transport.

    Serves ``screens[state]`` as the framebuffer and records every input the
    backend sends. When ``stateful`` is True it advances ``state`` like the
    real MockMed app would (click inside the button 0->1, first typed char
    1->2, Enter 2->3), so the unmodified record/replay pipeline can drive it.

    Args:
        screens: State ladder of PIL framebuffer images.
        stateful: Advance state on input (for the conformance test).
        as_raw_bytes: Return raw RGB bytes from ``framebuffer`` instead of a
            PIL image (exercises the backend's raw-bytes decode path).
    """

    def __init__(
        self,
        screens: list[Image.Image],
        *,
        stateful: bool = False,
        as_raw_bytes: bool = False,
    ) -> None:
        self.screens = screens
        self.stateful = stateful
        self.as_raw_bytes = as_raw_bytes
        self.state = 0
        self.connected = False
        self.disconnects = 0
        self.pointer_events: list[tuple[int, int, str, bool]] = []
        self.key_events: list[tuple[str, bool]] = []
        self.wheel_events: list[tuple[int, int]] = []

    # -- RDPTransport --------------------------------------------------------

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1

    def framebuffer(self):
        img = self.screens[self.state]
        if self.as_raw_bytes:
            return img.tobytes(), img.width, img.height
        return img, img.width, img.height

    def pointer(self, x: int, y: int, button: str, down: bool) -> None:
        self.pointer_events.append((x, y, button, down))
        if self.stateful and not down and self.state == 0:
            bx, by, bw, bh = BUTTON
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self.state = 1

    def key(self, keysym_or_char: str, down: bool) -> None:
        self.key_events.append((keysym_or_char, down))
        if not self.stateful or not down:
            return
        if self.state == 1 and keysym_or_char not in ("enter", "tab"):
            self.state = 2
        elif self.state == 2 and keysym_or_char == "enter":
            self.state = 3

    def wheel(self, dx: int, dy: int) -> None:
        self.wheel_events.append((dx, dy))

    def reset(self) -> None:
        self.state = 0
        self.pointer_events.clear()
        self.key_events.clear()
        self.wheel_events.clear()


@pytest.fixture()
def transport() -> FakeRDPTransport:
    return FakeRDPTransport(app_screens())


@pytest.fixture()
def backend(transport: FakeRDPTransport) -> FreeRDPBackend:
    return FreeRDPBackend(transport)


# -- protocol conformance ------------------------------------------------------


def test_implements_backend_protocol(backend: FreeRDPBackend) -> None:
    assert isinstance(backend, Backend)


def test_connect_on_construction(transport: FakeRDPTransport) -> None:
    assert transport.connected is False
    FreeRDPBackend(transport)
    assert transport.connected is True


def test_no_connect_when_disabled(transport: FakeRDPTransport) -> None:
    FreeRDPBackend(transport, connect=False)
    assert transport.connected is False


def test_pixel_only_has_no_structured_or_structural_capabilities(
    backend: FreeRDPBackend,
) -> None:
    # RDP is a pure-pixel substrate: the backend must not claim a structured
    # (a11y/DOM) identity layer or cheap URL/title/page-count observations it
    # cannot honor. Identity honestly falls back to the OCR tier.
    assert not isinstance(backend, IdentityBackend)
    assert not isinstance(backend, StructuralBackend)
    for attr in ("structured_text_at", "url", "page_title", "page_count"):
        assert not hasattr(backend, attr)


# -- screenshot / viewport -----------------------------------------------------


def test_screenshot_returns_valid_png(backend: FreeRDPBackend) -> None:
    png = backend.screenshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    decoded = Image.open(io.BytesIO(png))
    assert decoded.size == VIEWPORT


def test_screenshot_matches_fake_framebuffer(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    png = backend.screenshot()
    decoded = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    expected = np.asarray(transport.screens[0].convert("RGB"))
    assert np.array_equal(decoded, expected)


def test_screenshot_from_raw_bytes_framebuffer() -> None:
    # A transport that hands back raw RGB bytes (not a PIL image) must still
    # PNG-encode to the right pixels and dimensions.
    t = FakeRDPTransport(app_screens(), as_raw_bytes=True)
    b = FreeRDPBackend(t)
    png = b.screenshot()
    decoded = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    expected = np.asarray(t.screens[0].convert("RGB"))
    assert decoded.shape == (VIEWPORT[1], VIEWPORT[0], 3)
    assert np.array_equal(decoded, expected)


def test_viewport_matches_framebuffer(backend: FreeRDPBackend) -> None:
    assert backend.viewport == VIEWPORT


def test_viewport_override(transport: FakeRDPTransport) -> None:
    b = FreeRDPBackend(transport, viewport=(640, 480))
    assert b.viewport == (640, 480)


# -- click ---------------------------------------------------------------------


def test_click_sends_pointer_down_then_up(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.click(10, 20)
    assert transport.pointer_events == [
        (10, 20, "left", True),
        (10, 20, "left", False),
    ]


def test_double_click_sends_sequence_twice(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.click(30, 40, double=True)
    assert transport.pointer_events == [
        (30, 40, "left", True),
        (30, 40, "left", False),
        (30, 40, "left", True),
        (30, 40, "left", False),
    ]


def test_click_coordinates_pass_through(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.click(*BUTTON_CENTER)
    xs = {(x, y) for (x, y, _b, _d) in transport.pointer_events}
    assert xs == {BUTTON_CENTER}


# -- type_text -----------------------------------------------------------------


def test_type_text_per_char_key_events(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.type_text("Ab1")
    assert transport.key_events == [
        ("A", True), ("A", False),
        ("b", True), ("b", False),
        ("1", True), ("1", False),
    ]


def test_type_text_empty_sends_nothing(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.type_text("")
    assert transport.key_events == []


def test_type_text_preserves_case_and_symbols(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.type_text("O'B")
    assert transport.key_events == [
        ("O", True), ("O", False),
        ("'", True), ("'", False),
        ("B", True), ("B", False),
    ]


# -- press ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chord", "expected"),
    [
        ("Enter", ["enter"]),
        ("Escape", ["escape"]),
        ("ArrowDown", ["down"]),
        ("ControlOrMeta+a", ["ctrl", "a"]),
        ("Meta+d", ["meta", "d"]),
        ("Ctrl+Shift+Escape", ["ctrl", "shift", "escape"]),
    ],
)
def test_normalize_chord(chord: str, expected: list[str]) -> None:
    assert normalize_chord(chord) == expected


def test_press_single_key(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.press("enter")
    assert transport.key_events == [("enter", True), ("enter", False)]


def test_press_named_key_maps(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.press("ArrowDown")
    assert transport.key_events == [("down", True), ("down", False)]


def test_press_chord_nests_down_then_reverse_up(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.press("ControlOrMeta+a")
    # Down in order, up in reverse: Ctrl down, a down, a up, Ctrl up.
    assert transport.key_events == [
        ("ctrl", True), ("a", True), ("a", False), ("ctrl", False),
    ]


def test_press_empty_raises(backend: FreeRDPBackend) -> None:
    with pytest.raises(ValueError):
        backend.press("")


# -- scroll --------------------------------------------------------------------


def test_scroll_sends_wheel(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.scroll(0, 400)
    assert transport.wheel_events == [(0, 400)]


def test_scroll_horizontal(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.scroll(120, 0)
    assert transport.wheel_events == [(120, 0)]


def test_scroll_zero_sends_nothing(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.scroll(0, 0)
    assert transport.wheel_events == []


# -- lifecycle -----------------------------------------------------------------


def test_close_disconnects_transport(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.close()
    assert transport.connected is False
    assert transport.disconnects == 1


def test_fake_transport_satisfies_rdp_transport_protocol(
    transport: FakeRDPTransport,
) -> None:
    assert isinstance(transport, RDPTransport)


# -- record -> compile -> replay conformance (no compiler/replayer changes) -----


@pytest.mark.timeout(300)
def test_record_compile_replay_over_rdp_backend(tmp_path) -> None:
    """The unmodified Recorder, compiler and Replayer drive the FreeRDPBackend
    end to end against the stateful fake RDP desktop."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import ActionKind
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime.replayer import Replayer

    transport = FakeRDPTransport(app_screens(), stateful=True)
    backend = FreeRDPBackend(transport)

    recording_dir = tmp_path / "recording"
    bundle_dir = tmp_path / "bundle"
    run_dir = tmp_path / "run"

    recorder = Recorder(
        backend,
        recording_dir,
        settle_interval_s=0.02,
        settle_timeout_s=2.0,
    )
    recorder.click(*BUTTON_CENTER)
    recorder.type_text(NOTE_VALUE, param="note")
    recorder.press("Enter")
    recorder.finish()
    assert transport.state == 3  # the fake app reached its final state

    meta = json.loads((recording_dir / "meta.json").read_text())
    assert meta["viewport"] == list(VIEWPORT)
    assert meta["params"] == {"note": NOTE_VALUE}

    workflow = compile_recording(recording_dir, bundle_dir, name="rdp-smoke")
    assert [s.action for s in workflow.steps] == [
        ActionKind.CLICK,
        ActionKind.TYPE,
        ActionKind.KEY,
    ]

    transport.reset()
    report = Replayer(backend, poll_interval_s=0.02).run(
        workflow,
        params={"note": NOTE_VALUE},
        bundle_dir=bundle_dir,
        run_dir=run_dir,
    )
    assert report.success, [r.model_dump() for r in report.results]
    assert transport.state == 3


# -- live smoke test (gated; skipped in CI) ------------------------------------
#
# Runs ONLY when a real RDP target is configured via env vars. It uses the
# real AardwolfTransport (which needs the optional `rdp` extra:
# `pip install 'openadapt-flow[rdp]'`), grabs ONE framebuffer, asserts it is a
# non-trivial image, and disconnects. Never runs in CI.
#
#   export OPENADAPT_FLOW_RDP_TARGET=host_or_ip[:port]
#   export OPENADAPT_FLOW_RDP_USER=username
#   export OPENADAPT_FLOW_RDP_PASS=password
#   # optional: OPENADAPT_FLOW_RDP_DOMAIN, OPENADAPT_FLOW_RDP_WIDTH/_HEIGHT
#   pytest tests/test_rdp_backend.py -k live_smoke -s

import os


@pytest.mark.skipif(
    not (
        os.environ.get("OPENADAPT_FLOW_RDP_TARGET")
        and os.environ.get("OPENADAPT_FLOW_RDP_USER")
        and os.environ.get("OPENADAPT_FLOW_RDP_PASS")
    ),
    reason="live RDP target not configured (set OPENADAPT_FLOW_RDP_TARGET/_USER/_PASS)",
)
def test_live_smoke_real_rdp_framebuffer() -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")
    from openadapt_flow.backends.rdp_backend import AardwolfTransport

    target = os.environ["OPENADAPT_FLOW_RDP_TARGET"]
    host, _, port = target.partition(":")
    width = int(os.environ.get("OPENADAPT_FLOW_RDP_WIDTH", "1280"))
    height = int(os.environ.get("OPENADAPT_FLOW_RDP_HEIGHT", "800"))

    transport = AardwolfTransport.from_credentials(
        host,
        os.environ["OPENADAPT_FLOW_RDP_USER"],
        os.environ["OPENADAPT_FLOW_RDP_PASS"],
        domain=os.environ.get("OPENADAPT_FLOW_RDP_DOMAIN") or None,
        port=int(port) if port else 3389,
        width=width,
        height=height,
    )
    backend = FreeRDPBackend(transport)
    try:
        png = backend.screenshot()
        w, h = backend.viewport
        print(f"\n[live-smoke] connected; framebuffer {w}x{h}, {len(png)} PNG bytes")
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        img = Image.open(io.BytesIO(png))
        assert img.size == (w, h)
        # Non-trivial: more than one distinct colour (not a blank canvas).
        assert len(img.convert("RGB").getcolors(maxcolors=1 << 24) or []) > 1
    finally:
        backend.close()

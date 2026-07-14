"""Offline unit tests for the remote-display (Citrix-analog) pixel backend.

These run with NO live window and NO macOS permissions: a fake
:class:`WindowClient` records every call so the load-bearing logic is asserted
directly — the captured-pixel<->screen-point coordinate mapping (the DPI/scale
gap a real Citrix Workspace window also imposes), the fail-LOUD contract when
input cannot be delivered (a dropped click must never look like success), the
frontmost/occlusion requirement, keycode-based typing (a remote display forwards
scancodes, not synthetic Unicode), and — critically — that the backend exposes
ONLY the pixel-only :class:`Backend` protocol, never the structural/identity
capabilities (so the resolver's UIA rung is genuinely unavailable, the Citrix
constraint).
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from openadapt_flow.backend import (
    Backend,
    IdentityBackend,
    StructuralActionBackend,
    StructuralBackend,
)
from openadapt_flow.backends.remote_display import (
    RemoteDisplayBackend,
    RemoteDisplayError,
    WindowInfo,
    _split_chord,
)


class FakeClient:
    """A scripted :class:`WindowClient` that records calls (no macOS bindings)."""

    def __init__(
        self,
        *,
        trusted: bool = True,
        frontmost: bool = True,
        window: WindowInfo | None = None,
        px: tuple[int, int] = (3024, 1888),
    ) -> None:
        self.trusted = trusted
        self._frontmost = frontmost
        self.px = px
        self.window = (
            window
            if window is not None
            else WindowInfo(
                window_id=1,
                owner="Parallels Desktop",
                title="Windows 11",
                pid=99,
                bounds=(0.0, 38.0, 1512.0, 944.0),
                on_screen=True,
            )
        )
        self.calls: list[tuple] = []

    def input_trusted(self) -> bool:
        return self.trusted

    def frontmost_pid(self):
        return self.window.pid if self._frontmost else 7

    def find_window(self, owner, title):
        return self.window

    def capture(self, window_id):
        img = Image.new("RGB", self.px, (11, 22, 33))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), self.px[0], self.px[1]

    def activate(self, pid):
        self.calls.append(("activate", pid))

    def mouse(self, x, y, *, button, down, click_count):
        self.calls.append(
            ("mouse", round(x, 1), round(y, 1), button, down, click_count)
        )

    def mouse_move(self, x, y):
        self.calls.append(("move", round(x, 1), round(y, 1)))

    def type_chars(self, text):
        self.calls.append(("type", text))

    def key(self, keycode, *, down, flags):
        self.calls.append(("key", keycode, down, tuple(flags)))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))


def _backend(**kw) -> tuple[RemoteDisplayBackend, FakeClient]:
    client = FakeClient(**kw)
    return RemoteDisplayBackend(client=client, settle_s=0.0), client


def test_exposes_only_pixel_backend_protocol() -> None:
    """The Citrix property: base Backend yes, structural/identity NO — so the
    resolver's UIA rung is unavailable and identity falls back to OCR."""
    backend, _ = _backend()
    assert isinstance(backend, Backend)
    assert not isinstance(backend, StructuralActionBackend)
    assert not isinstance(backend, IdentityBackend)
    assert not isinstance(backend, StructuralBackend)
    assert not hasattr(backend, "structural_locator_at")
    assert not hasattr(backend, "structured_text_at")
    assert not hasattr(backend, "locate_structural")


def test_viewport_and_scale_from_capture() -> None:
    backend, _ = _backend(px=(3024, 1888))
    assert backend.viewport == (3024, 1888)
    backend.screenshot()
    assert backend._scale == pytest.approx(2.0)  # 3024 px / 1512 pt window


def test_screenshot_returns_png() -> None:
    backend, _ = _backend()
    png = backend.screenshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_click_maps_captured_pixels_to_screen_points() -> None:
    """captured (px) -> origin + px/scale. Window at (0,38), scale 2.0."""
    backend, client = _backend()
    backend.click(1000, 500)
    downs = [c for c in client.calls if c[0] == "mouse" and c[4] is True]
    assert downs and downs[0][1] == pytest.approx(500.0)  # 0 + 1000/2
    assert downs[0][2] == pytest.approx(288.0)  # 38 + 500/2


def test_double_click_click_state() -> None:
    backend, client = _backend()
    backend.click(200, 200, double=True)
    click_states = [c[5] for c in client.calls if c[0] == "mouse" and c[4] is True]
    assert click_states == [1, 2]


def test_type_text_routes_to_keycodes() -> None:
    backend, client = _backend()
    backend.type_text("Neil-1")
    assert ("type", "Neil-1") in client.calls


def test_press_named_key_enter() -> None:
    backend, client = _backend()
    backend.press("Enter")
    keys = [c for c in client.calls if c[0] == "key"]
    assert keys[0][1] == 0x24  # Return keycode
    assert keys[0][3] == ()  # no modifiers


def test_press_chord_ctrl_a_uses_control_flag() -> None:
    backend, client = _backend()
    backend.press("ControlOrMeta+a")
    keys = [c for c in client.calls if c[0] == "key"]
    assert keys, "no key events emitted"
    # 'a' keycode 0, control flag applied on both down and up (fail-safe release)
    assert all(k[1] == 0x00 and "control" in k[3] for k in keys)
    assert keys[0][2] is True and keys[-1][2] is False


def test_press_bare_char_types_it() -> None:
    backend, client = _backend()
    backend.press("x")
    assert ("type", "x") in client.calls


def test_scroll_noop_when_zero() -> None:
    backend, client = _backend()
    backend.scroll(0, 0)
    assert not any(c[0] == "scroll" for c in client.calls)


def test_scroll_dispatches() -> None:
    backend, client = _backend()
    backend.scroll(0, 120)
    assert any(c[0] == "scroll" for c in client.calls)


def test_fail_loud_when_not_accessibility_trusted() -> None:
    """A dropped synthetic click must never look like success -> refuse to act."""
    backend, _ = _backend(trusted=False)
    with pytest.raises(RemoteDisplayError, match="Accessibility"):
        backend.click(10, 10)
    with pytest.raises(RemoteDisplayError, match="Accessibility"):
        backend.type_text("x")
    with pytest.raises(RemoteDisplayError, match="Accessibility"):
        backend.press("Enter")


def test_require_input_trust_can_be_disabled_for_capture_only() -> None:
    """Capture-only use (no input) does not require Accessibility."""
    client = FakeClient(trusted=False)
    backend = RemoteDisplayBackend(
        client=client, require_input_trust=False, settle_s=0.0
    )
    assert backend.screenshot()[:8] == b"\x89PNG\r\n\x1a\n"


def test_window_not_found_raises() -> None:
    class NoWindow(FakeClient):
        def find_window(self, owner, title):
            return None

    backend = RemoteDisplayBackend(client=NoWindow(), settle_s=0.0)
    with pytest.raises(RemoteDisplayError, match="no on-screen window"):
        backend.screenshot()


def test_ensure_foreground_requires_frontmost_not_just_onscreen() -> None:
    """An occluded (not-frontmost) window fails ensure_foreground: capture works
    through occlusion but a coordinate click would hit the occluder."""
    backend, _ = _backend(frontmost=False)
    with pytest.raises(RemoteDisplayError, match="foreground"):
        backend.ensure_foreground(retries=2, settle_s=0.0)


def test_ensure_foreground_succeeds_when_frontmost() -> None:
    backend, client = _backend(frontmost=True)
    backend.ensure_foreground(retries=2, settle_s=0.0)
    assert any(c[0] == "activate" for c in client.calls)


def test_split_chord() -> None:
    assert _split_chord("ControlOrMeta+a") == (["control"], "a")
    assert _split_chord("Shift+Tab") == (["shift"], "Tab")
    assert _split_chord("Enter") == ([], "Enter")
    with pytest.raises(ValueError):
        _split_chord("")

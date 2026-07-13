"""Tests for the openadapt-capture -> openadapt-flow recording adapter.

These build a *real* openadapt-capture session on disk — a SQLAlchemy
``recording.db`` written through capture's own db layer plus an
``oa_recording-*.mp4`` written through capture's public ``VideoWriter`` — and
then run the adapter over capture's public API
(``CaptureSession.load(dir).actions()``). This exercises capture's real
event-processing pipeline (raw mouse/keyboard streams -> merged
clicks/drags/typed text) and real frame extraction, so the test cannot silently
pass against a schema that no longer exists. The converted recording is fed to
the UNMODIFIED compiler — the format bridge, proven end to end.

openadapt-capture screenshots the display at import time, so the whole module
is skipped when the package cannot be imported (headless CI / no display); it
runs for real on a developer desktop with the ``capture`` extra installed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

# openadapt-capture is an optional extra AND screenshots the display when
# imported (a module-level side effect in its recorder). Skip the whole module
# if it is absent or cannot be imported in this environment.
if importlib.util.find_spec("openadapt_capture") is None:
    pytest.skip(
        "openadapt-capture not installed (capture extra)", allow_module_level=True
    )
try:  # importing the package screenshots the display; skip if that fails
    import openadapt_capture  # noqa: F401
    from openadapt_capture.db import create_db
    from openadapt_capture.db.models import ActionEvent, Recording
    from openadapt_capture.video import VideoWriter
except Exception as exc:  # pragma: no cover - environment dependent
    pytest.skip(
        f"openadapt-capture cannot be imported here ({exc})",
        allow_module_level=True,
    )

from PIL import Image, ImageDraw

from openadapt_flow.adapters.capture import convert_capture

# Physical (video) pixels; logical screen is half that (pixel_ratio 2.0, the
# macOS Retina case where capture coords and frame pixels disagree).
FRAME_SIZE = (1280, 800)
PIXEL_RATIO = 2.0
FPS = 24
T0 = 100000.0  # wall-clock epoch (recording.timestamp)
VIDEO_T0 = T0 + 1.0  # first frame / video_start_time

BUTTON = (560, 400, 160, 48)  # physical px
BUTTON_CENTER_PHYSICAL = (BUTTON[0] + BUTTON[2] // 2, BUTTON[1] + BUTTON[3] // 2)
BUTTON_CENTER_LOGICAL = (
    BUTTON_CENTER_PHYSICAL[0] / PIXEL_RATIO,
    BUTTON_CENTER_PHYSICAL[1] / PIXEL_RATIO,
)
BANNER_LOADED = "Chart Loaded Ok"
BANNER_SAVED = "Encounter Saved Successfully"
NOTE_VALUE = "confidential follow up note"


# -- fixture drawing (PIL, so frames go straight into capture's VideoWriter) ---


def blank() -> Image.Image:
    return Image.new("RGB", FRAME_SIZE, (245, 245, 245))


def draw_text(img: Image.Image, x: int, y: int, text: str) -> None:
    ImageDraw.Draw(img).text((x, y), text, fill=(0, 0, 0))


def draw_button(img: Image.Image, x: int, y: int, w: int, h: int, label: str) -> None:
    draw = ImageDraw.Draw(img)
    draw.rectangle([x, y, x + w, y + h], fill=(205, 205, 205), outline=(70, 70, 70))
    draw.text((x + 12, y + h // 2 - 4), label, fill=(0, 0, 0))


def app_screens() -> list[Image.Image]:
    s0 = blank()
    draw_text(s0, 520, 70, "MockMed Desktop")
    draw_button(s0, *BUTTON, "Open Chart")
    s1 = s0.copy()
    draw_text(s1, 420, 230, BANNER_LOADED)
    s2 = s1.copy()
    draw_text(s2, 560, 470, NOTE_VALUE)
    s3 = s2.copy()
    draw_text(s3, 420, 320, BANNER_SAVED)
    return [s0, s1, s2, s3]


def write_video(path: Path, states: list[Image.Image]) -> None:
    """Write a *dense* action-gated-style video via capture's public VideoWriter.

    State ``k`` is shown for the wall-clock window ``[VIDEO_T0 + k - 0.5,
    VIDEO_T0 + k + 0.5)`` (transitions on the half-second), so a frame sampled
    at whole-second offset ``k`` — where this fixture's actions land — resolves
    unambiguously to state ``k`` through capture's real timestamp-based frame
    extraction (a sparse video would collide on block boundaries).
    """
    import math

    writer = VideoWriter(
        str(path),
        width=FRAME_SIZE[0],
        height=FRAME_SIZE[1],
        fps=FPS,
        crf=23,
        preset="ultrafast",
    )
    last = len(states) - 1
    offset = 0.0
    end = last + 0.6
    while offset <= end:
        k = min(int(math.floor(offset + 0.5)), last)
        writer.write_frame(states[k], VIDEO_T0 + offset)
        offset += 1.0 / FPS
    writer.close()


def write_recording_db(path: Path, action_rows: list[dict]) -> None:
    """Write a real capture recording.db via capture's SQLAlchemy models."""
    engine, Session = create_db(str(path))
    session = Session()
    try:
        recording = Recording(
            timestamp=T0,
            monitor_width=FRAME_SIZE[0],
            monitor_height=FRAME_SIZE[1],
            platform="darwin",
            task_description="add a note",
            video_start_time=VIDEO_T0,
            double_click_interval_seconds=0.5,
            double_click_distance_pixels=5.0,
            # capture persists pixel_ratio only in the config JSON.
            config={"pixel_ratio": PIXEL_RATIO},
        )
        session.add(recording)
        session.flush()
        for row in action_rows:
            session.add(ActionEvent(recording_id=recording.id, **row))
        session.commit()
    finally:
        session.close()
        engine.dispose()


def make_capture(tmp_path: Path, action_rows: list[dict], screens=None) -> Path:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    write_recording_db(capture_dir / "recording.db", action_rows)
    screens = screens if screens is not None else app_screens()
    write_video(capture_dir / f"oa_recording-{T0}.mp4", screens)
    return capture_dir


# -- raw action_event rows (capture's schema; processing merges them) ---------


def _click_rows(ts: float, x: float, y: float, button: str = "left") -> list[dict]:
    """A press+release pair -> capture merges into one mouse.singleclick."""
    return [
        {
            "name": "click",
            "timestamp": ts,
            "mouse_x": x,
            "mouse_y": y,
            "mouse_button_name": button,
            "mouse_pressed": True,
        },
        {
            "name": "click",
            "timestamp": ts + 0.01,
            "mouse_x": x,
            "mouse_y": y,
            "mouse_button_name": button,
            "mouse_pressed": False,
        },
    ]


def _type_rows(start_ts: float, text: str) -> list[dict]:
    """One press+release per character -> a run of key.type actions."""
    rows: list[dict] = []
    ts = start_ts
    for ch in text:
        rows.append({"name": "press", "timestamp": ts, "key_char": ch})
        rows.append({"name": "release", "timestamp": ts + 0.005, "key_char": ch})
        ts += 0.02
    return rows


def _named_key_rows(ts: float, key_name: str) -> list[dict]:
    """A named special key (no char) -> a key.type with empty text."""
    return [
        {"name": "press", "timestamp": ts, "key_name": key_name},
        {"name": "release", "timestamp": ts + 0.01, "key_name": key_name},
    ]


def demo_rows() -> list[dict]:
    """click -> type NOTE_VALUE -> Enter, at t = 1s, 2s, 3s (relative to T0)."""
    x, y = BUTTON_CENTER_LOGICAL
    rows: list[dict] = []
    rows += _click_rows(T0 + 1.0, x, y)
    rows += _type_rows(T0 + 2.0, NOTE_VALUE)
    rows += _named_key_rows(T0 + 3.0, "enter")
    return rows


@pytest.fixture(scope="module")
def converted(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp_path = tmp_path_factory.mktemp("adapter")
    capture_dir = make_capture(tmp_path, demo_rows())
    recording_dir = tmp_path / "recording"
    convert_capture(
        capture_dir, recording_dir, params={"note": NOTE_VALUE}, settle_s=1.0
    )
    return recording_dir


def events_of(recording_dir: Path) -> list[dict]:
    import json

    lines = (recording_dir / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_event_mapping_and_order(converted: Path) -> None:
    events = events_of(converted)
    # The typed run coalesced into ONE type event; Enter is a separate key.
    assert [e["kind"] for e in events] == ["click", "type", "key"]
    assert events[1]["text"] == NOTE_VALUE
    assert events[1]["param"] == "note"
    assert events[2]["key"] == "Enter"
    # Recorder line-format parity: {"i", ...event fields..., "t"}.
    assert list(events[0].keys()) == ["i", "kind", "x", "y", "t"]
    assert [e["i"] for e in events] == [0, 1, 2]
    assert [e["t"] for e in events] == [1.0, 2.0, 3.0]


def test_coordinates_scaled_to_frame_pixels(converted: Path) -> None:
    # Capture points are logical (Retina /2); frames are physical pixels.
    click = events_of(converted)[0]
    assert (click["x"], click["y"]) == BUTTON_CENTER_PHYSICAL


def test_meta_matches_recorder_contract(converted: Path) -> None:
    import json

    meta = json.loads((converted / "meta.json").read_text())
    assert meta["viewport"] == list(FRAME_SIZE)
    assert meta["params"] == {"note": NOTE_VALUE}
    assert meta["app_url"] is None
    assert meta["source"] == "openadapt-capture"
    assert meta["task_description"] == "add a note"


def test_frames_selected_from_video(converted: Path) -> None:
    """Before frames precede each action; after frames show its effect."""
    import cv2

    screens = [np.array(s)[:, :, ::-1] for s in app_screens()]  # RGB->BGR

    def state_of(path: Path) -> int:
        """Nearest app state (mp4 is lossy, so classify, don't compare)."""
        actual = cv2.imdecode(
            np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR
        ).astype(np.int16)
        diffs = [float(np.abs(actual - s.astype(np.int16)).mean()) for s in screens]
        return int(np.argmin(diffs))

    frames = converted / "frames"
    assert state_of(frames / "0000_before.png") == 0
    assert state_of(frames / "0000_after.png") == 1
    assert state_of(frames / "0001_before.png") == 1
    assert state_of(frames / "0001_after.png") == 2
    assert state_of(frames / "0002_before.png") == 2
    assert state_of(frames / "0002_after.png") == 3


@pytest.mark.timeout(300)
def test_converted_recording_compiles(converted: Path, tmp_path: Path) -> None:
    """The unmodified compiler accepts the adapted desktop recording."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import ActionKind

    workflow = compile_recording(converted, tmp_path / "bundle", name="capture-bridge")
    assert [s.action for s in workflow.steps] == [
        ActionKind.CLICK,
        ActionKind.TYPE,
        ActionKind.KEY,
    ]
    type_step = workflow.steps[1]
    assert type_step.param == "note"


# -- scroll conversion -------------------------------------------------------


def test_scroll_notches_to_pixels_and_sign(tmp_path: Path) -> None:
    # pynput: +dy = scroll up (view up); flow: +dy = view down.
    rows = [
        {
            "name": "scroll",
            "timestamp": T0 + 1.0,
            "mouse_x": 100.0,
            "mouse_y": 100.0,
            "mouse_dx": 0.0,
            "mouse_dy": -3.0,
        },
    ]
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:2])
    recording_dir = tmp_path / "recording"
    convert_capture(capture_dir, recording_dir)
    (scroll,) = events_of(recording_dir)
    assert scroll["kind"] == "scroll"
    assert (scroll["dx"], scroll["dy"]) == (0, 300)


# -- loud rejection of silently-lossy inputs ----------------------------------


def test_no_actions_rejected(tmp_path: Path) -> None:
    # Mouse moves are filtered by actions(include_moves=False) -> nothing to do.
    rows = [
        {"name": "move", "timestamp": T0 + 1.0, "mouse_x": 5.0, "mouse_y": 5.0},
        {"name": "move", "timestamp": T0 + 1.5, "mouse_x": 9.0, "mouse_y": 9.0},
    ]
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="no convertible actions"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_drag_rejected_not_dropped(tmp_path: Path) -> None:
    # down -> moves -> up at a far position -> capture emits a mouse.drag.
    rows = [
        {
            "name": "click",
            "timestamp": T0 + 1.0,
            "mouse_x": 10.0,
            "mouse_y": 10.0,
            "mouse_button_name": "left",
            "mouse_pressed": True,
        },
        {"name": "move", "timestamp": T0 + 1.1, "mouse_x": 60.0, "mouse_y": 60.0},
        {"name": "move", "timestamp": T0 + 1.2, "mouse_x": 120.0, "mouse_y": 120.0},
        {
            "name": "click",
            "timestamp": T0 + 1.3,
            "mouse_x": 120.0,
            "mouse_y": 120.0,
            "mouse_button_name": "left",
            "mouse_pressed": False,
        },
    ]
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="mouse.drag"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_right_click_rejected(tmp_path: Path) -> None:
    rows = _click_rows(T0 + 1.0, 10.0, 10.0, button="right")
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="button='right'"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_unknown_named_key_rejected(tmp_path: Path) -> None:
    rows = _named_key_rows(T0 + 1.0, "f13")
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="f13"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_shortcut_rejected(tmp_path: Path) -> None:
    # ctrl down, c down, c up, ctrl up -> one key.type with keys=[ctrl, c].
    rows = [
        {"name": "press", "timestamp": T0 + 1.0, "key_name": "ctrl"},
        {"name": "press", "timestamp": T0 + 1.01, "key_char": "c"},
        {"name": "release", "timestamp": T0 + 1.02, "key_char": "c"},
        {"name": "release", "timestamp": T0 + 1.03, "key_name": "ctrl"},
    ]
    capture_dir = make_capture(tmp_path, rows, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="shortcut"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_ambiguous_params_rejected(tmp_path: Path) -> None:
    capture_dir = make_capture(tmp_path, demo_rows())
    with pytest.raises(ValueError, match="same value"):
        convert_capture(
            capture_dir,
            tmp_path / "recording",
            params={"a": NOTE_VALUE, "b": NOTE_VALUE},
        )


def test_missing_recording_db_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        convert_capture(empty, tmp_path / "recording")

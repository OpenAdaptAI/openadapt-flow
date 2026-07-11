"""Tests for the openadapt-capture -> openadapt-flow recording adapter.

Builds a synthetic capture session (SQLite ``capture.db`` + cv2-written
``video.mp4``, exactly the on-disk format openadapt-capture produces) and
validates the conversion contract: derived-event mapping, logical-point ->
physical-pixel coordinate scaling, before/after frame selection from the
video, parameter marking, and loud rejection of inputs that would silently
drop user actions. The converted recording is then fed to the UNMODIFIED
compiler — the format bridge, proven end to end.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pytest

from openadapt_flow.adapters.capture import convert_capture

# Physical (video) pixels; logical screen is half that (pixel_ratio 2.0,
# the macOS Retina case where capture coords and frame pixels disagree).
FRAME_SIZE = (1280, 800)
SCREEN_SIZE = (640, 400)
FPS = 10.0
T0 = 1000.0  # wall-clock epoch of capture + video start

BUTTON = (560, 400, 160, 48)  # physical px
BUTTON_CENTER_LOGICAL = (
    (BUTTON[0] + BUTTON[2] // 2) / 2,
    (BUTTON[1] + BUTTON[3] // 2) / 2,
)
BANNER_LOADED = "Chart Loaded Ok"
BANNER_SAVED = "Encounter Saved Successfully"
NOTE_VALUE = "confidential follow up note"


def blank() -> np.ndarray:
    return np.full((FRAME_SIZE[1], FRAME_SIZE[0], 3), 245, dtype=np.uint8)


def draw_text(img: np.ndarray, x: int, y: int, text: str) -> None:
    cv2.putText(
        img, text, (x, y),
        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2, cv2.LINE_AA,
    )


def draw_button(img: np.ndarray, x: int, y: int, w: int, h: int, label: str) -> None:
    cv2.rectangle(img, (x, y), (x + w, y + h), (205, 205, 205), -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (70, 70, 70), 2)
    draw_text(img, x + 12, y + h // 2 + 8, label)


def app_screens() -> list[np.ndarray]:
    s0 = blank()
    draw_text(s0, 520, 84, "MockMed Desktop")
    draw_button(s0, *BUTTON, "Open Chart")
    s1 = s0.copy()
    draw_text(s1, 420, 244, BANNER_LOADED)
    s2 = s1.copy()
    draw_text(s2, 560, 470, NOTE_VALUE)
    s3 = s2.copy()
    draw_text(s3, 420, 320, BANNER_SAVED)
    return [s0, s1, s2, s3]


def write_video(path: Path, screens: list[np.ndarray], seconds_each: float) -> None:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, FRAME_SIZE
    )
    assert writer.isOpened()
    for screen in screens:
        for _ in range(int(seconds_each * FPS)):
            writer.write(screen)
    writer.release()


def write_db(path: Path, events: list[dict]) -> None:
    """Write a capture.db with openadapt-capture's exact schema subset."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE capture (
            id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
            platform TEXT NOT NULL,
            screen_width INTEGER NOT NULL, screen_height INTEGER NOT NULL,
            task_description TEXT,
            double_click_interval_seconds REAL,
            double_click_distance_pixels REAL,
            video_start_time REAL, metadata JSON, pixel_ratio REAL DEFAULT 1.0
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL, type TEXT NOT NULL, data JSON NOT NULL,
            parent_id INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO capture (id, started_at, platform, screen_width,"
        " screen_height, task_description, video_start_time, pixel_ratio)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("cap1", T0, "darwin", SCREEN_SIZE[0], SCREEN_SIZE[1],
         "add a note", T0, 2.0),
    )
    for event in events:
        conn.execute(
            "INSERT INTO events (timestamp, type, data) VALUES (?, ?, ?)",
            (event["timestamp"], event["type"], json.dumps(event)),
        )
    conn.commit()
    conn.close()


def make_capture(tmp_path: Path, events: list[dict], screens=None) -> Path:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    write_db(capture_dir / "capture.db", events)
    write_video(
        capture_dir / "video.mp4",
        screens if screens is not None else app_screens(),
        seconds_each=1.0,
    )
    return capture_dir


def demo_events() -> list[dict]:
    """click -> type -> Enter, with raw/noise events interleaved."""
    x, y = BUTTON_CENTER_LOGICAL
    return [
        {"timestamp": T0 + 0.2, "type": "screen.frame", "video_timestamp": 0.2},
        {"timestamp": T0 + 0.9, "type": "mouse.move", "x": 5.0, "y": 5.0},
        {"timestamp": T0 + 1.0, "type": "mouse.singleclick",
         "x": x, "y": y, "button": "left"},
        {"timestamp": T0 + 1.5, "type": "key.down", "key_name": "shift"},
        {"timestamp": T0 + 2.0, "type": "key.type", "text": NOTE_VALUE},
        {"timestamp": T0 + 3.0, "type": "key.down", "key_name": "enter"},
        {"timestamp": T0 + 3.1, "type": "key.up", "key_name": "enter"},
    ]


@pytest.fixture(scope="module")
def converted(tmp_path_factory: pytest.TempPathFactory):
    tmp_path = tmp_path_factory.mktemp("adapter")
    capture_dir = make_capture(tmp_path, demo_events())
    recording_dir = tmp_path / "recording"
    convert_capture(
        capture_dir, recording_dir, params={"note": NOTE_VALUE}, settle_s=1.0
    )
    return recording_dir


def events_of(recording_dir: Path) -> list[dict]:
    lines = (recording_dir / "events.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_event_mapping_and_order(converted: Path) -> None:
    events = events_of(converted)
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
    assert (click["x"], click["y"]) == (
        BUTTON[0] + BUTTON[2] // 2,
        BUTTON[1] + BUTTON[3] // 2,
    )


def test_meta_matches_recorder_contract(converted: Path) -> None:
    meta = json.loads((converted / "meta.json").read_text())
    assert meta["viewport"] == list(FRAME_SIZE)
    assert meta["params"] == {"note": NOTE_VALUE}
    assert meta["app_url"] is None
    assert meta["source"] == "openadapt-capture"


def test_frames_selected_from_video(converted: Path) -> None:
    """Before frames precede each action; after frames show its effect."""
    screens = app_screens()

    def state_of(path: Path) -> int:
        """Nearest app state (mp4 is lossy, so classify, don't compare)."""
        actual = cv2.imdecode(
            np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR
        ).astype(np.int16)
        diffs = [
            float(np.abs(actual - s.astype(np.int16)).mean()) for s in screens
        ]
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

    workflow = compile_recording(
        converted, tmp_path / "bundle", name="capture-bridge"
    )
    assert [s.action for s in workflow.steps] == [
        ActionKind.CLICK,
        ActionKind.TYPE,
        ActionKind.KEY,
    ]
    type_step = workflow.steps[1]
    assert type_step.param == "note"


# -- scroll conversion -------------------------------------------------------


def test_scroll_notches_to_pixels_and_sign(tmp_path: Path) -> None:
    screens = app_screens()[:2]
    events = [
        # pynput: +dy = scroll up (view up); flow: +dy = view down.
        {"timestamp": T0 + 0.5, "type": "mouse.scroll",
         "x": 100.0, "y": 100.0, "dx": 0.0, "dy": -3.0},
    ]
    capture_dir = make_capture(tmp_path, events, screens=screens)
    recording_dir = tmp_path / "recording"
    convert_capture(capture_dir, recording_dir)
    (scroll,) = events_of(recording_dir)
    assert scroll["kind"] == "scroll"
    assert (scroll["dx"], scroll["dy"]) == (0, 300)


# -- loud rejection of silently-lossy inputs ----------------------------------


def test_raw_only_capture_rejected(tmp_path: Path) -> None:
    events = [
        {"timestamp": T0 + 0.5, "type": "mouse.down",
         "x": 1.0, "y": 1.0, "button": "left"},
        {"timestamp": T0 + 0.6, "type": "mouse.up",
         "x": 1.0, "y": 1.0, "button": "left"},
    ]
    capture_dir = make_capture(tmp_path, events, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="no derived action events"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_drag_rejected_not_dropped(tmp_path: Path) -> None:
    events = demo_events() + [
        {"timestamp": T0 + 3.5, "type": "mouse.drag",
         "x": 1.0, "y": 1.0, "dx": 5.0, "dy": 5.0, "button": "left"},
    ]
    capture_dir = make_capture(tmp_path, events)
    with pytest.raises(ValueError, match="mouse.drag"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_right_click_rejected(tmp_path: Path) -> None:
    events = [
        {"timestamp": T0 + 0.5, "type": "mouse.singleclick",
         "x": 10.0, "y": 10.0, "button": "right"},
    ]
    capture_dir = make_capture(tmp_path, events, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="button='right'"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_unknown_named_key_rejected(tmp_path: Path) -> None:
    events = [
        {"timestamp": T0 + 0.5, "type": "key.down", "key_name": "f13"},
    ]
    capture_dir = make_capture(tmp_path, events, screens=app_screens()[:1])
    with pytest.raises(ValueError, match="f13"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_unknown_input_type_rejected(tmp_path: Path) -> None:
    events = demo_events() + [
        {"timestamp": T0 + 3.6, "type": "mouse.gesture", "x": 1.0, "y": 1.0},
    ]
    capture_dir = make_capture(tmp_path, events)
    with pytest.raises(ValueError, match="mouse.gesture"):
        convert_capture(capture_dir, tmp_path / "recording")


def test_ambiguous_params_rejected(tmp_path: Path) -> None:
    capture_dir = make_capture(tmp_path, demo_events())
    with pytest.raises(ValueError, match="same value"):
        convert_capture(
            capture_dir,
            tmp_path / "recording",
            params={"a": NOTE_VALUE, "b": NOTE_VALUE},
        )

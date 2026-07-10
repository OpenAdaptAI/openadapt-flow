"""openadapt-capture -> openadapt-flow recording adapter.

This module is the *recording adapter contract* for desktop demonstrations
(docs/desktop/PHASE1.md): it converts an openadapt-capture session into the
recording format the compiler consumes (``meta.json`` + ``events.jsonl`` +
``frames/{i:04d}_before.png`` / ``_after.png``).

Input contract (an openadapt-capture session directory):

    <capture>/
      capture.db     # SQLite:
                     #   capture(id, started_at, ended_at, platform,
                     #           screen_width, screen_height, pixel_ratio,
                     #           video_start_time, task_description, ...)
                     #   events(timestamp REAL, type TEXT, data JSON, ...)
      video.mp4      # screen video; a frame's wall-clock time is
                     #   capture.video_start_time + frame_pts_seconds

The adapter consumes *derived* action events (openadapt-capture's
post-processing merges raw down/up/move streams into these):

    mouse.singleclick   {x, y, button}      -> {"kind": "click"}
    mouse.doubleclick   {x, y, button}      -> {"kind": "double_click"}
    key.type            {text}              -> {"kind": "type"}
    key.down            {key_name|key_char} -> {"kind": "key"} (named,
                                               non-modifier keys only)
    mouse.scroll        {x, y, dx, dy}      -> {"kind": "scroll"}

Raw-only sessions (no derived events) are rejected: run openadapt-capture's
``process_events`` first. Deriving actions from raw streams is the capture
library's job, not this adapter's.

Coordinate spaces: capture mouse coordinates are in *logical points*
(pynput); video frames are *physical pixels* (on Retina/HiDPI these differ
by ``pixel_ratio``). openadapt-flow requires event coordinates in the same
pixel space as the frames, so points are scaled by
``video_frame_width / capture.screen_width`` (empirical, not the stored
pixel_ratio — the video is authoritative).

Frame selection: for an event at wall-clock time ``T``, the *before* frame
is the last video frame at or before ``T`` and the *after* frame is the
frame at ``T + settle_s``, clamped to just before the next event (an
approximation of the live Recorder's perceptual-hash settle wait; see
docs/desktop/PHASE1.md for the tradeoff).

Scroll deltas: pynput reports wheel *notches* with positive ``dy`` = scroll
up, while the flow recording stores *pixels* with positive ``dy`` = view
down (Playwright wheel convention). Notches are converted at
``SCROLL_PIXELS_PER_NOTCH`` px/notch and the vertical sign is flipped.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Wheel-notch -> pixel conversion (matches the WindowsBackend constant; the
# exact ratio is not load-bearing — replay's closed-loop scroll re-resolves
# after each gesture).
SCROLL_PIXELS_PER_NOTCH = 100

# Capture event types this adapter consumes.
_ACTION_TYPES = {
    "mouse.singleclick",
    "mouse.doubleclick",
    "key.type",
    "key.down",
    "mouse.scroll",
}
# Derived action types with no flow equivalent: converting a demonstration
# that contains them would silently drop a user action (a wrong-action
# seed), so they fail the conversion loudly instead.
_REJECTED_ACTION_TYPES = {
    "mouse.drag",
    "key.shortcut",
}
# Raw / non-action streams, safely skipped (their information is already
# merged into the derived events or is irrelevant to replay).
_IGNORED_TYPES = {
    "mouse.move",
    "mouse.down",
    "mouse.up",
    "key.up",
    "screen.frame",
    "audio.chunk",
}

# pynput key names (openadapt-capture ``key_name``) -> flow/Playwright names.
_KEY_NAME_MAP = {
    "enter": "Enter",
    "tab": "Tab",
    "esc": "Escape",
    "escape": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "space": "Space",
    "home": "Home",
    "end": "End",
    "page_up": "PageUp",
    "page_down": "PageDown",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
}

# Bare modifier presses carry no workflow meaning (their effect is only
# visible combined with another key, which capture merges elsewhere).
_MODIFIER_KEY_NAMES = {
    "shift", "shift_l", "shift_r",
    "ctrl", "ctrl_l", "ctrl_r",
    "alt", "alt_l", "alt_r", "alt_gr",
    "cmd", "cmd_l", "cmd_r",
    "caps_lock",
}


@dataclass
class _Session:
    """Metadata row from capture.db's ``capture`` table."""

    started_at: float
    screen_width: int
    screen_height: int
    pixel_ratio: float
    video_start_time: Optional[float]
    task_description: Optional[str]


class _Video:
    """Random-access frame reader for the capture's screen video."""

    def __init__(self, path: Path) -> None:
        import cv2

        self._cv2 = cv2
        self._cap = cv2.VideoCapture(str(path))
        if not self._cap.isOpened():
            raise ValueError(f"cannot open capture video: {path}")
        self.fps = float(self._cap.get(cv2.CAP_PROP_FPS)) or 30.0
        self.frame_count = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.duration_s = (
            self.frame_count / self.fps if self.frame_count else 0.0
        )

    def frame_png_at(self, t_video: float) -> bytes:
        """Return the frame nearest to video-time ``t_video`` as PNG bytes."""
        t = min(max(t_video, 0.0), max(self.duration_s - 1.0 / self.fps, 0.0))
        index = min(
            int(round(t * self.fps)),
            max(self.frame_count - 1, 0),
        )
        self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = self._cap.read()
        if not ok:
            raise ValueError(
                f"cannot read video frame at t={t_video:.3f}s (index {index})"
            )
        ok, buf = self._cv2.imencode(".png", frame)
        if not ok:  # pragma: no cover - imencode failure is environmental
            raise ValueError("cannot encode video frame as PNG")
        return buf.tobytes()

    def close(self) -> None:
        self._cap.release()


def _load_session(db_path: Path) -> _Session:
    """Read the ``capture`` metadata row."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM capture LIMIT 1").fetchone()
    if row is None:
        raise ValueError(f"no capture row in {db_path}")
    keys = row.keys()
    return _Session(
        started_at=float(row["started_at"]),
        screen_width=int(row["screen_width"]),
        screen_height=int(row["screen_height"]),
        pixel_ratio=float(row["pixel_ratio"]) if "pixel_ratio" in keys and row["pixel_ratio"] is not None else 1.0,
        video_start_time=(
            float(row["video_start_time"])
            if "video_start_time" in keys and row["video_start_time"] is not None
            else None
        ),
        task_description=(
            row["task_description"] if "task_description" in keys else None
        ),
    )


def _load_action_events(db_path: Path) -> list[dict[str, Any]]:
    """Read derived action events (timestamp order) from capture.db."""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT timestamp, type, data FROM events ORDER BY timestamp"
        ).fetchall()
    seen_types = {r[1] for r in rows}
    rejected = sorted(seen_types & _REJECTED_ACTION_TYPES)
    if rejected:
        raise ValueError(
            f"capture contains derived action types {rejected} that have no "
            "openadapt-flow equivalent; converting would silently drop user "
            "actions"
        )
    # Unknown *input-stream* types are load-bearing (dropping one drops a
    # user action); unknown auxiliary streams (window/browser/audio
    # metadata) are not.
    unknown_input = sorted(
        t
        for t in seen_types
        if t.startswith(("mouse.", "key."))
        and t not in _ACTION_TYPES | _REJECTED_ACTION_TYPES | _IGNORED_TYPES
    )
    if unknown_input:
        raise ValueError(
            f"capture contains unknown input event types {unknown_input}; "
            "extend the adapter before converting"
        )
    actions = [
        {"timestamp": float(r[0]), "type": r[1], **json.loads(r[2])}
        for r in rows
        if r[1] in _ACTION_TYPES
    ]
    if not actions:
        raise ValueError(
            "capture.db contains no derived action events "
            f"(found types: {sorted(seen_types)}); run openadapt-capture's "
            "process_events() to merge raw mouse/keyboard streams first"
        )
    return actions


def _key_event_name(event: dict[str, Any]) -> Optional[str]:
    """Flow key name for a ``key.down`` event, or None to skip it."""
    key_name = event.get("key_name") or event.get("canonical_key_name")
    if key_name:
        if key_name in _MODIFIER_KEY_NAMES:
            return None
        mapped = _KEY_NAME_MAP.get(key_name)
        if mapped is None:
            raise ValueError(
                f"unmapped key.down key_name {key_name!r} at "
                f"t={event['timestamp']:.3f}; extend _KEY_NAME_MAP"
            )
        return mapped
    char = event.get("key_char") or event.get("canonical_key_char")
    return char or None


def convert_capture(
    capture_dir: Path | str,
    out_recording_dir: Path | str,
    *,
    params: Optional[dict[str, str]] = None,
    settle_s: float = 1.0,
) -> Path:
    """Convert an openadapt-capture session into a flow recording directory.

    Args:
        capture_dir: Directory containing ``capture.db`` and ``video.mp4``.
        out_recording_dir: Output recording directory (created if missing).
        params: Optional ``{param_name: demonstrated_value}`` map. A ``type``
            event whose text equals a demonstrated value is marked as that
            parameter (the compiler then treats it as per-run input and
            lints against value leakage).
        settle_s: Seconds after each action at which the *after* frame is
            sampled (clamped to just before the next event).

    Returns:
        The recording directory path (compile-ready).

    Raises:
        FileNotFoundError: If ``capture.db`` or ``video.mp4`` is missing.
        ValueError: On raw-only sessions, unmapped keys, non-left clicks,
            multiple params demonstrating the same value, or an unreadable
            video.
    """
    capture_dir = Path(capture_dir)
    out_dir = Path(out_recording_dir)
    db_path = capture_dir / "capture.db"
    video_path = capture_dir / "video.mp4"
    for path in (db_path, video_path):
        if not path.exists():
            raise FileNotFoundError(path)

    params = dict(params or {})
    value_to_param: dict[str, str] = {}
    for name, value in params.items():
        if value in value_to_param:
            raise ValueError(
                f"params {value_to_param[value]!r} and {name!r} demonstrate "
                "the same value; parameter marking would be ambiguous"
            )
        value_to_param[value] = name

    session = _load_session(db_path)
    events = _load_action_events(db_path)
    video = _Video(video_path)
    try:
        # Capture coords are logical points; frames are physical pixels.
        # The video is authoritative for the frame pixel space.
        scale_x = video.width / session.screen_width
        scale_y = video.height / session.screen_height
        video_t0 = (
            session.video_start_time
            if session.video_start_time is not None
            else session.started_at
        )

        frames_dir = out_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        events_path = out_dir / "events.jsonl"

        used_params: dict[str, str] = {}
        lines: list[str] = []
        i = 0
        for j, event in enumerate(events):
            line = _convert_event(event, scale_x, scale_y, value_to_param)
            if line is None:  # skippable (e.g. bare modifier press)
                continue
            if "param" in line:
                used_params[line["param"]] = line["text"]

            t_event = event["timestamp"] - video_t0
            t_after = t_event + settle_s
            if j + 1 < len(events):
                next_t = events[j + 1]["timestamp"] - video_t0
                t_after = min(t_after, max(next_t - 1.0 / video.fps, t_event))
            (frames_dir / f"{i:04d}_before.png").write_bytes(
                video.frame_png_at(t_event - 1.0 / video.fps)
            )
            (frames_dir / f"{i:04d}_after.png").write_bytes(
                video.frame_png_at(t_after)
            )

            line["i"] = i
            line["t"] = round(event["timestamp"] - session.started_at, 3)
            # Match the Recorder's key order ({"i", ...event, "t"}).
            ordered = {
                "i": line.pop("i"),
                **{k: v for k, v in line.items() if k != "t"},
                "t": line["t"],
            }
            lines.append(json.dumps(ordered))
            i += 1

        if i == 0:
            raise ValueError(
                "no convertible action events (all were skippable)"
            )
        events_path.write_text("\n".join(lines) + "\n")

        meta = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "viewport": [video.width, video.height],
            "app_url": None,
            "params": used_params,
            "source": "openadapt-capture",
            "task_description": session.task_description,
        }
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return out_dir
    finally:
        video.close()


def _convert_event(
    event: dict[str, Any],
    scale_x: float,
    scale_y: float,
    value_to_param: dict[str, str],
) -> Optional[dict[str, Any]]:
    """Convert one capture action event to a flow event line (sans i/t).

    Returns None for events that are valid but carry no workflow meaning
    (bare modifier presses).
    """
    etype = event["type"]
    if etype in ("mouse.singleclick", "mouse.doubleclick"):
        button = event.get("button", "left")
        if button != "left":
            raise ValueError(
                f"{etype} with button={button!r} has no flow equivalent "
                f"(t={event['timestamp']:.3f})"
            )
        kind = "click" if etype == "mouse.singleclick" else "double_click"
        return {
            "kind": kind,
            "x": int(round(event["x"] * scale_x)),
            "y": int(round(event["y"] * scale_y)),
        }
    if etype == "key.type":
        text = event["text"]
        line: dict[str, Any] = {"kind": "type", "text": text}
        if text in value_to_param:
            line["param"] = value_to_param[text]
        return line
    if etype == "key.down":
        key = _key_event_name(event)
        if key is None:
            return None
        return {"kind": "key", "key": key}
    if etype == "mouse.scroll":
        # pynput: notches, +dy = scroll up. Flow: pixels, +dy = view down.
        return {
            "kind": "scroll",
            "dx": int(round(event.get("dx", 0) * SCROLL_PIXELS_PER_NOTCH)),
            "dy": int(round(-event.get("dy", 0) * SCROLL_PIXELS_PER_NOTCH)),
        }
    raise ValueError(f"unhandled capture event type: {etype!r}")

"""openadapt-capture -> openadapt-flow recording adapter.

This module is the *recording adapter contract* for desktop demonstrations
(docs/desktop/PHASE1.md): it converts an openadapt-capture session into the
recording format the compiler consumes (``meta.json`` + ``events.jsonl`` +
``frames/{i:04d}_before.png`` / ``_after.png``).

Input contract (a real openadapt-capture >= 0.5 session directory):

    <capture>/
      recording.db            # SQLAlchemy per-capture database:
                              #   recording(timestamp, monitor_width,
                              #     monitor_height, platform, task_description,
                              #     video_start_time, config, ...)
                              #   action_event(name, timestamp, mouse_x,
                              #     mouse_y, mouse_dx, mouse_dy,
                              #     mouse_button_name, mouse_pressed, key_name,
                              #     key_char, canonical_key_name, ...)
      oa_recording-*.mp4      # action-gated screen video

The adapter is a thin bridge over openadapt-capture's **public API** — it does
*no* raw SQL and knows nothing about capture's schema. It calls
``CaptureSession.load(dir)`` and iterates ``.actions(include_moves=False)``,
which runs capture's own event-processing pipeline (raw mouse/keyboard streams
-> merged clicks / drags / typed text) and exposes each merged action as a
public ``Action`` (``.type``, ``.timestamp``, ``.x/.y/.dx/.dy``,
``.button/.text/.keys``). Frames come from ``CaptureSession.get_frame_at`` (the
same tested frame-extraction path ``Action.screenshot`` uses), so the adapter
inherits capture's decoding and survives capture's future schema changes.

Action mapping (capture ``Action.type`` -> flow event ``kind``):

    mouse.singleclick   -> {"kind": "click", x, y}
    mouse.doubleclick   -> {"kind": "double_click", x, y}
    mouse.scroll        -> {"kind": "scroll", dx, dy}
    key.type            -> {"kind": "type", text[, param]}  OR
                           {"kind": "key", key}             (see below)

capture's processing merges *all* keyboard input into ``key.type``
(``KeyTypeEvent``) actions, one per key-release burst — so a typed word arrives
as a *run* of single-character ``key.type`` actions, and a named key such as
Enter arrives as a ``key.type`` with **empty** ``.text`` and its name in
``.keys``. This adapter therefore:

  * coalesces consecutive character ``key.type`` actions (non-empty ``.text``,
    including spaces and shifted characters) into one flow ``type`` event, so
    the compiler sees a whole typed value (and per-run parameter marking works);
  * emits a flow ``key`` event for a named special key (empty ``.text``, e.g.
    Enter/Tab/Escape/arrows), mapped through ``_KEY_NAME_MAP``;
  * skips a bare modifier press (no workflow meaning on its own).

Loud rejection (a demonstrated action must never be *silently* dropped): drags
(``mouse.drag``), non-left clicks, modifier chords/shortcuts (ctrl/alt/cmd + a
key), unmapped named keys, and any unknown input action type all raise instead
of being ignored.

Coordinate spaces: capture mouse coordinates are in *logical points* (pynput);
video frames are *physical pixels*. openadapt-flow requires event coordinates in
the same pixel space as the frames, so points are scaled by
``CaptureSession.pixel_ratio`` (physical / logical). NOTE: sessions recorded
with capture >=0.5.4 persist ``pixel_ratio`` on the recording model itself, so
scaling is always correct for them. Older 0.5.x sessions carry it only when
the recorder wrote it into the recording ``config`` JSON; absent that it
defaults to 1.0 and coordinates pass through unscaled — on such a legacy HiDPI
session click coordinates would be under-scaled, an honest limitation of the
old metadata that this adapter cannot recover from pixels alone.

Window-scoped sessions (capture's window recording mode, capture PR #30). A
session recorded with ``Recorder(window=...)`` is scoped to ONE window: frames
are that window's own pixels (the same viewport flow's ``rdp_window`` replay
backend captures) and action coordinates were translated *at capture time*
into the captured frame's pixel space
(``CaptureSession.window_capture["coordinate_space"] == "window_pixels"``).
This adapter detects such a session and:

  * does NOT apply ``pixel_ratio`` — the coordinates are already frame
    pixels, and rescaling them would double-scale every click (the exact
    silent-mis-conversion this detection exists to prevent);
  * takes frames as-is (already the client-window viewport);
  * screens every mouse action against the recording's bounds-timeline
    window events (capture appends one whenever the resolved bounds/title
    change; ``state.viewport`` is the captured frame size in effect): window
    capture records out-of-window input at out-of-range coordinates instead
    of clamping, and such an action targeted a DIFFERENT window, so
    conversion refuses loudly (dropping it would silently lose a
    demonstrated action; keeping it would compile a wrong-target step);
  * stamps the output ``meta.json`` with the recorded scoping
    (``window_capture``) plus ``backend_hints`` naming the recorded target
    owner/title in ``BackendConfig`` terms (``rdp_window`` /
    ``rdp_window_title``) so a ``replay --backend rdp`` invocation can
    resolve the same client window. Both fields are additive; the compiler
    ignores unknown ``meta.json`` keys, and nothing in the replay path reads
    them yet (wiring them into the backend factory is a follow-up).

A window-scoped session that declares a coordinate space this adapter does
not understand is refused loudly rather than converted with guessed scaling.

Frame selection: openadapt-capture records **action-gated** video (frames are
encoded around user actions, not continuously). For an event at wall-clock time
``T`` the *before* frame is ``get_frame_at(T)`` and the *after* frame is
``get_frame_at(T + settle_s)`` clamped to just before the next event — an
approximation of the live Recorder's perceptual-hash settle wait (see
docs/desktop/PHASE1.md). Because the video is action-gated, a per-action frame
may be unavailable; a missing *before* frame for a click is fatal (the compiler
requires it), a missing *after* frame simply yields no postconditions for that
step.

Scroll deltas: pynput reports wheel *notches* with positive ``dy`` = scroll up,
while the flow recording stores *pixels* with positive ``dy`` = view down
(Playwright wheel convention). Notches are converted at
``SCROLL_PIXELS_PER_NOTCH`` px/notch and the vertical sign is flipped.

openadapt-capture is an **optional** dependency (the ``capture`` extra:
``pip install 'openadapt-flow[capture]'``); it is imported lazily so the flow
core never pulls it onto the replay hot path.

Structural-identity gap (desktop→web parity). This offline conversion produces
a recording of the SAME shape as a web recording, and it compiles into a valid
bundle, but it CANNOT carry the ``structural`` locator (UIA ``AutomationId`` /
role+name) that a DOM-armed web bundle gets: capture records only
mouse/keyboard/video, so there is no live accessibility tree at conversion time
to read an element identity from. Every ``anchor.structural`` is therefore None
and replay uses the VISUAL ladder (template/ocr/geometry). To get the
deterministic structural top rung on desktop, record LIVE over ``WindowsBackend``
via :func:`openadapt_flow.adapters.desktop_recorder.record_desktop_demo` (the
recorder arms UIA locators per click). Re-arming an already-converted capture
session against a live UIA tree is a separate follow-up documented in
``desktop_recorder`` — it is not done here.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence, TypeGuard

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_capture.capture import Action, CaptureSession
    from PIL.Image import Image

# Wheel-notch -> pixel conversion (matches the WindowsBackend constant; the
# exact ratio is not load-bearing — replay's closed-loop scroll re-resolves
# after each gesture).
SCROLL_PIXELS_PER_NOTCH = 100

# Search window (seconds) for get_frame_at. Capture is action-gated, so frames
# cluster around action timestamps; a generous window finds the nearest one.
FRAME_TOLERANCE_S = 2.0

# The one coordinate space a window-scoped capture session may declare today:
# action coordinates already translated into the captured frame's pixel space
# (see openadapt_capture.window_capture). Any other declared space is refused.
WINDOW_PIXEL_SPACE = "window_pixels"

# pynput key names (openadapt-capture ``key_name`` / ``.keys``) ->
# flow/Playwright names.
_KEY_NAME_MAP = {
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "esc": "Escape",
    "escape": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "home": "Home",
    "end": "End",
    "page_up": "PageUp",
    "page_down": "PageDown",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
}

# Bare modifier presses carry no workflow meaning on their own (their effect is
# only visible combined with another key).
_MODIFIER_KEY_NAMES = {
    "shift",
    "shift_l",
    "shift_r",
    "ctrl",
    "ctrl_l",
    "ctrl_r",
    "alt",
    "alt_l",
    "alt_r",
    "alt_gr",
    "cmd",
    "cmd_l",
    "cmd_r",
    "caps_lock",
}
# Modifiers that, combined with another key, form a shortcut/chord with no flow
# equivalent (shift is excluded — shift+char is just a shifted character).
_CHORD_MODIFIER_NAMES = _MODIFIER_KEY_NAMES - {
    "shift",
    "shift_l",
    "shift_r",
    "caps_lock",
}


def _require_capture() -> "type[CaptureSession]":
    """Return openadapt-capture's ``CaptureSession`` or raise a clear error.

    openadapt-capture is an optional dependency (the ``capture`` extra); import
    it lazily so the flow core never depends on it.
    """
    try:
        from openadapt_capture import CaptureSession
    except ImportError as exc:  # pragma: no cover - exercised via install state
        raise ImportError(
            "openadapt-capture is required to convert desktop recordings but "
            "is not installed. Install the optional extra:\n\n"
            "    pip install 'openadapt-flow[capture]'\n"
        ) from exc
    return CaptureSession


def _window_capture_meta(session: "CaptureSession") -> Optional[dict[str, Any]]:
    """Window-scoping metadata for a window-scoped session, else None.

    Reads the ``CaptureSession.window_capture`` property (openadapt-capture
    releases after 0.5.4) with a defensive fallback to the recording's
    ``config`` JSON — the persisted source of the same dict (key
    ``capture_window``) — for installed capture versions that predate the
    property. A window-scoped session loaded through an older package MUST
    still be recognized: missing the scoping would silently rescale
    already-frame-pixel coordinates by ``pixel_ratio`` (double-scaling every
    click), the exact mis-conversion this detection exists to prevent.
    """
    window_capture = getattr(session, "window_capture", None)
    if isinstance(window_capture, dict):
        return window_capture
    recording = getattr(session, "_recording", None)
    config = getattr(recording, "config", None)
    if isinstance(config, dict):
        fallback = config.get("capture_window")
        if isinstance(fallback, dict):
            return fallback
    return None


def _is_viewport(value: Any) -> TypeGuard[Sequence[float]]:
    """True when ``value`` is a plausible (width, height) pixel pair."""
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(v, (int, float)) and v > 0 for v in value)
    )


def _window_viewport_timeline(
    session: "CaptureSession", window_capture: dict[str, Any]
) -> list[tuple[float, tuple[int, int]]]:
    """Time-ordered ``(timestamp, (width, height))`` of the captured frame size.

    Built from the recording's bounds-timeline window events: capture's window
    mode appends one whenever the resolved window's bounds/title change, with
    the captured frame's pixel size in ``state["viewport"]`` — so a
    mid-recording resize is honored per-action. Read via a public
    ``session.window_events`` accessor when the installed capture exposes one,
    else via the session's underlying recording model (same rows). Falls back
    to the static ``window_capture["viewport"]`` when the session carries no
    timeline rows.

    Raises:
        ValueError: When neither source yields a viewport — without one,
            out-of-window input cannot be screened, and converting unscreened
            window-space coordinates could compile wrong-target steps.
    """
    entries: list[tuple[float, tuple[int, int]]] = []
    rows: Any = getattr(session, "window_events", None)
    if callable(rows):
        rows = rows()
    if rows is None:
        rows = getattr(getattr(session, "_recording", None), "window_events", None)
    for row in rows or []:
        state = getattr(row, "state", None)
        if not isinstance(state, dict) or not state.get("window_capture"):
            continue
        ts = getattr(row, "timestamp", None)
        viewport = state.get("viewport")
        if ts is None or not _is_viewport(viewport):
            continue
        entries.append((float(ts), (int(viewport[0]), int(viewport[1]))))
    entries.sort(key=lambda entry: entry[0])
    if not entries:
        viewport = window_capture.get("viewport")
        if not _is_viewport(viewport):
            raise ValueError(
                "window-scoped capture session carries no captured-frame "
                "viewport (no bounds-timeline window events and no viewport "
                "in its window-capture metadata); out-of-window input cannot "
                "be screened, so the session cannot be converted safely — "
                "re-record with a capture version that persists the window "
                "viewport"
            )
        entries.append((float("-inf"), (int(viewport[0]), int(viewport[1]))))
    return entries


def _reject_out_of_window(
    actions: "list[Action]",
    timeline: list[tuple[float, tuple[int, int]]],
) -> None:
    """Refuse loudly on any mouse action outside the captured window.

    Window-scoped recording translates GLOBAL input into the captured frame's
    pixel space *without clamping*, so input aimed at another window (or the
    desktop) lands at out-of-range coordinates. Such an action cannot replay
    faithfully inside the window: dropping it would silently lose a
    demonstrated action, and keeping it would compile a wrong-target step —
    so, per this module's loud-rejection policy, conversion refuses. Each
    action is checked against the viewport in effect at its timestamp (the
    latest bounds-timeline entry at or before it).
    """
    for action in actions:
        if not action.type.startswith("mouse."):
            continue
        x, y = action.x, action.y
        if x is None or y is None:
            continue
        ts = float(action.timestamp)
        width, height = timeline[0][1]
        for entry_ts, size in timeline:
            if entry_ts <= ts:
                width, height = size
            else:
                break
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(
                f"out-of-window input: {action.type} at ({x:.0f}, {y:.0f}) "
                f"(t={ts:.3f}) falls outside the captured window viewport "
                f"{width}x{height}; the demonstrated action targeted a "
                "different window or the desktop, so converting it would "
                "compile a wrong-target step — re-record keeping all input "
                "inside the captured window"
            )


def _flow_events(
    actions: "list[Action]",
    scale: float,
    value_to_param: dict[str, str],
) -> list[dict[str, Any]]:
    """Convert capture ``Action``s into ordered flow event dicts.

    Each returned dict carries the flow event fields plus a private ``_ts``
    (the source wall-clock timestamp, used later for frame selection). Runs of
    character ``key.type`` actions are coalesced into a single ``type`` event.

    Raises:
        ValueError: On any action that has no flow equivalent (drag, non-left
            click, modifier chord, unmapped named key, unknown input type) —
            converting it would silently drop a demonstrated action.
    """
    events: list[dict[str, Any]] = []
    # A run of typed characters, buffered so the compiler sees one ``type``
    # event per typed value (capture emits one key.type per key-release burst).
    text_run: dict[str, Any] = {"chars": [], "ts": None}

    def flush_text() -> None:
        if not text_run["chars"]:
            return
        text = "".join(text_run["chars"])
        line: dict[str, Any] = {
            "kind": "type",
            "text": text,
            "_ts": text_run["ts"],
        }
        if text in value_to_param:
            line["param"] = value_to_param[text]
        events.append(line)
        text_run["chars"] = []
        text_run["ts"] = None

    for action in actions:
        atype = action.type
        ts = float(action.timestamp)

        if atype in ("mouse.singleclick", "mouse.doubleclick"):
            flush_text()
            button = action.button or "left"
            if button != "left":
                raise ValueError(
                    f"{atype} with button={button!r} has no flow equivalent "
                    f"(t={ts:.3f}); converting would silently drop a user action"
                )
            kind = "click" if atype == "mouse.singleclick" else "double_click"
            events.append(
                {
                    "kind": kind,
                    "x": int(round((action.x or 0.0) * scale)),
                    "y": int(round((action.y or 0.0) * scale)),
                    "_ts": ts,
                }
            )
        elif atype == "mouse.scroll":
            flush_text()
            # pynput: notches, +dy = scroll up. Flow: pixels, +dy = view down.
            events.append(
                {
                    "kind": "scroll",
                    "dx": int(round((action.dx or 0.0) * SCROLL_PIXELS_PER_NOTCH)),
                    "dy": int(round(-(action.dy or 0.0) * SCROLL_PIXELS_PER_NOTCH)),
                    "_ts": ts,
                }
            )
        elif atype == "mouse.drag":
            raise ValueError(
                f"mouse.drag has no flow equivalent (t={ts:.3f}); converting "
                "would silently drop a user action"
            )
        elif atype == "key.type":
            _convert_key_type(action, ts, events, text_run, flush_text)
        elif atype == "mouse.move":
            continue  # defensive: include_moves=False already filters these
        elif atype.startswith(("mouse.", "key.")):
            raise ValueError(
                f"unknown input action type {atype!r} (t={ts:.3f}); extend the "
                "adapter before converting"
            )
        else:
            continue  # non-input auxiliary action; safely ignored

    flush_text()
    return events


def _convert_key_type(
    action: "Action",
    ts: float,
    events: list[dict[str, Any]],
    text_run: dict[str, Any],
    flush_text: Callable[[], None],
) -> None:
    """Handle one ``key.type`` action: accumulate text, or emit a key event.

    capture merges all keyboard input into ``key.type`` actions. This routes
    each one:

      * modifier chord (ctrl/alt/cmd + key) -> reject (no flow equivalent);
      * non-empty ``.text`` -> typed characters, accumulated into ``text_run``
        (spaces and shifted characters included);
      * empty ``.text`` with a single named special key -> flow ``key`` event;
      * bare modifier press -> skipped (no workflow meaning on its own).
    """
    keys = list(action.keys or [])
    mods = [k for k in keys if k in _MODIFIER_KEY_NAMES]
    non_mods = [k for k in keys if k not in _MODIFIER_KEY_NAMES]
    if non_mods and any(k in _CHORD_MODIFIER_NAMES for k in mods):
        raise ValueError(
            f"keyboard shortcut {'+'.join(keys)!r} has no flow equivalent "
            f"(t={ts:.3f}); converting would silently drop a user action"
        )

    if action.text:
        # Literal typed characters (a shifted character carries shift in .keys
        # but is still just text). Accumulated into the run buffer.
        if not text_run["chars"]:
            text_run["ts"] = ts
        text_run["chars"].append(action.text)
        return

    # Empty text: a named special key press (Enter/Tab/...) or a bare modifier.
    if not non_mods:
        return  # bare modifier press: nothing to emit, do not break a text run
    if len(non_mods) != 1:
        raise ValueError(
            f"key.type action with multiple named keys {non_mods!r} has no "
            f"flow equivalent (t={ts:.3f})"
        )
    name = non_mods[0]
    mapped = _KEY_NAME_MAP.get(name.lower())
    if mapped is None:
        raise ValueError(f"unmapped key {name!r} at t={ts:.3f}; extend _KEY_NAME_MAP")
    flush_text()
    events.append({"kind": "key", "key": mapped, "_ts": ts})


def _write_png(path: Path, image: "Image") -> None:
    """Write a PIL frame to ``path`` as PNG."""
    image.save(path, format="PNG")


def convert_capture(
    capture_dir: Path | str,
    out_recording_dir: Path | str,
    *,
    params: Optional[dict[str, str]] = None,
    settle_s: float = 1.0,
) -> Path:
    """Convert an openadapt-capture session into a flow recording directory.

    Consumes a real openadapt-capture session through its public API
    (``CaptureSession.load(dir).actions()``) and writes a compile-ready
    recording (``meta.json`` + ``events.jsonl`` + ``frames/``).

    A WINDOW-SCOPED session (``CaptureSession.window_capture`` not None; see
    the module docstring) is converted in its own pixel space: coordinates
    pass through unscaled (they are already captured-frame pixels), every
    mouse action is screened against the recorded bounds-timeline (an
    out-of-window action refuses conversion), and the output ``meta.json``
    additionally carries ``window_capture`` provenance plus ``backend_hints``
    (``rdp_window`` / ``rdp_window_title``) naming the recorded target window
    for ``replay --backend rdp``.

    Args:
        capture_dir: An openadapt-capture session directory (contains
            ``recording.db`` and an ``oa_recording-*.mp4`` video).
        out_recording_dir: Output recording directory (created if missing).
        params: Optional ``{param_name: demonstrated_value}`` map. A coalesced
            ``type`` event whose text equals a demonstrated value is marked as
            that parameter (the compiler then treats it as per-run input and
            lints against value leakage).
        settle_s: Seconds after each action at which the *after* frame is
            sampled (clamped to just before the next event).

    Returns:
        The recording directory path (compile-ready).

    Raises:
        ImportError: If the optional ``capture`` extra (openadapt-capture) is
            not installed.
        FileNotFoundError: If ``capture_dir`` is not a valid capture session
            (no ``recording.db``) — raised by ``CaptureSession.load``.
        ValueError: On a session with no convertible actions, an action with no
            flow equivalent (drag, non-left click, modifier chord, unmapped
            named key, unknown input type), multiple params demonstrating the
            same value, or a click whose before frame is missing from the
            action-gated video. Also, for a window-scoped session: on an
            out-of-window action, an unknown declared coordinate space, or
            missing viewport metadata (out-of-window input could not be
            screened) — refusing loudly instead of mis-converting.
    """
    CaptureSession = _require_capture()
    capture_dir = Path(capture_dir)
    out_dir = Path(out_recording_dir)

    params = dict(params or {})
    value_to_param: dict[str, str] = {}
    for name, value in params.items():
        if value in value_to_param:
            raise ValueError(
                f"params {value_to_param[value]!r} and {name!r} demonstrate "
                "the same value; parameter marking would be ambiguous"
            )
        value_to_param[value] = name

    session = CaptureSession.load(capture_dir)
    try:
        window_capture = _window_capture_meta(session)
        if window_capture is not None:
            space = window_capture.get("coordinate_space")
            if space != WINDOW_PIXEL_SPACE:
                raise ValueError(
                    "window-scoped capture session declares "
                    f"coordinate_space={space!r}, which this adapter does not "
                    f"understand (expected {WINDOW_PIXEL_SPACE!r}); converting "
                    "it could silently mis-scale action coordinates — upgrade "
                    "openadapt-flow to a version that supports this session's "
                    "coordinate space"
                )
            # Window mode: coordinates were translated at capture time into
            # the captured frame's pixel space. Applying pixel_ratio here
            # would DOUBLE-scale them; frames are already the window viewport.
            scale = 1.0
        else:
            scale = float(session.pixel_ratio or 1.0)
        actions = list(session.actions(include_moves=False))
        if window_capture is not None:
            _reject_out_of_window(
                actions, _window_viewport_timeline(session, window_capture)
            )
        events = _flow_events(actions, scale, value_to_param)
        if not events:
            raise ValueError(
                "capture session produced no convertible actions "
                f"({len(actions)} raw actions); nothing to compile"
            )

        started_at = float(session.started_at)
        frames_dir = out_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        used_params: dict[str, str] = {}
        viewport: Optional[list[int]] = None
        lines: list[str] = []

        for i, event in enumerate(events):
            ts = float(event["_ts"])
            before_img = session.get_frame_at(ts, tolerance=FRAME_TOLERANCE_S)
            t_after = ts + settle_s
            if i + 1 < len(events):
                t_after = min(t_after, float(events[i + 1]["_ts"]))
            after_img = session.get_frame_at(t_after, tolerance=FRAME_TOLERANCE_S)

            if event["kind"] in ("click", "double_click") and before_img is None:
                raise ValueError(
                    f"no video frame available for {event['kind']} at "
                    f"t={ts - started_at:.3f}s: the action-gated capture video "
                    "has no frame near this action, so its target cannot be "
                    "anchored"
                )
            if before_img is not None:
                _write_png(frames_dir / f"{i:04d}_before.png", before_img)
                if viewport is None:
                    viewport = [before_img.width, before_img.height]
            if after_img is not None:
                _write_png(frames_dir / f"{i:04d}_after.png", after_img)
                if viewport is None:
                    viewport = [after_img.width, after_img.height]

            if "param" in event:
                used_params[event["param"]] = event["text"]

            line: dict[str, Any] = {"i": i}
            line.update({k: v for k, v in event.items() if k != "_ts"})
            line["t"] = round(ts - started_at, 3)
            lines.append(json.dumps(line))

        (out_dir / "events.jsonl").write_text("\n".join(lines) + "\n")

        meta: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "viewport": viewport,
            "app_url": None,
            "params": used_params,
            "source": "openadapt-capture",
            "task_description": session.task_description,
        }
        if window_capture is not None:
            # Additive provenance + replay hints (the compiler ignores unknown
            # meta.json keys; a non-window session's meta is unchanged). The
            # hints carry the recorded TARGET owner/title substrings — the
            # user's proven-to-resolve intent, stabler across replays than the
            # resolved window's live title — in BackendConfig terms
            # (rdp_window / rdp_window_title), so `replay --backend rdp` can
            # resolve the same client window. Resolved values are kept as
            # provenance and used only when the target carried neither field.
            target = window_capture.get("target") or {}
            owner = target.get("owner")
            title = target.get("title")
            if owner is None and title is None:
                owner = window_capture.get("owner")
                title = window_capture.get("title")
            meta["window_capture"] = {
                "coordinate_space": WINDOW_PIXEL_SPACE,
                "target_owner": owner,
                "target_title": title,
                "resolved_owner": window_capture.get("owner"),
                "resolved_title": window_capture.get("title"),
            }
            hints: dict[str, Any] = {"backend": "rdp"}
            if owner:
                hints["rdp_window"] = owner
            if title:
                hints["rdp_window_title"] = title
            meta["backend_hints"] = hints
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return out_dir
    finally:
        session.close()

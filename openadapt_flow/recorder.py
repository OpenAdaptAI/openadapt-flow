"""Recorder: wraps a Backend and captures frames + events for each action.

Recording format (DESIGN.md):

    <recording>/
      meta.json          # {"id", "created_at", "viewport": [w,h], "app_url",
                         #  "params": {"<param_name>": "<value typed>"}}
      events.jsonl       # {"i":0,"kind":"click","x":123,"y":45,"t":1.20}
                         # {"i":1,"kind":"type","text":"...","param":"note",...}
                         # {"i":2,"kind":"key","key":"Enter","t":3.10}
                         # {"i":3,"kind":"scroll","dx":0,"dy":400,"t":4.02}
                         # Events additionally carry url/title/pages
                         # _before/_after keys when the backend exposes
                         # structural observations (StructuralBackend).
      frames/{i:04d}_before.png
      frames/{i:04d}_after.png   # captured after the action settled

The settle wait is implemented inline here (imagehash polling) on purpose:
this module must not depend on `openadapt_flow.vision`.
"""

from __future__ import annotations

import io
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import imagehash
from PIL import Image

from openadapt_flow.backend import Backend


def _phash(png: bytes) -> imagehash.ImageHash:
    """Perceptual hash of a PNG frame."""
    with Image.open(io.BytesIO(png)) as img:
        return imagehash.phash(img)


class Recorder:
    """Records a demonstration by mirroring backend actions to disk.

    Each action captures a before frame, performs the action on the wrapped
    backend, waits for the screen to settle (polling screenshots until two
    consecutive frames have identical perceptual hashes, with a timeout),
    captures the after frame, and appends one event line to ``events.jsonl``.

    Args:
        backend: The `Backend` to act through and screenshot from.
        out_dir: Recording directory; created if missing. `finish()` returns
            this path.
        app_url: Optional app URL stored in ``meta.json``.
        settle_interval_s: Poll interval for the settle wait.
        settle_stable_frames: Consecutive identical frames required.
        settle_timeout_s: Max seconds to wait for the screen to settle.
    """

    def __init__(
        self,
        backend: Backend,
        out_dir: Path | str,
        *,
        app_url: Optional[str] = None,
        settle_interval_s: float = 0.1,
        settle_stable_frames: int = 2,
        settle_timeout_s: float = 3.0,
    ) -> None:
        self._backend = backend
        self._dir = Path(out_dir)
        self._frames_dir = self._dir / "frames"
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self._dir / "events.jsonl"
        self._events_path.write_text("")  # truncate any stale file
        self._app_url = app_url
        self._settle_interval_s = settle_interval_s
        self._settle_stable_frames = settle_stable_frames
        self._settle_timeout_s = settle_timeout_s
        self._params: dict[str, str] = {}
        self._i = 0
        self._t0 = time.monotonic()

    # -- action API (mirrors Backend, plus param-aware type_text) -----------

    def click(self, x: int, y: int) -> None:
        """Click at pixel coordinates, recording the event and frames."""
        self._record(
            {"kind": "click", "x": int(x), "y": int(y)},
            lambda: self._backend.click(int(x), int(y)),
        )

    def double_click(self, x: int, y: int) -> None:
        """Double-click at pixel coordinates, recording the event."""
        self._record(
            {"kind": "double_click", "x": int(x), "y": int(y)},
            lambda: self._backend.click(int(x), int(y), double=True),
        )

    def type_text(self, text: str, param: Optional[str] = None) -> None:
        """Type text into the focused element, recording the event.

        Args:
            text: The literal text typed during the demonstration.
            param: If set, the typed value is a workflow parameter with this
                name; it is included on the event and in ``meta.json`` params.
        """
        event: dict[str, Any] = {"kind": "type", "text": text}
        if param is not None:
            event["param"] = param
            self._params[param] = text
        self._record(event, lambda: self._backend.type_text(text))

    def press(self, key: str) -> None:
        """Press a key or chord (e.g. ``'Enter'``), recording the event."""
        self._record({"kind": "key", "key": key}, lambda: self._backend.press(key))

    def scroll(self, dx: int, dy: int) -> None:
        """Scroll by (dx, dy) pixels via the wheel, recording the event."""
        self._record(
            {"kind": "scroll", "dx": int(dx), "dy": int(dy)},
            lambda: self._backend.scroll(int(dx), int(dy)),
        )

    # -- lifecycle -----------------------------------------------------------

    def finish(self) -> Path:
        """Write ``meta.json`` and return the recording directory."""
        viewport = self._backend.viewport
        meta = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "viewport": [int(viewport[0]), int(viewport[1])],
            "app_url": self._app_url,
            "params": dict(self._params),
        }
        (self._dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return self._dir

    # -- internals -----------------------------------------------------------

    def _record(self, event: dict[str, Any], act: Callable[[], None]) -> None:
        """Capture before frame, act, wait settle, capture after, log event."""
        i = self._i
        before = self._backend.screenshot()
        (self._frames_dir / f"{i:04d}_before.png").write_bytes(before)
        structural_before = self._structural_state()
        act()
        after = self._wait_settled()
        (self._frames_dir / f"{i:04d}_after.png").write_bytes(after)
        line: dict[str, Any] = {"i": i, **event}
        for key, value in structural_before.items():
            line[f"{key}_before"] = value
        for key, value in self._structural_state().items():
            line[f"{key}_after"] = value
        line["t"] = round(time.monotonic() - self._t0, 3)
        with self._events_path.open("a") as f:
            f.write(json.dumps(line) + "\n")
        self._i += 1

    def _structural_state(self) -> dict[str, Any]:
        """Structural observations the backend can provide right now.

        Backends MAY expose ``url`` / ``page_title`` / ``page_count`` (see
        ``openadapt_flow.backend.StructuralBackend``); whatever is available
        is captured per event so the compiler can mine structural
        postconditions (URL/title change, new tab) for steps whose action
        changed nothing visible in the frame. Missing observations are
        simply absent from the event.
        """
        state: dict[str, Any] = {}
        for attr, key in (
            ("url", "url"),
            ("page_title", "title"),
            ("page_count", "pages"),
        ):
            try:
                value = getattr(self._backend, attr, None)
            except Exception:
                value = None
            if value is not None:
                state[key] = value
        return state

    def _wait_settled(self) -> bytes:
        """Poll screenshots until N consecutive identical hashes or timeout.

        Returns:
            The last PNG frame captured (settled if achieved before timeout).
        """
        deadline = time.monotonic() + self._settle_timeout_s
        png = self._backend.screenshot()
        prev = _phash(png)
        consecutive = 1
        while (
            consecutive < self._settle_stable_frames
            and time.monotonic() < deadline
        ):
            time.sleep(self._settle_interval_s)
            png = self._backend.screenshot()
            cur = _phash(png)
            if cur - prev == 0:
                consecutive += 1
            else:
                consecutive = 1
            prev = cur
        return png

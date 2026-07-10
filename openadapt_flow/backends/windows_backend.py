"""Windows backend over the WAA (Windows Agent Arena) HTTP API.

Implements the `openadapt_flow.backend.Backend` protocol against the WAA
Flask server that runs inside the Windows VM (the WAADirect pattern from
openadapt-evals — plain HTTP, no adapter layer):

    GET  /screenshot        -> raw PNG bytes (Flask send_file, NOT base64 JSON)
    POST /execute_windows   -> server does exec(command, ...) with pyautogui
                               importable. The payload is
                               ``{"command": "<bare Python statements>"}`` —
                               NOT wrapped in ``python -c "..."``.

The backend is vision-only by construction: PNG frames in, pixel-coordinate
input out. It deliberately does NOT implement the optional
`StructuralBackend` observations (url/title/page count) — native Windows has
no cheap equivalent, so those steps stay honestly unverified (docs/LIMITS.md).

Typed text is embedded into the command via ``repr()`` (a valid Python
literal, immune to quoting bugs). Non-ASCII text cannot be typed by
pyautogui.write (it silently drops unknown characters — a wrong-write mode),
so it is routed through the Windows clipboard instead: the value travels
base64-encoded to PowerShell ``Set-Clipboard`` and is pasted with Ctrl+V.
"""

from __future__ import annotations

import base64
import struct
from typing import Optional

import requests

DEFAULT_SERVER_URL = "http://localhost:5001"

# Screenshot retry defaults (mirrors WAADirect in openadapt-evals).
SCREENSHOT_MAX_RETRIES = 3
SCREENSHOT_RETRY_DELAY_S = 2.0

# The Backend protocol expresses scroll in pixels (a Playwright wheel
# gesture); pyautogui expresses it in wheel notches. One notch scrolls
# roughly this many pixels in common Windows apps. Approximate by design —
# to be tuned against the real VM (Phase 2); replay's closed-loop scroll
# re-resolves after each gesture, so the exact ratio is not load-bearing.
SCROLL_PIXELS_PER_NOTCH = 100

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Playwright-style modifier names (as recorded / emitted by the replayer,
# e.g. 'ControlOrMeta+a') -> pyautogui key names. On Windows the Meta/Command
# key maps to the Windows key and ControlOrMeta resolves to Ctrl.
_MODIFIER_MAP = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "controlormeta": "ctrl",
    "meta": "win",
    "cmd": "win",
    "command": "win",
    "win": "win",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
}

# Playwright-style named keys -> pyautogui key names.
_NAMED_KEY_MAP = {
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "escape": "esc",
    "esc": "esc",
    "backspace": "backspace",
    "delete": "delete",
    "space": "space",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}


def _png_size(png: bytes) -> tuple[int, int]:
    """Return (width, height) parsed from a PNG's IHDR chunk.

    Args:
        png: PNG file bytes.

    Returns:
        (width, height) in pixels.

    Raises:
        ValueError: If the bytes are not a PNG.
    """
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG frame")
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)


def _pyautogui_key(part: str) -> str:
    """Map one Playwright-style key/modifier name to pyautogui's name."""
    lower = part.lower()
    if lower in _MODIFIER_MAP:
        return _MODIFIER_MAP[lower]
    if lower in _NAMED_KEY_MAP:
        return _NAMED_KEY_MAP[lower]
    # Single characters and F-keys: pyautogui uses lowercase names.
    return lower


def normalize_chord(key: str) -> list[str]:
    """Normalize a key or ``+``-joined chord to pyautogui key names.

    Args:
        key: e.g. ``'Enter'``, ``'ControlOrMeta+a'``, ``'Meta+d'``.

    Returns:
        List of pyautogui key names, modifiers first (input order kept).

    Raises:
        ValueError: If the chord is empty.
    """
    parts = [p for p in key.split("+") if p]
    if not parts:
        raise ValueError(f"empty key chord: {key!r}")
    return [_pyautogui_key(p) for p in parts]


class WindowsBackend:
    """`Backend` implementation over the WAA HTTP API.

    Args:
        server_url: WAA Flask server base URL (default matches the standard
            SSH tunnel ``localhost:5001`` -> VM ``:5000``).
        viewport: Optional (width, height) override. When omitted it is
            derived once from the first screenshot's PNG header and cached.
        type_interval_s: Inter-key delay for ``pyautogui.write``.
        timeout_s: HTTP timeout per request.
        screenshot_max_retries: Screenshot retry attempts (server may be
            momentarily unready).
        screenshot_retry_delay_s: Delay between screenshot retries.
        session: Optional ``requests.Session`` (injected in tests).
    """

    def __init__(
        self,
        server_url: str = DEFAULT_SERVER_URL,
        *,
        viewport: Optional[tuple[int, int]] = None,
        type_interval_s: float = 0.05,
        timeout_s: float = 30.0,
        screenshot_max_retries: int = SCREENSHOT_MAX_RETRIES,
        screenshot_retry_delay_s: float = SCREENSHOT_RETRY_DELAY_S,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self._viewport = viewport
        self._type_interval_s = type_interval_s
        self._timeout_s = timeout_s
        self._screenshot_max_retries = max(1, int(screenshot_max_retries))
        self._screenshot_retry_delay_s = screenshot_retry_delay_s
        self._session = session if session is not None else requests.Session()

    # -- Backend protocol ----------------------------------------------------

    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the VM screen, derived from a screenshot."""
        if self._viewport is None:
            self._viewport = _png_size(self.screenshot())
        return self._viewport

    def screenshot(self) -> bytes:
        """Return the current frame as PNG bytes (with retries).

        WAA's ``/screenshot`` returns raw PNG via Flask ``send_file()`` —
        read ``resp.content``, never ``resp.json()``.

        Raises:
            RuntimeError: If all attempts fail or return a non-PNG payload.
        """
        import time

        last_error: Optional[Exception] = None
        for attempt in range(1, self._screenshot_max_retries + 1):
            try:
                resp = self._session.get(
                    f"{self.server_url}/screenshot", timeout=self._timeout_s
                )
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"screenshot HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                png = resp.content
                _png_size(png)  # validates signature and header
                return png
            except Exception as e:  # noqa: BLE001 - retried, then re-raised
                last_error = e
                if attempt < self._screenshot_max_retries:
                    time.sleep(self._screenshot_retry_delay_s)
        raise RuntimeError(
            f"screenshot failed after {self._screenshot_max_retries} attempts"
        ) from last_error

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click (or double-click) at pixel coordinates via pyautogui."""
        fn = "doubleClick" if double else "click"
        self._execute(f"import pyautogui; pyautogui.{fn}({int(x)}, {int(y)})")

    def type_text(self, text: str) -> None:
        """Type text into the currently focused element.

        ASCII text goes through ``pyautogui.write``. Text containing
        characters pyautogui cannot type (it silently drops them — a
        wrong-write failure mode) goes through the Windows clipboard:
        base64 -> PowerShell ``Set-Clipboard`` -> Ctrl+V.
        """
        if not text:
            return
        if text.isascii():
            self._execute(
                "import pyautogui; "
                f"pyautogui.write({text!r}, "
                f"interval={self._type_interval_s})"
            )
            return
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        ps = (
            "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{b64}')))"
        )
        self._execute(
            "\n".join(
                [
                    "import subprocess; import time; import pyautogui",
                    "subprocess.run(['powershell', '-NoProfile', '-Command', "
                    f"{ps!r}], capture_output=True)",
                    "time.sleep(0.2)",
                    "pyautogui.hotkey('ctrl', 'v')",
                ]
            )
        )

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. ``'Enter'`` or ``'ControlOrMeta+a'``."""
        keys = normalize_chord(key)
        if len(keys) == 1:
            self._execute(
                f"import pyautogui; pyautogui.press({keys[0]!r})"
            )
        else:
            args = ", ".join(repr(k) for k in keys)
            self._execute(f"import pyautogui; pyautogui.hotkey({args})")

    def scroll(self, dx: int, dy: int) -> None:
        """Dispatch a wheel gesture (Backend convention: positive ``dy``
        scrolls content up / view down).

        pyautogui's convention is inverted for vertical scrolling (positive
        = view up), so ``dy`` changes sign; horizontal signs agree.
        """
        parts: list[str] = ["import pyautogui"]
        v = self._notches(dy)
        h = self._notches(dx)
        if v:
            parts.append(f"pyautogui.scroll({-v})")
        if h:
            parts.append(f"pyautogui.hscroll({h})")
        if len(parts) == 1:
            return
        self._execute("; ".join(parts))

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _notches(pixels: int) -> int:
        """Convert a pixel delta to wheel notches (>=1 notch when nonzero)."""
        if pixels == 0:
            return 0
        notches = round(abs(pixels) / SCROLL_PIXELS_PER_NOTCH)
        return max(1, notches) * (1 if pixels > 0 else -1)

    def _execute(self, command: str) -> None:
        """POST bare Python statements to WAA's ``/execute_windows``.

        Raises:
            RuntimeError: On a non-200 response.
        """
        resp = self._session.post(
            f"{self.server_url}/execute_windows",
            json={"command": command},
            timeout=self._timeout_s,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"execute_windows HTTP {resp.status_code}: {resp.text[:200]}"
            )

    def probe(self) -> bool:
        """True if the WAA server answers with a valid PNG screenshot."""
        try:
            self.screenshot()
            return True
        except RuntimeError:
            return False

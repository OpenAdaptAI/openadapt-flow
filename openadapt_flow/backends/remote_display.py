"""Local-OS pixel backend: drive a REMOTE-DISPLAY CLIENT WINDOW, pixels only.

This is the faithful **Citrix analog** the healthcare pilot actually reuses. Over
Citrix, the local machine holds a **Citrix Workspace/Receiver window that paints
the pixels of a remote session**; there is no in-guest agent on our side of the
ICA boundary and **UIA/MSAA does not cross ICA**. The production Accuro-over-Citrix
wire is therefore NOT the RDP protocol client (:mod:`openadapt_flow.backends.rdp_backend`
speaks RDP, which is not ICA) — it is a **local-OS backend that screenshots the
Workspace client window and injects OS-level input into it**. This module is that
backend, built on macOS (Quartz / AppKit) against any on-screen client window.

For the proof on infra we control the client window is the **Parallels Desktop VM
window** showing the guest desktop: same *class* of substrate as Citrix Workspace
— host-side pixels of a remote guest, host OS input injected into the window, and
**no access to the guest UIA tree through the window**. Swapping the target from
the Parallels window to a "Citrix Workspace"/"Accuro" window is a one-line title
change; the screenshot + inject code is identical. (What real Citrix adds — HDX
compression, network latency, DPI/credential/lock-screen drift — is documented in
``docs/desktop/CITRIX_PIXEL.md``, not simulated here.)

Faithful pixel-only property (the whole point):

* :meth:`RemoteDisplayBackend.screenshot` captures **only the client window's
  pixels** (``CGWindowListCreateImage`` by window id) — the driving process sees
  the remote display, nothing else.
* input is injected at the **host OS level** (``CGEvent`` mouse / keyboard /
  wheel) into that window, mapped from screenshot-pixel space to screen points.
* the backend deliberately implements **only** the base
  :class:`openadapt_flow.backend.Backend` protocol — NOT ``StructuralBackend``,
  ``IdentityBackend`` or ``StructuralActionBackend``. So the resolver's
  ``structural`` (UIA) rung is **unavailable** and resolution runs on the visual
  floor (template / OCR / geometry / grounder); identity falls back to the OCR
  name+DOB tier — exactly the Citrix constraint (``backend.py`` protocol notes,
  ``docs/desktop/LIMITS.md``).

Permission reality (macOS, and the honest failure mode): window capture needs
**Screen Recording** permission and input injection needs **Accessibility**
permission for the driving app (Terminal / the Python host). If Accessibility is
not granted, ``CGEventPost`` is **silently dropped** — a dropped click that looked
like success is the exact "silent wrong action" this project refuses. So every
input method FAILS LOUD (raises :class:`RemoteDisplayError`) when the process is
not Accessibility-trusted; a caller never mistakes a no-op for a completed action.

The macOS bindings are isolated behind :class:`MacWindowClient` (a small
:class:`WindowClient` seam) so the coordinate math, the pixel<->point mapping, and
the fail-loud contract are unit-testable with a fake client and no live window.
"""

from __future__ import annotations

import io
import struct
import time
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class RemoteDisplayError(RuntimeError):
    """A remote-display capture/inject operation failed (or is not permitted)."""


@dataclass(frozen=True)
class WindowInfo:
    """One on-screen window, as reported by the window server.

    ``bounds`` is ``(x, y, w, h)`` in **screen points** (top-left origin — the
    same space ``CGEvent`` mouse coordinates use), so a captured-pixel point maps
    to a screen point by ``bounds_origin + pixel / scale``.
    """

    window_id: int
    owner: str
    title: str
    pid: int
    bounds: tuple[float, float, float, float]
    on_screen: bool = True


# macOS US-layout virtual key codes for printable characters. A synthetic
# Unicode keystroke (keycode 0 + ``CGEventKeyboardSetUnicodeString``) is NOT
# forwarded into a Parallels/Citrix guest — the remote display forwards
# hardware-like SCANCODES, so a real key code (with Shift for the upper glyph)
# is required. ``(keycode, needs_shift)``.
_CHAR_KEYCODES: dict[str, tuple[int, bool]] = {}
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _CHAR_KEYCODES[_ch] = (
        {
            "a": 0x00,
            "b": 0x0B,
            "c": 0x08,
            "d": 0x02,
            "e": 0x0E,
            "f": 0x03,
            "g": 0x05,
            "h": 0x04,
            "i": 0x22,
            "j": 0x26,
            "k": 0x28,
            "l": 0x25,
            "m": 0x2E,
            "n": 0x2D,
            "o": 0x1F,
            "p": 0x23,
            "q": 0x0C,
            "r": 0x0F,
            "s": 0x01,
            "t": 0x11,
            "u": 0x20,
            "v": 0x09,
            "w": 0x0D,
            "x": 0x07,
            "y": 0x10,
            "z": 0x06,
        }[_ch],
        False,
    )
    _CHAR_KEYCODES[_ch.upper()] = (_CHAR_KEYCODES[_ch][0], True)
_DIGIT_KEYCODES = {
    "0": 0x1D,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "5": 0x17,
    "6": 0x16,
    "7": 0x1A,
    "8": 0x1C,
    "9": 0x19,
}
for _d, _kc in _DIGIT_KEYCODES.items():
    _CHAR_KEYCODES[_d] = (_kc, False)
# punctuation / symbols used by clinical text (base glyph, then shifted glyph)
_CHAR_KEYCODES.update(
    {
        " ": (0x31, False),
        ".": (0x2F, False),
        ",": (0x2B, False),
        "-": (0x1B, False),
        "/": (0x2C, False),
        ";": (0x29, False),
        "=": (0x18, False),
        "'": (0x27, False),
        "[": (0x21, False),
        "]": (0x1E, False),
        "\\": (0x2A, False),
        "`": (0x32, False),
        ":": (0x29, True),
        "_": (0x1B, True),
        "?": (0x2C, True),
        "!": (0x12, True),
        "(": (0x19, True),
        ")": (0x1D, True),
        "+": (0x18, True),
        '"': (0x27, True),
        "@": (0x13, True),
        "#": (0x14, True),
        "%": (0x17, True),
        "&": (0x1A, True),
        "*": (0x1C, True),
        "$": (0x15, True),
    }
)

# macOS virtual key codes for the named keys the Backend protocol emits. A
# printable character is typed via ``_CHAR_KEYCODES`` above, so only
# non-printable keys need a code here.
_MAC_KEYCODES = {
    "enter": 0x24,
    "return": 0x24,
    "tab": 0x30,
    "escape": 0x35,
    "esc": 0x35,
    "backspace": 0x33,
    "delete": 0x75,  # forward delete
    "space": 0x31,
    "home": 0x73,
    "end": 0x77,
    "pageup": 0x74,
    "pagedown": 0x79,
    "up": 0x7E,
    "down": 0x7D,
    "left": 0x7B,
    "right": 0x7C,
    # single letters used inside chords (e.g. Ctrl+A select-all)
    "a": 0x00,
    "c": 0x08,
    "v": 0x09,
    "x": 0x07,
    "z": 0x06,
}

# Modifier tokens -> a symbolic flag name resolved against Quartz at call time
# (kept as strings so this module imports without Quartz, for CI). On a Windows
# GUEST the app expects Ctrl-based shortcuts, so ``meta``/``ctrl``/``controlormeta``
# all map to the CONTROL flag — a Ctrl+A the guest reads as select-all — matching
# WindowsBackend's ``controlormeta -> ctrl`` decision.
_MODIFIER_FLAGS = {
    "ctrl": "control",
    "control": "control",
    "controlormeta": "control",
    "meta": "control",
    "cmd": "control",
    "command": "control",
    "win": "control",
    "alt": "alternate",
    "option": "alternate",
    "shift": "shift",
}

_NAMED_KEY_ALIASES = {
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}


def _png_size(png: bytes) -> tuple[int, int]:
    """Return ``(width, height)`` from a PNG's IHDR chunk."""
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG frame")
    w, h = struct.unpack(">II", png[16:24])
    return int(w), int(h)


def _split_chord(key: str) -> tuple[list[str], str]:
    """Split ``'ControlOrMeta+a'`` into (modifier tokens, final key token).

    Returns the modifiers (canonicalized flag names) and the final key token
    (a named key or a single character), each lower-cased. Raises ValueError on
    an empty chord.
    """
    parts = [p for p in key.split("+") if p]
    if not parts:
        raise ValueError(f"empty key chord: {key!r}")
    mods: list[str] = []
    for p in parts[:-1]:
        flag = _MODIFIER_FLAGS.get(p.lower())
        if flag is None:
            raise ValueError(f"unknown modifier in chord {key!r}: {p!r}")
        mods.append(flag)
    final = parts[-1]
    final = _NAMED_KEY_ALIASES.get(final.lower(), final)
    return mods, final


@runtime_checkable
class WindowClient(Protocol):
    """Minimal host-OS window client the backend drives (seam for tests).

    Deliberately tiny and honest: locate a window, capture its pixels, and
    inject OS-level input at screen points. The real macOS implementation is
    :class:`MacWindowClient`; tests pass a fake. Coordinates passed to the input
    methods are **screen points** (top-left origin).
    """

    def input_trusted(self) -> bool:
        """True iff this process may inject OS input (Accessibility granted)."""
        ...

    def frontmost_pid(self) -> Optional[int]:
        """PID of the frontmost application, or None if unknown."""
        ...

    def find_window(
        self, owner_substr: str, title_substr: Optional[str]
    ) -> Optional[WindowInfo]:
        """Return the front-most on-screen window matching owner/title, or None."""
        ...

    def capture(self, window_id: int) -> tuple[bytes, int, int]:
        """Capture window ``window_id``; return ``(png_bytes, px_w, px_h)``."""
        ...

    def activate(self, pid: int) -> None:
        """Un-hide and bring the app owning ``pid`` frontmost (route keystrokes)."""
        ...

    def mouse(
        self, x: float, y: float, *, button: str, down: bool, click_count: int
    ) -> None:
        """Post a mouse button transition at screen point (x, y)."""
        ...

    def mouse_move(self, x: float, y: float) -> None:
        """Post a mouse-moved event to screen point (x, y)."""
        ...

    def type_chars(self, text: str) -> None:
        """Type ``text`` into the focused window via hardware-like key codes."""
        ...

    def key(self, keycode: int, *, down: bool, flags: list[str]) -> None:
        """Post a key transition for ``keycode`` with modifier ``flags``."""
        ...

    def scroll(self, dx: int, dy: int) -> None:
        """Post a wheel gesture (line units; Backend sign convention)."""
        ...


class RemoteDisplayBackend:
    """`Backend` over a remote-display CLIENT WINDOW (pixels in, OS input out).

    Args:
        client: The host-OS window client (real :class:`MacWindowClient` or a
            fake). Defaults to a live :class:`MacWindowClient`.
        owner_substr: Case-insensitive substring of the window's owner app
            (default ``"Parallels"`` — the VM window; set ``"Citrix"`` /
            ``"Workspace"`` for a real ICA session).
        title_substr: Optional case-insensitive substring of the window title,
            to disambiguate multiple windows of the same owner.
        require_input_trust: When True (default) every input method raises if the
            process is not Accessibility-trusted, so a silently-dropped click can
            never masquerade as a completed action.
        activate_before_input: When True (default) raise the target app frontmost
            before each input burst so keystrokes route to it.
        settle_s: Seconds to pause after activating / between click edges.
    """

    def __init__(
        self,
        client: Optional[WindowClient] = None,
        *,
        owner_substr: str = "Parallels",
        title_substr: Optional[str] = None,
        require_input_trust: bool = True,
        activate_before_input: bool = True,
        settle_s: float = 0.03,
    ) -> None:
        self._client = client if client is not None else MacWindowClient()
        self._owner_substr = owner_substr
        self._title_substr = title_substr
        self._require_input_trust = require_input_trust
        self._activate_before_input = activate_before_input
        self._settle_s = settle_s
        self._window: Optional[WindowInfo] = None
        self._viewport: Optional[tuple[int, int]] = None
        self._scale: float = 1.0

    # -- window resolution ---------------------------------------------------

    def _resolve_window(self, *, refresh: bool = False) -> WindowInfo:
        """Locate (and cache) the target client window.

        Raises:
            RemoteDisplayError: If no matching window is on screen.
        """
        if self._window is not None and not refresh:
            return self._window
        win = self._client.find_window(self._owner_substr, self._title_substr)
        if win is None:
            raise RemoteDisplayError(
                "no on-screen window matching owner "
                f"{self._owner_substr!r} title {self._title_substr!r}; "
                "is the remote-display client window open and visible?"
            )
        self._window = win
        return win

    def ensure_foreground(self, *, retries: int = 10, settle_s: float = 0.4) -> None:
        """Un-hide + activate the client window and wait until it is on screen.

        A remote-display client window can be Cmd+H-hidden or de-fronted between
        actions; a hidden window captures nothing. Call this once before driving
        (and the harness may re-call it). Requires input trust only insofar as
        activation is a UI operation; it never injects input.

        Raises:
            RemoteDisplayError: If the window never comes on screen.
        """
        win = self._resolve_window(refresh=True)
        for _ in range(max(1, retries)):
            self._client.activate(win.pid)
            time.sleep(settle_s)
            win = self._resolve_window(refresh=True)
            # on_screen alone is insufficient: a window occluded by another app
            # is still "on screen", yet clicks would hit the occluder. Require
            # the client app to be FRONTMOST (unoccluded) too.
            if win.on_screen and self._client.frontmost_pid() == win.pid:
                return
        raise RemoteDisplayError(
            f"client window {self._owner_substr!r}/{self._title_substr!r} "
            "did not come to the foreground (frontmost check failed)"
        )

    # -- Backend protocol ----------------------------------------------------

    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the captured client-window frame, in pixels."""
        if self._viewport is None:
            self.screenshot()
        assert self._viewport is not None
        return self._viewport

    def screenshot(self) -> bytes:
        """Return the client window's current pixels as PNG bytes.

        Captures ONLY the target window (``CGWindowListCreateImage`` by id), so
        the frame is exactly the remote display — the coordinate space
        :meth:`click` maps into. Also refreshes the cached scale (captured
        pixels per screen point) and viewport so they can never disagree with
        the frame just returned.

        Raises:
            RemoteDisplayError: If the window is gone or capture returns nothing.
        """
        win = self._resolve_window()
        try:
            png, px_w, px_h = self._client.capture(win.window_id)
        except RemoteDisplayError:
            # The window id may be stale (window reopened); re-resolve once.
            win = self._resolve_window(refresh=True)
            png, px_w, px_h = self._client.capture(win.window_id)
        if not png or px_w <= 0 or px_h <= 0:
            raise RemoteDisplayError("client-window capture returned no pixels")
        # Validate and record the frame's true pixel size.
        w, h = _png_size(png)
        self._viewport = (w, h)
        bounds_w = win.bounds[2] or float(w)
        self._scale = (w / bounds_w) if bounds_w else 1.0
        return png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click (or double-click) at captured-pixel coordinates (x, y)."""
        sx, sy = self._to_screen(x, y)
        self._ensure_input_ready()
        self._client.mouse_move(sx, sy)
        time.sleep(self._settle_s)
        counts = 2 if double else 1
        for i in range(counts):
            self._client.mouse(sx, sy, button="left", down=True, click_count=i + 1)
            time.sleep(self._settle_s)
            self._client.mouse(sx, sy, button="left", down=False, click_count=i + 1)
            time.sleep(self._settle_s)

    def type_text(self, text: str) -> None:
        """Type ``text`` into the focused control (hardware-like key codes)."""
        if not text:
            return
        self._ensure_input_ready()
        self._client.type_chars(text)

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. ``'Enter'`` or ``'ControlOrMeta+a'``.

        Modifiers are pressed (flags applied to the key events), the final key
        is pressed and released, then modifiers are released. Every key-up is
        best-effort in a ``finally`` so a failure can never leave a modifier
        latched (a stuck Ctrl silently corrupts the next input — a wrong action).
        """
        mods, final = _split_chord(key)
        self._ensure_input_ready()
        # A bare printable key with no modifiers: type it as a character.
        if len(final) == 1 and not mods:
            self._client.type_chars(final)
            return
        # Named key, or a modified key: resolve a key code (named table first,
        # then the printable-character table so chords like Ctrl+A work).
        code = _MAC_KEYCODES.get(final.lower())
        shift = False
        if code is None:
            char = _CHAR_KEYCODES.get(final)
            if char is not None:
                code, shift = char
        if code is None:
            raise RemoteDisplayError(f"no key mapping for {final!r} in {key!r}")
        flags = list(mods) + (["shift"] if shift else [])
        try:
            self._client.key(code, down=True, flags=flags)
        finally:
            self._client.key(code, down=False, flags=flags)

    def scroll(self, dx: int, dy: int) -> None:
        """Dispatch a wheel gesture by ``(dx, dy)`` pixels."""
        if dx == 0 and dy == 0:
            return
        self._ensure_input_ready()
        self._client.scroll(int(dx), int(dy))

    # -- internals -----------------------------------------------------------

    def _ensure_input_ready(self) -> None:
        """Fail LOUD if input can't actually be delivered; else focus the app.

        A silently-dropped synthetic event (Accessibility not granted) would let
        a no-op look like a completed click/keystroke — the silent wrong action
        this project refuses. So refuse to act at all when untrusted.
        """
        if self._require_input_trust and not self._client.input_trusted():
            raise RemoteDisplayError(
                "process is not Accessibility-trusted, so OS input would be "
                "silently dropped; grant Accessibility to the driving app "
                "(System Settings > Privacy & Security > Accessibility) before "
                "driving a remote-display window. Refusing to emit an input that "
                "cannot be delivered (a dropped click must never look like "
                "success)."
            )
        if self._activate_before_input:
            win = self._resolve_window()
            self._client.activate(win.pid)
            time.sleep(self._settle_s)

    def _to_screen(self, px: int, py: int) -> tuple[float, float]:
        """Map a captured-pixel point to a screen point (for CGEvent)."""
        win = self._resolve_window()
        if self._viewport is None:
            self.screenshot()
            win = self._resolve_window()
        scale = self._scale or 1.0
        ox, oy = win.bounds[0], win.bounds[1]
        return (ox + px / scale, oy + py / scale)


# =============================================================================
# Real macOS client (Quartz / AppKit). Imported lazily so this module loads on
# any platform / in CI without pyobjc.
# =============================================================================


class MacWindowClient:
    """Live :class:`WindowClient` over Quartz (capture/input) + AppKit (activate).

    Every macOS binding is imported inside the method that uses it, so importing
    this class costs nothing and CI (Linux) can import the module to unit-test
    :class:`RemoteDisplayBackend` against a fake client.
    """

    def input_trusted(self) -> bool:
        try:
            from ApplicationServices import AXIsProcessTrusted

            return bool(AXIsProcessTrusted())
        except Exception:  # noqa: BLE001 - absence == untrusted
            return False

    def find_window(
        self, owner_substr: str, title_substr: Optional[str]
    ) -> Optional[WindowInfo]:
        import Quartz

        owner_l = owner_substr.lower()
        title_l = title_substr.lower() if title_substr else None
        # kCGWindowListOptionAll (not OnScreenOnly): a remote-display client is
        # sometimes momentarily HIDDEN (Cmd+H / focus loss) — we still need its
        # pid to un-hide + foreground it, and CGWindowListCreateImage can capture
        # a de-fronted window. on_screen is recorded so a caller can foreground.
        opts = Quartz.kCGWindowListOptionAll
        wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
        best: Optional[WindowInfo] = None
        best_area = -1.0
        for w in wins or []:
            owner = str(w.get("kCGWindowOwnerName", "") or "")
            name = str(w.get("kCGWindowName", "") or "")
            if owner_l not in owner.lower():
                continue
            if title_l is not None and title_l not in name.lower():
                continue
            if int(w.get("kCGWindowLayer", 0) or 0) != 0:
                continue  # skip menubar/overlay layers; the app window is layer 0
            b = w.get("kCGWindowBounds", {}) or {}
            bounds = (
                float(b.get("X", 0.0)),
                float(b.get("Y", 0.0)),
                float(b.get("Width", 0.0)),
                float(b.get("Height", 0.0)),
            )
            area = bounds[2] * bounds[3]
            if area > best_area:
                best_area = area
                best = WindowInfo(
                    window_id=int(w.get("kCGWindowNumber", 0) or 0),
                    owner=owner,
                    title=name,
                    pid=int(w.get("kCGWindowOwnerPID", 0) or 0),
                    bounds=bounds,
                    on_screen=bool(w.get("kCGWindowIsOnscreen", False)),
                )
        return best

    def capture(self, window_id: int) -> tuple[bytes, int, int]:
        import Quartz
        from PIL import Image

        img_ref = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionIncludingWindow,
            window_id,
            Quartz.kCGWindowImageBoundsIgnoreFraming,
        )
        if img_ref is None:
            raise RemoteDisplayError(
                f"CGWindowListCreateImage returned None for {window_id}"
            )
        w = int(Quartz.CGImageGetWidth(img_ref))
        h = int(Quartz.CGImageGetHeight(img_ref))
        if w <= 0 or h <= 0:
            raise RemoteDisplayError("captured image has zero size")
        bpr = int(Quartz.CGImageGetBytesPerRow(img_ref))
        provider = Quartz.CGImageGetDataProvider(img_ref)
        data = Quartz.CGDataProviderCopyData(provider)
        buf = bytes(data)
        # CGImage from the window server is BGRA, premultiplied; PIL reads BGRA
        # with a row stride and we drop alpha for a stable RGB PNG.
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", bpr, 1)
        out = io.BytesIO()
        img.convert("RGB").save(out, format="PNG")
        return out.getvalue(), w, h

    def frontmost_pid(self) -> Optional[int]:
        try:
            from AppKit import NSWorkspace

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(app.processIdentifier()) if app is not None else None
        except Exception:  # noqa: BLE001
            return None

    def activate(self, pid: int) -> None:
        # 1) fast path: un-hide + activate via the running-application API.
        try:
            from AppKit import (
                NSApplicationActivateIgnoringOtherApps,
                NSRunningApplication,
            )

            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is not None:
                if app.isHidden():
                    app.unhide()
                app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        except Exception:  # noqa: BLE001 - activation is best-effort focus
            pass
        # 2) forceful path: a non-GUI (accessory) process cannot reliably steal
        # activation from the current frontmost app via the API above, so also
        # ask System Events to raise the process. Crucial for input: window-buffer
        # CAPTURE works through occlusion, but a coordinate CLICK lands on whatever
        # is VISUALLY topmost — the client window MUST be truly frontmost or clicks
        # hit the wrong app. (Requires Accessibility, which input already needs.)
        if self.frontmost_pid() == pid:
            return
        try:
            import subprocess

            subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to set frontmost of '
                    f"(first process whose unix id is {int(pid)}) to true",
                ],
                capture_output=True,
                timeout=5,
            )
        except Exception:  # noqa: BLE001 - best-effort
            pass

    # -- input injection (screen points) ------------------------------------

    def _flags_mask(self, flags: list[str]) -> int:
        import Quartz

        mask = 0
        table = {
            "control": Quartz.kCGEventFlagMaskControl,
            "command": Quartz.kCGEventFlagMaskCommand,
            "alternate": Quartz.kCGEventFlagMaskAlternate,
            "shift": Quartz.kCGEventFlagMaskShift,
        }
        for f in flags:
            mask |= int(table.get(f, 0))
        return mask

    def _post(self, event: object) -> None:
        import Quartz

        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def mouse(
        self, x: float, y: float, *, button: str, down: bool, click_count: int
    ) -> None:
        import Quartz

        kind = {
            ("left", True): Quartz.kCGEventLeftMouseDown,
            ("left", False): Quartz.kCGEventLeftMouseUp,
            ("right", True): Quartz.kCGEventRightMouseDown,
            ("right", False): Quartz.kCGEventRightMouseUp,
        }[(button, down)]
        btn = (
            Quartz.kCGMouseButtonLeft
            if button == "left"
            else Quartz.kCGMouseButtonRight
        )
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), btn)
        if click_count > 1:
            Quartz.CGEventSetIntegerValueField(
                ev, Quartz.kCGMouseEventClickState, int(click_count)
            )
        self._post(ev)

    def mouse_move(self, x: float, y: float) -> None:
        import Quartz

        ev = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft
        )
        self._post(ev)

    def type_chars(self, text: str) -> None:
        import Quartz

        for ch in text:
            mapping = _CHAR_KEYCODES.get(ch)
            if mapping is not None:
                keycode, shift = mapping
                flags = ["shift"] if shift else []
                self.key(keycode, down=True, flags=flags)
                self.key(keycode, down=False, flags=flags)
            else:
                # Fallback for a character with no US-layout code: a synthetic
                # Unicode keystroke. NOTE: a remote-display guest may drop this
                # (it forwards scancodes) — hence the keycode path is primary.
                for down in (True, False):
                    ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                    Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                    self._post(ev)
            time.sleep(0.006)

    def key(self, keycode: int, *, down: bool, flags: list[str]) -> None:
        import Quartz

        ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        mask = self._flags_mask(flags)
        if mask:
            Quartz.CGEventSetFlags(ev, mask)
        self._post(ev)

    def scroll(self, dx: int, dy: int) -> None:
        import Quartz

        # Backend sign convention: positive dy scrolls content up / view down.
        # CGEvent wheel positive scrolls content down / view up, so negate dy.
        lines_v = -_lines(dy)
        lines_h = _lines(dx)
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 2, lines_v, lines_h
        )
        self._post(ev)


def _lines(pixels: int) -> int:
    """Convert a pixel delta to wheel lines (~40 px per line; >=1 when nonzero)."""
    if pixels == 0:
        return 0
    lines = round(abs(pixels) / 40)
    return max(1, lines) * (1 if pixels > 0 else -1)

"""Windows-host :class:`WindowClient` for the remote-display pixel backend.

This is the win32 counterpart of :class:`MacWindowClient`
(:mod:`openadapt_flow.backends.remote_display`): it lets a **Windows host**
replay into one specific client window's own pixel space — the
Citrix-Workspace / Parallels / Microsoft-Remote-Desktop client window on the
operator's machine. Same seam, same contract, same fail-LOUD discipline; only
the OS bindings differ. A dental clinic on Windows endpoints closes the local
Citrix pixel loop through this class.

Coordinate semantics (kept in exact parity with ``RemoteDisplayBackend`` and
openadapt-capture's window-scoped recording — read both before changing):

* ``WindowInfo.bounds`` is the **client area** rectangle ``(x, y, w, h)`` in
  screen coordinates (top-left origin) — ``GetClientRect`` mapped through
  ``ClientToScreen``. The client area (no title bar, no resize border) is the
  surface that paints the remote session's pixels, so captured pixel (0, 0)
  is exactly screen point ``bounds`` origin.
* :meth:`Win32WindowClient.capture` returns the client area's own pixels
  (``PrintWindow`` with ``PW_CLIENTONLY``; ``BitBlt`` of the client DC as the
  fallback). Because this process is made **per-monitor-DPI-aware (v2)**
  before any window call, ``GetClientRect``, screen coordinates, and
  ``SendInput`` all speak *physical pixels* — so the backend's derived
  ``scale`` is 1.0 by construction and a captured pixel maps to a screen
  point as ``bounds_origin + pixel`` (the same ``origin + pixel/scale``
  formula the macOS client satisfies with scale 2.0 on Retina).
* If per-monitor DPI awareness cannot be established the client REFUSES to
  capture or inject (:class:`DpiAwarenessError`): under DPI virtualization
  the OS lies to us about geometry, and a click computed in virtualized
  coordinates can land on the wrong control — a silent wrong action.

Window identification (capture 0.6.0 parity, tightened to flow's exact-match
discipline): ``owner`` is matched against the owning process's **executable
basename** (e.g. ``wfica32.exe`` for Citrix Workspace, ``prl_client_app`` for
Parallels Client) case-insensitively and tolerant of the ``.exe`` suffix;
``title`` (optional) must match the window title exactly, case-insensitively.
The window class name is recorded and an ``expected_class`` filter is
available for targets whose titles are dynamic (e.g. Citrix's
``Transparent Windows Client``). Zero or multiple matches are surfaced to the
backend, whose ambiguity gate fails closed — this client never picks "the
largest match".

Honest failure modes (all subclasses of ``RemoteDisplayError``, so they map
onto the existing halt semantics):

* window destroyed / no longer a window        -> :class:`WindowGoneError`
* window minimized (client area not painted)   -> :class:`WindowMinimizedError`
* PrintWindow AND BitBlt both fail             -> :class:`CaptureFailedError`
* ``SendInput`` injected fewer events than
  requested (blocked / desktop locked)         -> :class:`InputDeliveryError`
* per-monitor DPI awareness unavailable        -> :class:`DpiAwarenessError`

UIPI honesty: Windows has no macOS-style Accessibility grant, but User
Interface Privilege Isolation silently discards synthetic input sent to a
higher-integrity (elevated) process — the exact "dropped click that looks
like success" this project refuses. :meth:`input_trusted` therefore returns
False when the most recently resolved target window belongs to a process more
elevated than ours (or whose elevation cannot be determined), and every input
method independently refuses to emit into a known-elevated target. Delivery
of each burst is additionally checked via ``SendInput``'s injected-event
count, and the backend's foreground-identity proof runs immediately before
and after every input burst.

All Win32 calls go through the :class:`Win32Api` seam so the selection
matching, coordinate mapping, focus-halt, and capture-fallback logic are unit
tested with a fake API on any platform; :class:`NativeWin32Api` (ctypes,
stdlib only — this repo deliberately avoids pywin32) binds the real functions
and is constructible only on Windows. CI on non-Windows imports this module
freely (nothing at module scope touches ``ctypes.windll``).

Status honesty: this client is **mock-contract-tested only**. No counted
qualification batch against a real Windows host / real client window has been
published; see ``claims.yaml`` (``win32-window-replay-roadmap``) and
``docs/desktop/CITRIX_PIXEL.md`` for the evidence ladder.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

from openadapt_flow.backends.remote_display import (
    RemoteDisplayError,
    WindowInfo,
    _lines,
)

if TYPE_CHECKING:  # pragma: no cover
    from PIL import Image


# -- typed failure modes (all halt the same way RemoteDisplayError does) ------


class Win32WindowError(RemoteDisplayError):
    """Base class for win32 window-client failures (halts like the backend)."""


class WindowGoneError(Win32WindowError):
    """The target HWND no longer identifies a window (closed / recreated)."""


class WindowMinimizedError(Win32WindowError):
    """The target window is minimized; its client area is not being painted."""


class CaptureFailedError(Win32WindowError):
    """Both PrintWindow and BitBlt failed to produce the client-area pixels."""


class InputDeliveryError(Win32WindowError):
    """SendInput did not inject every requested event (a dropped input must
    never look like a completed action)."""


class DpiAwarenessError(Win32WindowError):
    """Per-monitor DPI awareness could not be established; coordinates would
    be virtualized and clicks could land on the wrong control."""


# -- key codes ----------------------------------------------------------------

# Windows virtual-key codes for the named keys the Backend protocol emits.
# Mirrors the macOS ``_MAC_KEYCODES`` table in remote_display.py, including
# the single letters used inside chords (Ctrl+A select-all etc.) so a
# lower-case chord letter resolves unshifted exactly as it does on macOS.
_WIN_VKS: dict[str, int] = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "space": 0x20,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    # single letters used inside chords (VK codes equal ASCII uppercase)
    "a": 0x41,
    "c": 0x43,
    "v": 0x56,
    "x": 0x58,
    "z": 0x5A,
}

# Modifier flag names (the backend's canonical tokens) -> modifier VKs.
_MODIFIER_VKS: dict[str, int] = {
    "control": 0x11,  # VK_CONTROL
    "alternate": 0x12,  # VK_MENU
    "shift": 0x10,  # VK_SHIFT
    "command": 0x11,  # a Windows guest reads Ctrl where macOS reads Cmd
}

# VKs that require KEYEVENTF_EXTENDEDKEY so the guest/remote client receives
# the extended scancode (navigation cluster, not the numpad aliases).
_EXTENDED_VKS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E})

_WHEEL_DELTA = 120


def normalize_to_virtual_desktop(
    x: float, y: float, virtual: tuple[int, int, int, int]
) -> tuple[int, int]:
    """Map a physical screen point to SendInput's 0..65535 absolute space.

    ``virtual`` is the virtual desktop ``(left, top, width, height)`` in
    physical pixels (``SM_XVIRTUALSCREEN`` family). Windows maps normalized
    coordinate N to pixel ``left + round(N * (width - 1) / 65535)``, so the
    inverse here uses ``width - 1`` — an off-by-one that shifts clicks a
    pixel on small screens if dropped.
    """
    left, top, width, height = virtual
    if width <= 1 or height <= 1:
        raise InputDeliveryError(f"virtual desktop metrics are degenerate: {virtual!r}")
    nx = round((x - left) * 65535 / (width - 1))
    ny = round((y - top) * 65535 / (height - 1))
    if not (0 <= nx <= 65535 and 0 <= ny <= 65535):
        raise InputDeliveryError(
            f"screen point {(x, y)!r} is outside the virtual desktop "
            f"{virtual!r}; refusing to clamp a click onto a different pixel"
        )
    return int(nx), int(ny)


def owner_matches_process(owner: str, image_basename: str) -> bool:
    """Exact case-insensitive owner match, tolerant of the ``.exe`` suffix.

    capture 0.6.0 matches ``owner`` against the owning process's executable
    name (``wfica32`` for Citrix Workspace); replay keeps that convention but
    refuses substring matching — ``"wfica32"`` == ``"wfica32.exe"`` while
    ``"fica"`` matches nothing.
    """
    if not image_basename:
        return False
    a = owner.casefold()
    b = image_basename.casefold()
    if a == b:
        return True
    if b.endswith(".exe") and a == b[: -len(".exe")]:
        return True
    if a.endswith(".exe") and b == a[: -len(".exe")]:
        return True
    return False


@runtime_checkable
class Win32Api(Protocol):
    """The raw Win32 surface the client drives (seam for offline tests).

    Every method is a thin, policy-free binding; ALL selection, mapping, and
    refusal logic lives in :class:`Win32WindowClient` where it is unit-tested
    against a fake implementation of this protocol.
    """

    def ensure_dpi_awareness(self) -> str:
        """Make this process DPI-aware; return the achieved level.

        One of ``"per-monitor-v2"``, ``"per-monitor"``, ``"system"``,
        ``"unaware"``. Must be idempotent.
        """
        ...

    def enum_top_level_windows(self) -> list[int]:
        """All top-level HWNDs in z-order (front to back)."""
        ...

    def is_window(self, hwnd: int) -> bool: ...

    def is_window_visible(self, hwnd: int) -> bool: ...

    def is_iconic(self, hwnd: int) -> bool: ...

    def is_cloaked(self, hwnd: int) -> bool:
        """DWM-cloaked (e.g. a suspended UWP ghost window)."""
        ...

    def window_title(self, hwnd: int) -> str: ...

    def window_class(self, hwnd: int) -> str: ...

    def window_pid(self, hwnd: int) -> int:
        """Owning process id, or 0 when unknown."""
        ...

    def process_image_basename(self, pid: int) -> str:
        """Executable basename (e.g. ``wfica32.exe``), or ``""`` unknown."""
        ...

    def client_bounds(self, hwnd: int) -> Optional[tuple[float, float, float, float]]:
        """Client-area rect ``(x, y, w, h)`` in physical screen px, or None."""
        ...

    def foreground_window(self) -> Optional[int]:
        """Root HWND of the foreground (keyboard-focus) window, or None."""
        ...

    def root_window_at_point(self, x: float, y: float) -> Optional[int]:
        """Root HWND of the topmost visible window at a screen point."""
        ...

    def restore(self, hwnd: int) -> None:
        """ShowWindow(SW_RESTORE) — un-minimize."""
        ...

    def force_foreground(self, hwnd: int) -> None:
        """Best-effort foregrounding (AttachThreadInput dance); caller
        verifies the result via :meth:`foreground_window`."""
        ...

    def print_window(self, hwnd: int, size: tuple[int, int]) -> Optional["Image.Image"]:
        """PrintWindow(PW_CLIENTONLY | PW_RENDERFULLCONTENT), or None."""
        ...

    def blt_window(self, hwnd: int, size: tuple[int, int]) -> Optional["Image.Image"]:
        """BitBlt of the client DC (on-screen pixels only), or None."""
        ...

    def send_mouse_button(self, x: float, y: float, button: str, down: bool) -> None:
        """Inject one absolute-position button transition (raises
        :class:`InputDeliveryError` if not fully injected)."""
        ...

    def send_mouse_move(self, x: float, y: float) -> None: ...

    def send_key_vk(self, vk: int, down: bool) -> None:
        """Inject one VK transition with its hardware scancode (extended flag
        where required)."""
        ...

    def send_unicode_char(self, ch: str, down: bool) -> None:
        """KEYEVENTF_UNICODE fallback (a remote-display client may drop it)."""
        ...

    def send_wheel(self, delta: int, horizontal: bool) -> None: ...

    def vk_for_char(self, ch: str) -> Optional[tuple[int, bool]]:
        """(vk, needs_shift) for the active layout, or None when the char is
        unmapped or needs AltGr (then the Unicode path is used)."""
        ...

    def self_elevated(self) -> Optional[bool]:
        """Whether THIS process runs elevated (None = unknown)."""
        ...

    def process_elevated(self, pid: int) -> Optional[bool]:
        """Whether ``pid`` runs elevated (None = unknown -> fail closed)."""
        ...


class Win32WindowClient:
    """Live :class:`WindowClient` over Win32 (capture + SendInput injection).

    Args:
        api: The raw Win32 seam. Defaults to :class:`NativeWin32Api` (real
            ctypes bindings; Windows only). Tests pass a fake.
        expected_class: Optional exact case-insensitive window CLASS filter,
            for targets whose titles are dynamic (Citrix Workspace windows
            carry stable classes such as ``Transparent Windows Client``).
        char_delay_s: Pause between typed characters (parity with the macOS
            client's pacing; a remote display needs time to forward each
            scancode).
    """

    def __init__(
        self,
        api: Optional[Win32Api] = None,
        *,
        expected_class: Optional[str] = None,
        char_delay_s: float = 0.006,
    ) -> None:
        self._api: Win32Api = api if api is not None else NativeWin32Api()
        self._expected_class = expected_class
        self._char_delay_s = char_delay_s
        self._dpi_level: Optional[str] = None
        # Most recent exact-match resolution: pid -> hwnd (activation target)
        # and the pid set (UIPI elevation guard).
        self._activation_hints: dict[int, int] = {}
        self._last_match_pids: frozenset[int] = frozenset()

    # -- DPI ------------------------------------------------------------------

    def _require_dpi(self) -> None:
        """Establish (once) and require per-monitor DPI awareness."""
        if self._dpi_level is None:
            self._dpi_level = self._api.ensure_dpi_awareness()
        if self._dpi_level not in ("per-monitor-v2", "per-monitor"):
            raise DpiAwarenessError(
                "process could not become per-monitor DPI aware (achieved "
                f"{self._dpi_level!r}); under DPI virtualization window "
                "geometry is scaled behind our back and a mapped click can "
                "land on the wrong control. Refusing capture/input rather "
                "than risking a silent wrong action."
            )

    # -- WindowClient: resolution --------------------------------------------

    def find_windows(self, owner: str, title: Optional[str]) -> list[WindowInfo]:
        """Every exact owner(+title)(+class) match among real top-level windows.

        ``owner`` matches the owning process executable basename (capture
        0.6.0 convention), exact case-insensitive, ``.exe`` optional. Cloaked
        (DWM ghost) windows are skipped so a suspended UWP duplicate can never
        manufacture a false ambiguity halt. Minimized windows are RETURNED
        (with ``on_screen=False``) so :meth:`activate` can restore them; the
        backend's on-screen/foreground proofs still gate every input.
        """
        self._require_dpi()
        title_l = title.casefold() if title is not None else None
        class_l = (
            self._expected_class.casefold()
            if self._expected_class is not None
            else None
        )
        matches: list[WindowInfo] = []
        hints: dict[int, int] = {}
        for hwnd in self._api.enum_top_level_windows():
            if not self._api.is_window_visible(hwnd):
                continue
            if self._api.is_cloaked(hwnd):
                continue
            win_title = self._api.window_title(hwnd)
            if not win_title:
                # Unnamed tool/host windows (capture 0.6.0 skips these too).
                continue
            if title_l is not None and win_title.casefold() != title_l:
                continue
            if class_l is not None:
                if self._api.window_class(hwnd).casefold() != class_l:
                    continue
            pid = self._api.window_pid(hwnd)
            if pid <= 0:
                continue
            image = self._api.process_image_basename(pid)
            if not owner_matches_process(owner, image):
                continue
            bounds = self._api.client_bounds(hwnd)
            if bounds is None:
                continue
            iconic = self._api.is_iconic(hwnd)
            if not iconic and (bounds[2] <= 0 or bounds[3] <= 0):
                continue
            matches.append(
                WindowInfo(
                    window_id=int(hwnd),
                    owner=image,
                    title=win_title,
                    pid=int(pid),
                    bounds=bounds,
                    on_screen=not iconic,
                )
            )
            hints[int(pid)] = int(hwnd)
        # Remember the resolution for activation + the UIPI elevation guard.
        # Only unambiguous pid->hwnd pairs may steer activation.
        pid_counts: dict[int, int] = {}
        for w in matches:
            pid_counts[w.pid] = pid_counts.get(w.pid, 0) + 1
        self._activation_hints = {
            pid: hwnd for pid, hwnd in hints.items() if pid_counts.get(pid) == 1
        }
        self._last_match_pids = frozenset(w.pid for w in matches)
        return matches

    # -- WindowClient: trust / focus -----------------------------------------

    def input_trusted(self) -> bool:
        """False when injection into the resolved target would be UIPI-dropped.

        Windows has no Accessibility consent, but UIPI silently discards
        synthetic input aimed at a more-elevated process. Evaluated against
        the most recent :meth:`find_windows` resolution; unknown elevation
        fails closed. With no resolution yet there is nothing to distrust —
        the input methods re-assert against the resolved target anyway.
        """
        if self._api.self_elevated() is True:
            return True
        for pid in self._last_match_pids:
            if self._api.process_elevated(pid) is not False:
                return False
        return True

    def _assert_injectable(self) -> None:
        """Refuse to emit input that UIPI would silently discard."""
        if not self.input_trusted():
            raise InputDeliveryError(
                "target window belongs to an elevated (or unknown-elevation) "
                "process; UIPI would silently discard synthetic input from "
                "this process. Run the driver elevated to drive an elevated "
                "target — a dropped input must never look like success."
            )

    def frontmost_pid(self) -> Optional[int]:
        hwnd = self._api.foreground_window()
        if hwnd is None:
            return None
        pid = self._api.window_pid(hwnd)
        return pid if pid > 0 else None

    def key_window_id(self, pid: int) -> Optional[int]:
        """The foreground (keyboard-focus) root window iff owned by ``pid``.

        On Windows the foreground window IS the keyboard focus target, so the
        backend's ``key_window_id(pid) == window_id`` proof requires the exact
        target HWND to hold the foreground — stricter than the macOS z-order
        proxy, and exactly the identity discipline this substrate needs.
        """
        hwnd = self._api.foreground_window()
        if hwnd is None:
            return None
        if self._api.window_pid(hwnd) != int(pid):
            return None
        return int(hwnd)

    def window_at_point(self, x: float, y: float) -> Optional[int]:
        """Root HWND of the topmost visible window at a screen point."""
        return self._api.root_window_at_point(x, y)

    def activate(self, pid: int) -> None:
        """Restore + best-effort foreground the resolved window for ``pid``.

        Uses the unambiguous hwnd remembered from the last
        :meth:`find_windows` (the backend always re-resolves immediately
        before activating). Best-effort by contract: the backend's
        foreground/key-window proof decides, and refuses input on failure.
        """
        hwnd = self._activation_hints.get(int(pid))
        if hwnd is None or not self._api.is_window(hwnd):
            return
        if self._api.is_iconic(hwnd):
            self._api.restore(hwnd)
        self._api.force_foreground(hwnd)

    # -- WindowClient: capture -----------------------------------------------

    def capture(self, window_id: int) -> tuple[bytes, int, int]:
        """Client-area pixels of ``window_id`` as ``(png, px_w, px_h)``.

        Fallback order: ``PrintWindow(PW_CLIENTONLY | PW_RENDERFULLCONTENT)``
        (works for occluded and DWM-composited windows) first; then a
        ``BitBlt`` of the client DC (on-screen pixels only — correct exactly
        when the backend's occlusion proof holds). Both failing is a loud
        typed error, never an empty frame.
        """
        import io

        self._require_dpi()
        hwnd = int(window_id)
        if not self._api.is_window(hwnd):
            raise WindowGoneError(
                f"window {hwnd} no longer exists; the client window was "
                "closed or recreated — re-resolve before capturing"
            )
        if self._api.is_iconic(hwnd):
            raise WindowMinimizedError(
                f"window {hwnd} is minimized; its client area is not being "
                "painted. Restore the client window (ensure_foreground) "
                "before capturing."
            )
        bounds = self._api.client_bounds(hwnd)
        if bounds is None or bounds[2] <= 0 or bounds[3] <= 0:
            raise CaptureFailedError(
                f"window {hwnd} has no usable client area (bounds={bounds!r})"
            )
        size = (int(bounds[2]), int(bounds[3]))
        img = self._api.print_window(hwnd, size)
        if img is None:
            img = self._api.blt_window(hwnd, size)
        if img is None:
            raise CaptureFailedError(
                f"both PrintWindow and BitBlt failed for window {hwnd}; the "
                "window may be on a secure/locked desktop or rendered by a "
                "protected surface"
            )
        if tuple(img.size) != size:
            raise CaptureFailedError(
                f"captured image size {img.size!r} disagrees with the client "
                f"rect {size!r}; refusing a frame whose geometry cannot be "
                "trusted"
            )
        out = io.BytesIO()
        img.convert("RGB").save(out, format="PNG")
        return out.getvalue(), size[0], size[1]

    # -- WindowClient: input (screen points == physical pixels) ---------------

    def mouse(
        self, x: float, y: float, *, button: str, down: bool, click_count: int
    ) -> None:
        """Post one absolute mouse button transition.

        ``click_count`` is advisory on Windows: the OS synthesizes
        double-clicks from transition timing/position, and the backend's two
        rapid down/up pairs land inside the double-click interval.
        """
        self._assert_injectable()
        self._api.send_mouse_button(x, y, button, down)

    def mouse_move(self, x: float, y: float) -> None:
        self._assert_injectable()
        self._api.send_mouse_move(x, y)

    def type_chars(self, text: str) -> None:
        """Type text via layout-resolved VKs (hardware-like scancodes).

        A remote-display client forwards scancodes, so ``VkKeyScanW``-resolved
        key events are primary; characters the layout cannot produce (or that
        need AltGr) fall back to a synthetic Unicode keystroke, which such a
        client may drop — the same documented caveat as the macOS client.
        """
        self._assert_injectable()
        for ch in text:
            mapping = self._api.vk_for_char(ch)
            if mapping is not None:
                vk, shift = mapping
                if shift:
                    self._api.send_key_vk(_MODIFIER_VKS["shift"], True)
                try:
                    self._api.send_key_vk(vk, True)
                    self._api.send_key_vk(vk, False)
                finally:
                    if shift:
                        self._api.send_key_vk(_MODIFIER_VKS["shift"], False)
            else:
                self._api.send_unicode_char(ch, True)
                self._api.send_unicode_char(ch, False)
            time.sleep(self._char_delay_s)

    def key(self, keycode: int, *, down: bool, flags: list[str]) -> None:
        """Post a VK transition with real modifier key events around it.

        macOS carries modifiers as event flags; Windows needs actual modifier
        transitions. The backend emits down-then-up with identical flags (up
        in a ``finally``), so modifiers are pressed before the key on the
        down edge and released after it on the up edge — a failure can never
        leave a modifier latched.
        """
        self._assert_injectable()
        mods = [_MODIFIER_VKS[f] for f in flags if f in _MODIFIER_VKS]
        if down:
            for m in mods:
                self._api.send_key_vk(m, True)
            self._api.send_key_vk(int(keycode), True)
        else:
            try:
                self._api.send_key_vk(int(keycode), False)
            finally:
                for m in reversed(mods):
                    self._api.send_key_vk(m, False)

    def scroll(self, dx: int, dy: int) -> None:
        """Wheel gesture in Backend sign convention (positive dy = content up).

        Windows' positive ``WHEEL_DELTA`` scrolls content down (view up), so
        vertical lines are negated — the same inversion the macOS client
        applies for CGEvent.
        """
        self._assert_injectable()
        lines_v = -_lines(int(dy))
        lines_h = _lines(int(dx))
        if lines_v:
            self._api.send_wheel(lines_v * _WHEEL_DELTA, horizontal=False)
        if lines_h:
            self._api.send_wheel(lines_h * _WHEEL_DELTA, horizontal=True)

    def resolve_key(self, token: str) -> Optional[tuple[int, bool]]:
        """(VK, needs_shift) for a named key or character token, else None.

        Named-key table first (so a lower-case chord letter like the ``a`` in
        ``Ctrl+a`` resolves unshifted, matching the macOS client), then the
        active keyboard layout via ``VkKeyScanW``.
        """
        vk = _WIN_VKS.get(token.lower())
        if vk is not None:
            return vk, False
        if len(token) == 1:
            return self._api.vk_for_char(token)
        return None


# =============================================================================
# Real Win32 API bindings (ctypes, stdlib only). Constructible on Windows only;
# nothing at module scope touches ctypes.windll, so any platform imports this
# module and CI unit-tests the client against a fake Win32Api.
# =============================================================================


class NativeWin32Api:
    """ctypes bindings for :class:`Win32Api` (no pywin32 dependency)."""

    # Declared for the type checker: on a non-Windows checker platform the
    # __init__ body below is unreachable (it raises first), so these would
    # otherwise have no inferable types.
    _ctypes: Any
    _wintypes: Any
    _user32: Any
    _gdi32: Any
    _kernel32: Any
    _dwmapi: Any
    _shcore: Any
    _advapi32: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise Win32WindowError(
                "NativeWin32Api requires a Windows host; on other platforms "
                "inject a Win32Api fake (tests) or use the platform's client"
            )
        import ctypes
        import ctypes.wintypes as wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32
        self._kernel32 = ctypes.windll.kernel32
        try:
            self._dwmapi = ctypes.windll.dwmapi
        except OSError:  # pragma: no cover - dwmapi ships with every DWM OS
            self._dwmapi = None
        try:
            self._shcore = ctypes.windll.shcore
        except OSError:
            self._shcore = None
        try:
            self._advapi32 = ctypes.windll.advapi32
        except OSError:  # pragma: no cover - advapi32 always present
            self._advapi32 = None

    # -- DPI ------------------------------------------------------------------

    def ensure_dpi_awareness(self) -> str:
        ctypes = self._ctypes
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == HANDLE(-4).
        set_ctx = getattr(self._user32, "SetProcessDpiAwarenessContext", None)
        get_ctx = getattr(self._user32, "GetThreadDpiAwarenessContext", None)
        eq_ctx = getattr(self._user32, "AreDpiAwarenessContextsEqual", None)
        if set_ctx is not None:
            if set_ctx(ctypes.c_void_p(-4)):
                return "per-monitor-v2"
            # Already set (possibly by the host process); read what we have.
            if get_ctx is not None and eq_ctx is not None:
                current = get_ctx()
                for handle, name in (
                    (-4, "per-monitor-v2"),
                    (-3, "per-monitor"),
                    (-2, "system"),
                ):
                    if eq_ctx(current, ctypes.c_void_p(handle)):
                        return name
        if self._shcore is not None:
            # PROCESS_PER_MONITOR_DPI_AWARE == 2; S_OK or already-set both
            # leave us at least per-monitor aware.
            E_ACCESSDENIED = -2147024891  # already set for this process
            res = self._shcore.SetProcessDpiAwareness(2)
            if res in (0, E_ACCESSDENIED):
                return "per-monitor"
        if self._user32.SetProcessDPIAware():
            return "system"
        return "unaware"

    # -- enumeration / identity ----------------------------------------------

    def enum_top_level_windows(self) -> list[int]:
        ctypes = self._ctypes
        wintypes = self._wintypes
        hwnds: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd: int, _lparam: int) -> bool:
            hwnds.append(int(hwnd))
            return True

        self._user32.EnumWindows(_cb, 0)
        return hwnds

    def is_window(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindow(self._wintypes.HWND(hwnd)))

    def is_window_visible(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowVisible(self._wintypes.HWND(hwnd)))

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._user32.IsIconic(self._wintypes.HWND(hwnd)))

    def is_cloaked(self, hwnd: int) -> bool:
        if self._dwmapi is None:  # pragma: no cover
            return False
        ctypes = self._ctypes
        DWMWA_CLOAKED = 14
        value = ctypes.wintypes.DWORD(0)
        res = self._dwmapi.DwmGetWindowAttribute(
            self._wintypes.HWND(hwnd),
            ctypes.wintypes.DWORD(DWMWA_CLOAKED),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return res == 0 and value.value != 0

    def window_title(self, hwnd: int) -> str:
        ctypes = self._ctypes
        length = self._user32.GetWindowTextLengthW(self._wintypes.HWND(hwnd))
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(self._wintypes.HWND(hwnd), buf, length + 1)
        return buf.value

    def window_class(self, hwnd: int) -> str:
        ctypes = self._ctypes
        buf = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(self._wintypes.HWND(hwnd), buf, 256)
        return buf.value

    def window_pid(self, hwnd: int) -> int:
        ctypes = self._ctypes
        pid = self._wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(
            self._wintypes.HWND(hwnd), ctypes.byref(pid)
        )
        return int(pid.value)

    def process_image_basename(self, pid: int) -> str:
        import ntpath

        ctypes = self._ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = self._kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return ""
        try:
            size = self._wintypes.DWORD(4096)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = self._kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            )
            return ntpath.basename(buf.value) if ok else ""
        finally:
            self._kernel32.CloseHandle(handle)

    def client_bounds(self, hwnd: int) -> Optional[tuple[float, float, float, float]]:
        ctypes = self._ctypes
        wintypes = self._wintypes
        rect = wintypes.RECT()
        if not self._user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        origin = wintypes.POINT(0, 0)
        if not self._user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(origin)):
            return None
        return (
            float(origin.x),
            float(origin.y),
            float(rect.right - rect.left),
            float(rect.bottom - rect.top),
        )

    # -- focus ----------------------------------------------------------------

    def foreground_window(self) -> Optional[int]:
        GA_ROOT = 2
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return None
        root = self._user32.GetAncestor(self._wintypes.HWND(hwnd), GA_ROOT)
        return int(root) if root else int(hwnd)

    def root_window_at_point(self, x: float, y: float) -> Optional[int]:
        GA_ROOT = 2
        point = self._wintypes.POINT(int(round(x)), int(round(y)))
        hwnd = self._user32.WindowFromPoint(point)
        if not hwnd:
            return None
        root = self._user32.GetAncestor(self._wintypes.HWND(hwnd), GA_ROOT)
        return int(root) if root else int(hwnd)

    def restore(self, hwnd: int) -> None:
        SW_RESTORE = 9
        self._user32.ShowWindow(self._wintypes.HWND(hwnd), SW_RESTORE)

    def force_foreground(self, hwnd: int) -> None:
        """AttachThreadInput dance; best-effort, caller verifies the result."""
        ctypes = self._ctypes
        wintypes = self._wintypes
        target_thread = self._user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
        fg = self._user32.GetForegroundWindow()
        fg_thread = (
            self._user32.GetWindowThreadProcessId(wintypes.HWND(fg), None) if fg else 0
        )
        attached = False
        try:
            if fg_thread and fg_thread != target_thread:
                attached = bool(
                    self._user32.AttachThreadInput(fg_thread, target_thread, True)
                )
            self._user32.BringWindowToTop(wintypes.HWND(hwnd))
            self._user32.SetForegroundWindow(wintypes.HWND(hwnd))
        finally:
            if attached:
                self._user32.AttachThreadInput(fg_thread, target_thread, False)
        del ctypes  # bound above for symmetry; nothing else to do

    # -- capture ---------------------------------------------------------------

    def _capture_into_dib(
        self, hwnd: int, size: tuple[int, int], use_print_window: bool
    ) -> Optional["Image.Image"]:
        import ctypes

        from PIL import Image

        w, h = size
        if w <= 0 or h <= 0:
            return None
        user32, gdi32 = self._user32, self._gdi32
        wintypes = self._wintypes

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [
                ("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", ctypes.c_uint32 * 3),
            ]

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        hdc_client = user32.GetDC(wintypes.HWND(hwnd))
        if not hdc_client:
            return None
        memdc = gdi32.CreateCompatibleDC(hdc_client)
        bits = ctypes.c_void_p()
        DIB_RGB_COLORS = 0
        hbmp = gdi32.CreateDIBSection(
            hdc_client, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        try:
            if not memdc or not hbmp or not bits:
                return None
            old = gdi32.SelectObject(memdc, hbmp)
            try:
                if use_print_window:
                    PW_CLIENTONLY = 0x1
                    PW_RENDERFULLCONTENT = 0x2
                    ok = user32.PrintWindow(
                        wintypes.HWND(hwnd), memdc, PW_CLIENTONLY | PW_RENDERFULLCONTENT
                    )
                else:
                    SRCCOPY = 0x00CC0020
                    ok = gdi32.BitBlt(memdc, 0, 0, w, h, hdc_client, 0, 0, SRCCOPY)
                if not ok:
                    return None
                buf = ctypes.string_at(bits, w * h * 4)
                return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
            finally:
                gdi32.SelectObject(memdc, old)
        finally:
            if hbmp:
                gdi32.DeleteObject(hbmp)
            if memdc:
                gdi32.DeleteDC(memdc)
            user32.ReleaseDC(wintypes.HWND(hwnd), hdc_client)

    def print_window(self, hwnd: int, size: tuple[int, int]) -> Optional["Image.Image"]:
        return self._capture_into_dib(hwnd, size, use_print_window=True)

    def blt_window(self, hwnd: int, size: tuple[int, int]) -> Optional["Image.Image"]:
        return self._capture_into_dib(hwnd, size, use_print_window=False)

    # -- input -----------------------------------------------------------------

    def _virtual_screen(self) -> tuple[int, int, int, int]:
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        return (
            int(self._user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
            int(self._user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
            int(self._user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
            int(self._user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
        )

    def _input_structs(self):
        ctypes = self._ctypes

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]

        return MOUSEINPUT, KEYBDINPUT, INPUT

    def _send(self, inputs: list) -> None:
        ctypes = self._ctypes
        n = len(inputs)
        array = (inputs[0].__class__ * n)(*inputs)
        sent = self._user32.SendInput(n, array, ctypes.sizeof(inputs[0]))
        if int(sent) != n:
            err = self._kernel32.GetLastError()
            raise InputDeliveryError(
                f"SendInput injected {int(sent)}/{n} events (GetLastError="
                f"{err}); input was blocked (locked desktop, UIPI, or "
                "another injection failure) — refusing to treat a dropped "
                "input as success"
            )

    def _mouse_input(self, x: float, y: float, flags: int, data: int = 0):
        MOUSEINPUT, _KEYBDINPUT, INPUT = self._input_structs()
        INPUT_MOUSE = 0
        MOUSEEVENTF_ABSOLUTE = 0x8000
        MOUSEEVENTF_VIRTUALDESK = 0x4000
        MOUSEEVENTF_MOVE = 0x0001
        nx, ny = normalize_to_virtual_desktop(x, y, self._virtual_screen())
        item = INPUT()
        item.type = INPUT_MOUSE
        item.union.mi = MOUSEINPUT(
            nx,
            ny,
            data,
            flags | MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            0,
            0,
        )
        return item

    def send_mouse_button(self, x: float, y: float, button: str, down: bool) -> None:
        MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
        MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
        flag = {
            ("left", True): MOUSEEVENTF_LEFTDOWN,
            ("left", False): MOUSEEVENTF_LEFTUP,
            ("right", True): MOUSEEVENTF_RIGHTDOWN,
            ("right", False): MOUSEEVENTF_RIGHTUP,
        }[(button, down)]
        self._send([self._mouse_input(x, y, flag)])

    def send_mouse_move(self, x: float, y: float) -> None:
        self._send([self._mouse_input(x, y, 0)])

    def send_wheel(self, delta: int, horizontal: bool) -> None:
        MOUSEINPUT, _KEYBDINPUT, INPUT = self._input_structs()
        INPUT_MOUSE = 0
        MOUSEEVENTF_WHEEL = 0x0800
        MOUSEEVENTF_HWHEEL = 0x1000
        item = INPUT()
        item.type = INPUT_MOUSE
        item.union.mi = MOUSEINPUT(
            0,
            0,
            delta & 0xFFFFFFFF,
            MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL,
            0,
            0,
        )
        self._send([item])

    def send_key_vk(self, vk: int, down: bool) -> None:
        _MOUSEINPUT, KEYBDINPUT, INPUT = self._input_structs()
        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_EXTENDEDKEY = 0x0001
        MAPVK_VK_TO_VSC = 0
        scan = int(self._user32.MapVirtualKeyW(int(vk), MAPVK_VK_TO_VSC))
        flags = 0
        if not down:
            flags |= KEYEVENTF_KEYUP
        if int(vk) in _EXTENDED_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        item = INPUT()
        item.type = INPUT_KEYBOARD
        item.union.ki = KEYBDINPUT(int(vk), scan, flags, 0, 0)
        self._send([item])

    def send_unicode_char(self, ch: str, down: bool) -> None:
        _MOUSEINPUT, KEYBDINPUT, INPUT = self._input_structs()
        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004
        flags = KEYEVENTF_UNICODE | (0 if down else KEYEVENTF_KEYUP)
        item = INPUT()
        item.type = INPUT_KEYBOARD
        item.union.ki = KEYBDINPUT(0, ord(ch), flags, 0, 0)
        self._send([item])

    def vk_for_char(self, ch: str) -> Optional[tuple[int, bool]]:
        res = int(self._user32.VkKeyScanW(self._wintypes.WCHAR(ch)))
        if res == -1:
            return None
        vk = res & 0xFF
        state = (res >> 8) & 0xFF
        if state & 0b110:  # Ctrl/Alt (AltGr) required: use the Unicode path
            return None
        return vk, bool(state & 0b1)

    # -- elevation (UIPI guard) ------------------------------------------------

    def _token_elevated(self, process_handle: int) -> Optional[bool]:
        if self._advapi32 is None:  # pragma: no cover
            return None
        ctypes = self._ctypes
        TOKEN_QUERY = 0x0008
        TokenElevation = 20
        token = self._wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(
            process_handle, TOKEN_QUERY, ctypes.byref(token)
        ):
            return None
        try:
            elevation = self._wintypes.DWORD(0)
            returned = self._wintypes.DWORD(0)
            ok = self._advapi32.GetTokenInformation(
                token,
                TokenElevation,
                ctypes.byref(elevation),
                ctypes.sizeof(elevation),
                ctypes.byref(returned),
            )
            if not ok:
                return None
            return bool(elevation.value)
        finally:
            self._kernel32.CloseHandle(token)

    def self_elevated(self) -> Optional[bool]:
        return self._token_elevated(self._kernel32.GetCurrentProcess())

    def process_elevated(self, pid: int) -> Optional[bool]:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = self._kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return None
        try:
            return self._token_elevated(handle)
        finally:
            self._kernel32.CloseHandle(handle)

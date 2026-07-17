"""Native macOS window backend with fail-closed targeting and permissions.

This backend reuses the proven CoreGraphics/AppKit primitives from
``remote_display`` while preserving native Mac semantics:

* ``ControlOrMeta`` / ``Meta`` / ``Command`` mean the Command key, not Control.
* text uses exact-window-bound AX selected-text delivery rather than silently
  droppable Unicode event payloads or keyboard-layout-dependent scancodes.
* every target selector must resolve to exactly one normal application window.
* the exact target window must be topmost before input; app-level focus alone is
  insufficient when an application owns multiple overlapping windows.
* Screen Recording and Accessibility denial fail before capture/input instead
  of producing a blank frame or a silently dropped event.

It intentionally does not claim AX structural resolution yet. AX is used only
to raise the already resolved exact window and replace the selection in that
window's already focused text element. Until broader live evidence exists this
class implements only the base ``Backend`` protocol and keeps the existing
visual ladder as its honest resolution surface.
"""

from __future__ import annotations

import time
from typing import Optional, Protocol, runtime_checkable

from openadapt_flow.backends.remote_display import (
    _CHAR_KEYCODES,
    _MAC_KEYCODES,
    _NAMED_KEY_ALIASES,
    MacWindowClient,
    RemoteDisplayBackend,
    RemoteDisplayError,
    WindowClient,
    WindowInfo,
)


class MacOSBackendError(RemoteDisplayError):
    """Native macOS capture/input could not be performed safely."""


@runtime_checkable
class MacOSClient(WindowClient, Protocol):
    """Typed native-Mac extension of the shared window/input client."""

    def capture_trusted(self) -> bool: ...

    def request_capture_access(self) -> bool: ...

    def request_input_access(self) -> bool: ...

    def find_windows(
        self, owner_substr: str, title_substr: Optional[str]
    ) -> list[WindowInfo]: ...

    def frontmost_window_id(self) -> Optional[int]: ...

    def focused_application_pid(self) -> Optional[int]: ...

    def window_id_at_point(self, x: float, y: float) -> Optional[int]: ...

    def raise_window(self, window: WindowInfo) -> bool: ...

    def exact_window_focused_main(self, window: WindowInfo) -> bool: ...

    def replace_selected_text(self, window: WindowInfo, text: str) -> bool: ...


_NATIVE_MODIFIER_FLAGS = {
    "ctrl": "control",
    "control": "control",
    "controlormeta": "command",
    "meta": "command",
    "cmd": "command",
    "command": "command",
    "win": "command",
    "alt": "alternate",
    "option": "alternate",
    "shift": "shift",
}


def _split_native_chord(key: str) -> tuple[list[str], str]:
    parts = [part for part in key.split("+") if part]
    if not parts:
        raise ValueError(f"empty key chord: {key!r}")
    modifiers: list[str] = []
    for part in parts[:-1]:
        flag = _NATIVE_MODIFIER_FLAGS.get(part.lower())
        if flag is None:
            raise ValueError(f"unknown modifier in chord {key!r}: {part!r}")
        modifiers.append(flag)
    final = _NAMED_KEY_ALIASES.get(parts[-1].lower(), parts[-1])
    return modifiers, final


class MacOSBackend(RemoteDisplayBackend):
    """Drive one uniquely selected native macOS application window."""

    def __init__(
        self,
        client: Optional[MacOSClient] = None,
        *,
        app: str,
        window_title: Optional[str] = None,
        require_capture_trust: bool = True,
        require_input_trust: bool = True,
        settle_s: float = 0.03,
        foreground_retries: int = 10,
        foreground_settle_s: float = 0.1,
    ) -> None:
        if not app.strip():
            raise ValueError("native macOS backend requires a non-empty app name")
        native_client = client if client is not None else MacWindowClient()
        self._mac_client: MacOSClient = native_client
        self._require_capture_trust = require_capture_trust
        self._foreground_retries = max(1, int(foreground_retries))
        self._foreground_settle_s = max(0.0, float(foreground_settle_s))
        self._captured_window: Optional[WindowInfo] = None
        super().__init__(
            native_client,
            owner_substr=app.strip(),
            title_substr=window_title.strip() if window_title else None,
            require_input_trust=require_input_trust,
            activate_before_input=False,
            settle_s=settle_s,
        )

    def _resolve_window(self, *, refresh: bool = False) -> WindowInfo:
        if self._window is not None and not refresh:
            return self._window
        candidates = self._mac_client.find_windows(
            self._owner_substr, self._title_substr
        )
        if not candidates:
            raise MacOSBackendError(
                "no native macOS window matching app "
                f"{self._owner_substr!r} title {self._title_substr!r}"
            )
        if len(candidates) != 1:
            summary = ", ".join(
                f"id={window.window_id} pid={window.pid} title={window.title!r}"
                for window in candidates[:5]
            )
            raise MacOSBackendError(
                "ambiguous native macOS target: selector matched "
                f"{len(candidates)} windows ({summary}); provide a unique "
                "macos_window_title. Refusing first-match input."
            )
        self._window = candidates[0]
        return candidates[0]

    def screenshot(self) -> bytes:
        if self._require_capture_trust and not self._mac_client.capture_trusted():
            raise MacOSBackendError(
                "Screen Recording is not granted, so target-window pixels may "
                "be blank or unavailable. In System Settings > Privacy & "
                "Security > Screen & System Audio Recording, enable the app "
                "that launches openadapt-flow, then restart it."
            )
        frame = super().screenshot()
        self._captured_window = self._resolve_window()
        return frame

    def _assert_bound_physical_target(
        self,
        bound: WindowInfo,
        *,
        point: Optional[tuple[float, float]] = None,
    ) -> None:
        """Require every global-input proof to still match one bound window."""
        current = self._resolve_window(refresh=True)
        point_window_id = (
            self._mac_client.window_id_at_point(*point) if point is not None else None
        )
        if (
            current != bound
            or not current.on_screen
            or self._mac_client.focused_application_pid() != bound.pid
            or self._mac_client.frontmost_window_id() != bound.window_id
            or not self._mac_client.exact_window_focused_main(bound)
            or (point is not None and point_window_id != bound.window_id)
        ):
            raise MacOSBackendError(
                "native physical-input target changed after foreground proof; "
                f"bound id={bound.window_id} pid={bound.pid} "
                f"bounds={bound.bounds!r} title={bound.title!r}. Refusing "
                "coordinate/global input against a stale window mapping"
            )

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click only through one foregrounded, frame-compatible window binding."""
        if self._captured_window is None or self._viewport is None:
            self._ensure_input_ready()
            self.screenshot()
        assert self._captured_window is not None
        assert self._viewport is not None
        frame_window = self._captured_window
        bound = self._bind_physical_target()
        if (
            bound.window_id != frame_window.window_id
            or bound.pid != frame_window.pid
            or bound.title != frame_window.title
            or bound.bounds[2:] != frame_window.bounds[2:]
        ):
            raise MacOSBackendError(
                "native window was resized, reopened, or retitled after its "
                "frame was captured; refusing to map stale pixel coordinates "
                f"from id={frame_window.window_id} bounds={frame_window.bounds!r} "
                f"to id={bound.window_id} bounds={bound.bounds!r}"
            )
        width, height = self._viewport
        if not (0 <= x < width and 0 <= y < height):
            raise MacOSBackendError(
                f"click ({x}, {y}) is outside captured viewport {self._viewport}"
            )
        scale = self._scale or 1.0
        sx = bound.bounds[0] + x / scale
        sy = bound.bounds[1] + y / scale

        point = (sx, sy)
        self._assert_bound_physical_target(bound, point=point)
        self._mac_client.mouse_move(sx, sy)
        time.sleep(self._settle_s)
        self._assert_bound_physical_target(bound, point=point)
        counts = 2 if double else 1
        for index in range(counts):
            self._assert_bound_physical_target(bound, point=point)
            self._mac_client.mouse(
                sx,
                sy,
                button="left",
                down=True,
                click_count=index + 1,
            )
            try:
                time.sleep(self._settle_s)
                self._assert_bound_physical_target(bound, point=point)
            finally:
                # Always release a button that this backend pressed. If focus
                # changed after mouse-down, the assertion still propagates and
                # no further click is attempted, but leaving a global button
                # latched would corrupt the operator's next physical action.
                self._mac_client.mouse(
                    sx,
                    sy,
                    button="left",
                    down=False,
                    click_count=index + 1,
                )
            time.sleep(self._settle_s)
            self._assert_bound_physical_target(bound, point=point)

    def ensure_foreground(
        self, *, retries: Optional[int] = None, settle_s: Optional[float] = None
    ) -> None:
        """Require the exact selected window, not merely its app, to be topmost."""
        attempts = self._foreground_retries if retries is None else max(1, retries)
        pause = self._foreground_settle_s if settle_s is None else max(0.0, settle_s)
        target = self._resolve_window(refresh=True)
        for _ in range(attempts):
            self._mac_client.activate(target.pid)
            # Activating an application is not enough when macOS restores a
            # sibling document window in the same process. Raise the uniquely
            # selected AX window, then still verify its exact CoreGraphics id
            # below before input. A failed AX raise never relaxes the check.
            exact_ax_focus = self._mac_client.raise_window(target)
            target = self._resolve_window(refresh=True)
            if (
                exact_ax_focus
                and target.on_screen
                and self._mac_client.focused_application_pid() == target.pid
                and self._mac_client.frontmost_window_id() == target.window_id
            ):
                return
            # Activation is asynchronous on some applications, so retry after
            # the configured settle interval. Check before sleeping: a remote
            # control or notification window can legitimately take focus, and
            # sleeping after AXRaise widens the race between proof and input.
            time.sleep(pause)
        raise MacOSBackendError(
            f"target window id={target.window_id} app={target.owner!r} "
            f"title={target.title!r} is not the topmost window; refusing input "
            "that could land in another application window"
        )

    def _ensure_input_trusted(self) -> None:
        if self._require_input_trust and not self._mac_client.input_trusted():
            raise MacOSBackendError(
                "Accessibility is not granted, so macOS would silently drop "
                "synthetic input. In System Settings > Privacy & Security > "
                "Accessibility, enable the app that launches openadapt-flow, "
                "then restart it. Refusing to emit input."
            )

    def _ensure_input_ready(self) -> None:
        """Gate physical/global input on both active app and exact window."""
        self._ensure_input_trusted()
        self.ensure_foreground()
        bound = self._resolve_window(refresh=True)
        self._assert_bound_physical_target(bound)

    def _bind_physical_target(self) -> WindowInfo:
        """Run the base-compatible input gate and return its exact binding."""
        self._ensure_input_ready()
        bound = self._resolve_window(refresh=True)
        self._assert_bound_physical_target(bound)
        return bound

    def type_text(self, text: str) -> None:
        if not text:
            return
        # AX text replacement is addressed to one exact focused element, not
        # routed through the global keyboard event stream. NSWorkspace can lag
        # behind the WindowServer/AX state under remote control, so bound AX
        # delivery does not require its frontmost PID. It still requires all
        # exact target proofs below. Physical input paths continue to call
        # _ensure_input_ready(), which retains the active-app PID requirement.
        self._ensure_input_trusted()
        target = self._resolve_window(refresh=True)
        for _ in range(self._foreground_retries):
            self._mac_client.activate(target.pid)
            exact_ax_focus = self._mac_client.raise_window(target)
            target = self._resolve_window(refresh=True)
            exact_cg_topmost = (
                target.on_screen
                and self._mac_client.frontmost_window_id() == target.window_id
            )
            if exact_ax_focus and exact_cg_topmost:
                if self._mac_client.replace_selected_text(target, text):
                    return
                # Never retry or fall back after an AX delivery attempt. An AX
                # error is not proof that the target remained unchanged, so a
                # retry could duplicate or redirect consequential text.
                raise MacOSBackendError(
                    "the exact focused native text element was not writable or "
                    f"did not confirm AX delivery for window id={target.window_id} "
                    f"pid={target.pid} title={target.title!r}; refusing physical "
                    "keyboard or clipboard fallback"
                )
            time.sleep(self._foreground_settle_s)
        raise MacOSBackendError(
            "native text target could not be bound to the unique exact topmost "
            f"CG and focused/main AX window id={target.window_id} pid={target.pid} "
            f"title={target.title!r}; refusing text delivery"
        )

    def press(self, key: str) -> None:
        """Press a native Mac key/chord with Command-correct semantics."""
        modifiers, final = _split_native_chord(key)
        if len(final) == 1 and not modifiers:
            self.type_text(final)
            return
        bound = self._bind_physical_target()
        code = _MAC_KEYCODES.get(final.lower())
        shift = False
        if code is None:
            character = _CHAR_KEYCODES.get(final)
            if character is not None:
                code, shift = character
        if code is None:
            raise MacOSBackendError(f"no native Mac key mapping for {final!r}")
        flags = list(modifiers) + (["shift"] if shift else [])
        self._assert_bound_physical_target(bound)
        try:
            self._mac_client.key(code, down=True, flags=flags)
            self._assert_bound_physical_target(bound)
        finally:
            self._mac_client.key(code, down=False, flags=flags)
        self._assert_bound_physical_target(bound)

    def scroll(self, dx: int, dy: int) -> None:
        if dx == 0 and dy == 0:
            return
        raise MacOSBackendError(
            "native global scroll is not point-bound and could affect the "
            "window under the operator's cursor; refusing until a verified "
            "target-point scroll operation is available"
        )

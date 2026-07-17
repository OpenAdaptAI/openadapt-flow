"""Native macOS window backend with fail-closed targeting and permissions.

This backend reuses the proven CoreGraphics/AppKit primitives from
``remote_display`` while preserving native Mac semantics:

* ``ControlOrMeta`` / ``Meta`` / ``Command`` mean the Command key, not Control.
* text uses layout-independent Unicode events rather than guest scancodes.
* every target selector must resolve to exactly one normal application window.
* the exact target window must be topmost before input; app-level focus alone is
  insufficient when an application owns multiple overlapping windows.
* Screen Recording and Accessibility denial fail before capture/input instead
  of producing a blank frame or a silently dropped event.

It intentionally does not claim AX structural resolution yet. AX observation
and action require live permissioned trials across the supported applications;
until that evidence exists this class implements only the base ``Backend``
protocol and keeps the existing visual ladder as its honest resolution surface.
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

    def type_unicode(self, text: str) -> None: ...


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
        return super().screenshot()

    def ensure_foreground(
        self, *, retries: Optional[int] = None, settle_s: Optional[float] = None
    ) -> None:
        """Require the exact selected window, not merely its app, to be topmost."""
        attempts = self._foreground_retries if retries is None else max(1, retries)
        pause = self._foreground_settle_s if settle_s is None else max(0.0, settle_s)
        target = self._resolve_window(refresh=True)
        for _ in range(attempts):
            self._mac_client.activate(target.pid)
            time.sleep(pause)
            target = self._resolve_window(refresh=True)
            if (
                target.on_screen
                and self._mac_client.frontmost_pid() == target.pid
                and self._mac_client.frontmost_window_id() == target.window_id
            ):
                return
        raise MacOSBackendError(
            f"target window id={target.window_id} app={target.owner!r} "
            f"title={target.title!r} is not the topmost window; refusing input "
            "that could land in another application window"
        )

    def _ensure_input_ready(self) -> None:
        if self._require_input_trust and not self._mac_client.input_trusted():
            raise MacOSBackendError(
                "Accessibility is not granted, so macOS would silently drop "
                "synthetic input. In System Settings > Privacy & Security > "
                "Accessibility, enable the app that launches openadapt-flow, "
                "then restart it. Refusing to emit input."
            )
        self.ensure_foreground()

    def type_text(self, text: str) -> None:
        if not text:
            return
        self._ensure_input_ready()
        self._mac_client.type_unicode(text)

    def press(self, key: str) -> None:
        """Press a native Mac key/chord with Command-correct semantics."""
        modifiers, final = _split_native_chord(key)
        self._ensure_input_ready()
        if len(final) == 1 and not modifiers:
            self._mac_client.type_unicode(final)
            return
        code = _MAC_KEYCODES.get(final.lower())
        shift = False
        if code is None:
            character = _CHAR_KEYCODES.get(final)
            if character is not None:
                code, shift = character
        if code is None:
            raise MacOSBackendError(f"no native Mac key mapping for {final!r}")
        flags = list(modifiers) + (["shift"] if shift else [])
        try:
            self._mac_client.key(code, down=True, flags=flags)
        finally:
            self._mac_client.key(code, down=False, flags=flags)

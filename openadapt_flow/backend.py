"""Backend protocol: the only interface the runtime uses to touch a GUI.

The runtime is vision-only by construction — it sees PNG bytes and emits
clicks/keys at pixel coordinates. Anything that can screenshot and inject
input can be a backend: a Playwright page (reference/test backend), a native
OS layer (pyautogui/Quartz), or an RDP session.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class StructuralBackend(Protocol):
    """Optional structural observations a backend MAY expose.

    Vision alone cannot see effects that never render in the frame — a
    new-tab click, an SPA route change below the fold. Backends that can
    cheaply observe URL / title / page count expose these read-only
    properties; the recorder captures them per event and the compiler mines
    *structural* postconditions (URL_CHANGED, TITLE_CHANGED, NEW_TAB_OPENED)
    as a fallback for steps that would otherwise assert nothing. Backends
    without these observations (native OS, RDP) simply don't implement them;
    such steps stay honestly unverified (docs/LIMITS.md).

    Each property returns None when the observation is momentarily
    unavailable (e.g. mid-navigation).
    """

    @property
    def url(self) -> Optional[str]:
        """Current page URL, or None if unobservable."""
        ...

    @property
    def page_title(self) -> Optional[str]:
        """Current page title, or None if unobservable."""
        ...

    @property
    def page_count(self) -> Optional[int]:
        """Number of open pages/tabs, or None if unobservable."""
        ...


@runtime_checkable
class Backend(Protocol):
    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the screen surface in pixels."""
        ...

    def screenshot(self) -> bytes:
        """Return the current frame as PNG bytes."""
        ...

    def click(self, x: int, y: int, *, double: bool = False) -> None: ...

    def type_text(self, text: str) -> None:
        """Type text into the currently focused element."""
        ...

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. 'Enter', 'Tab', 'Meta+a'."""
        ...

    def scroll(self, dx: int, dy: int) -> None:
        """Scroll by (dx, dy) pixels — a wheel gesture at the current
        pointer position (positive dy scrolls content up / view down)."""
        ...

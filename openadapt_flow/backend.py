"""Backend protocol: the only interface the runtime uses to touch a GUI.

The runtime is vision-only by construction — it sees PNG bytes and emits
clicks/keys at pixel coordinates. Anything that can screenshot and inject
input can be a backend: a Playwright page (reference/test backend), a native
OS layer (pyautogui/Quartz), or an RDP session.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


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

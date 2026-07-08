"""Playwright-driven reference backend (sync API, chromium, headless-capable).

Implements the `openadapt_flow.backend.Backend` protocol against a Playwright
`Page`: full-viewport PNG screenshots, mouse clicks at pixel coordinates,
keyboard typing, and key/chord presses. Viewport is fixed at 1280x800 with
deviceScaleFactor=1 so CSS pixels equal screenshot pixels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

VIEWPORT: tuple[int, int] = (1280, 800)

_MODIFIER_ALIASES = {
    "meta": "Meta",
    "cmd": "Meta",
    "command": "Meta",
    "ctrl": "Control",
    "control": "Control",
    "alt": "Alt",
    "option": "Alt",
    "shift": "Shift",
}

_NAMED_KEYS = {
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "space": "Space",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
}


def _normalize_chord(key: str) -> str:
    """Normalize a key or chord like ``'Meta+a'`` to Playwright's format.

    Modifier aliases (``ctrl``, ``cmd``, ...) are canonicalized; common named
    keys are case-corrected; single characters pass through unchanged.

    Args:
        key: Key name or ``+``-joined chord (e.g. ``'Enter'``, ``'Meta+a'``).

    Returns:
        The Playwright-compatible key/chord string.
    """
    parts = [p for p in key.split("+") if p]
    normalized: list[str] = []
    for part in parts:
        lower = part.lower()
        if lower in _MODIFIER_ALIASES:
            normalized.append(_MODIFIER_ALIASES[lower])
        elif lower in _NAMED_KEYS:
            normalized.append(_NAMED_KEYS[lower])
        else:
            normalized.append(part)
    return "+".join(normalized)


class PlaywrightBackend:
    """`Backend` implementation over a Playwright sync-API `Page`.

    Attributes:
        page: The underlying Playwright page (public so record-time helpers
            such as the demo driver may use locators; replay never does).
    """

    def __init__(self, page: "Page") -> None:
        """Wrap an existing Playwright page.

        Args:
            page: A page created with viewport 1280x800, deviceScaleFactor=1.
        """
        self.page = page

    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the page viewport in pixels."""
        size = self.page.viewport_size
        if size is None:  # pragma: no cover - viewport always set by launch()
            return VIEWPORT
        return (size["width"], size["height"])

    def screenshot(self) -> bytes:
        """Return the current full-viewport frame as PNG bytes."""
        return self.page.screenshot(type="png", full_page=False)

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click (or double-click) at pixel coordinates via the mouse."""
        if double:
            self.page.mouse.dblclick(x, y)
        else:
            self.page.mouse.click(x, y)

    def type_text(self, text: str) -> None:
        """Type text into the currently focused element."""
        self.page.keyboard.type(text)

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. ``'Enter'`` or ``'Meta+a'``."""
        self.page.keyboard.press(_normalize_chord(key))

    def scroll(self, dx: int, dy: int) -> None:
        """Dispatch a wheel gesture at the current mouse position.

        The wheel event targets whatever element is under the pointer, so
        scrolling works inside iframes and nested scroll containers exactly
        as it does for a human — position the pointer first (a preceding
        click does this naturally during both record and replay).
        """
        self.page.mouse.wheel(dx, dy)

    @classmethod
    def launch(
        cls, url: str, headless: bool = True
    ) -> tuple["PlaywrightBackend", Callable[[], None]]:
        """Start Playwright + chromium, open ``url``, and return a backend.

        Args:
            url: URL to navigate the new page to.
            headless: Whether to launch chromium headless.

        Returns:
            ``(backend, close)`` where ``close()`` shuts down the browser and
            the Playwright driver.
        """
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=headless)
        except Exception:
            pw.stop()
            raise
        page = browser.new_page(
            viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
            device_scale_factor=1,
        )
        page.goto(url)
        backend = cls(page)

        def close() -> None:
            try:
                browser.close()
            finally:
                pw.stop()

        return backend, close

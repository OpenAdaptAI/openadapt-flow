"""Backend protocol: the only interface the runtime uses to touch a GUI.

The runtime is vision-only by construction — it sees PNG bytes and emits
clicks/keys at pixel coordinates. Anything that can screenshot and inject
input can be a backend: a Playwright page (reference/test backend), a native
OS layer (pyautogui/Quartz), or an RDP session.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class SystemOfRecordBackend(Protocol):
    """Optional system-of-record observation a backend MAY expose.

    Vision (and even structural URL/title) cannot see whether a consequential
    write actually reached the system of record — a partial save, a phantom
    optimistic-UI success, a duplicate submission all look identical on screen
    (``docs/LIMITS.md`` "5 of 7 write faults silent"). A backend that can read
    the app's authoritative store (a JSON ``/api/db`` endpoint, an EMR's own
    API) exposes it here; the recorder snapshots it before and after each event
    (``sor_before`` / ``sor_after`` on the event, exactly as it already records
    ``url_before`` / ``url_after``), and the compiler's effect miner
    (``compiler.effect_mining``) derives typed ``record_written`` /
    ``field_equals`` effects from the observed delta.

    Backends without a readable system of record (pixel-only substrates) simply
    do not implement this; the miner then falls back to a flagged placeholder
    or an honest "no verifiable effect derivable" (never a fabricated binding).
    """

    @property
    def system_of_record(self) -> Optional[list[dict[str, Any]]]:
        """Current system-of-record records, or None if unobservable.

        None (not ``[]``) when the store cannot be read right now — the miner
        distinguishes "not observed" from "observed empty" (a legitimate
        baseline for a first write).
        """
        ...


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
class IdentityBackend(Protocol):
    """Optional STRUCTURED-TEXT identity capability a backend MAY expose.

    The runtime resolves targets by VISION alone (screenshot in, clicks out);
    that never changes. But *identity verification* -- proving the resolved
    target is the recorded entity, not a look-alike sibling -- does not have
    to be OCR-based when the backend can hand back the real, structured
    characters under a point.

    An adversarial review proved the OCR-only identity path cannot close the
    same-name / same-DOB glyph-collapse case: two DIFFERENT patients whose MRN
    differs only by an O/0 or l/1 glyph ("MG4408" vs "MG44O8") render to a
    byte-identical OCR band -- the same input a legit re-read produces -- so
    no function downstream of OCR can distinguish them (see docs/LIMITS.md and
    benchmark/dense_surface/DENSE_SURFACE.md). The escape is to stop relying
    on OCR for identity where a higher-fidelity signal exists.

    ``structured_text_at`` returns that higher-fidelity signal: the accessible
    / DOM text at (or around) a coordinate, in the REAL characters --
    "MG4408" with a genuine digit 0, not an OCR guess. Backends implement it
    from whatever structured layer they own:

    - a browser backend (Playwright) reads the DOM element under the point
      (``elementFromPoint`` -> row/cell ``textContent`` + ``aria-label``);
    - a native desktop backend reads the accessibility tree -- Windows UI
      Automation ``Name``/``Value``/text, or macOS AX attributes. Crucially,
      an element lacking a stable ``AutomationId`` usually STILL exposes
      Name/Value text, so UIA/AX identity is viable on most native apps even
      where an AutomationId-keyed selector is not.

    The identity ladder treats DOM and UIA/AX text identically -- both are
    "structured text". A pure-pixel substrate (Citrix/RDP/VDI, or a backend
    with no a11y tree) returns None from every point, and identity falls back
    to the OCR name+DOB-primary tier (docs/LIMITS.md). This is an ADDITIVE
    identity capability: the 4-method vision resolution protocol
    (:class:`Backend`) is unchanged.
    """

    def structured_text_at(self, x: int, y: int) -> Optional[str]:
        """Return the structured (DOM / a11y) text at/around pixel (x, y).

        The coordinate space matches :meth:`Backend.click` -- the same pixels
        the resolver emits. Returns the target's row/element text in its REAL
        characters, or None when the backend cannot observe structured text at
        that point (pixel-only substrate, no a11y node, or a momentary
        failure -- never raises).
        """
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

"""Backend implementations of the `openadapt_flow.backend.Backend` protocol.

`PlaywrightBackend` is re-exported lazily so that importing this package does
not require playwright unless the backend is actually used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

__all__ = ["PlaywrightBackend"]


def __getattr__(name: str) -> object:
    if name == "PlaywrightBackend":
        from openadapt_flow.backends.playwright_backend import (
            PlaywrightBackend,
        )

        return PlaywrightBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

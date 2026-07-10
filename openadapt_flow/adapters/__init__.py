"""Adapters converting external demonstration sources into openadapt-flow
recordings (``meta.json`` + ``events.jsonl`` + ``frames/``).

`convert_capture` is re-exported lazily so importing this package does not
pull in cv2 until the adapter is actually used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.adapters.capture import convert_capture

__all__ = ["convert_capture"]


def __getattr__(name: str) -> object:
    if name == "convert_capture":
        from openadapt_flow.adapters.capture import convert_capture

        return convert_capture
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""Grounder protocol and implementations.

A Grounder is the last rung of the resolution ladder: given the current
screen, a step intent, and optional anchor text, it returns a Match-like
object (point/region/confidence) or None.

Ships a :class:`NullGrounder` (always None; keeps the ladder wiring uniform)
and an :class:`AnthropicGrounder` that is import-guarded behind the optional
``anthropic`` package (the ``grounder`` extra). The Anthropic implementation
is never exercised in tests and requires an API key at runtime.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel

from openadapt_flow.ir import Point, Region


class GrounderMatch(BaseModel):
    """Match-like result returned by a Grounder.

    Mirrors the shape of ``openadapt_flow.vision.Match`` without importing
    the vision package (the runtime must stay decoupled from it).
    """

    point: Point
    region: Region
    confidence: float


@runtime_checkable
class Grounder(Protocol):
    """Protocol for model-backed target grounding."""

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Locate the target described by ``intent`` on the screen.

        Args:
            screen_png: Current frame as PNG bytes.
            intent: Human-readable description of the target/step.
            ocr_text: Text label at/near the target, if known.

        Returns:
            A Match-like object, or None if the target cannot be located.
        """
        ...


class NullGrounder:
    """Grounder that never locates anything (the safe default)."""

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Always return None."""
        return None


_DEFAULT_MODEL = "claude-opus-4-8"
_REGION_HALF_W = 20
_REGION_HALF_H = 10

_PROMPT = (
    "You are grounding a UI automation target on a screenshot.\n"
    "Target intent: {intent}\n"
    "Target text label (may be stale): {ocr_text}\n\n"
    "Reply with ONLY a JSON object of pixel coordinates for the point to "
    'click, e.g. {{"x": 123, "y": 45}}. If the target is not visible, reply '
    'with ONLY {{"x": null, "y": null}}.'
)


class AnthropicGrounder:
    """Grounder backed by the Anthropic API (vision-capable Claude model).

    Requires the optional ``anthropic`` package (install the ``grounder``
    extra). Never used in tests; calls count as model calls in run reports.
    """

    def __init__(self, model: str = _DEFAULT_MODEL, client: object = None) -> None:
        """Create the grounder.

        Args:
            model: Anthropic model id (must be vision-capable).
            client: Optional pre-built ``anthropic.Anthropic`` client
                (useful for injecting configuration).

        Raises:
            ImportError: If the ``anthropic`` package is not installed.
        """
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError(
                "AnthropicGrounder requires the 'anthropic' package. "
                "Install it with: pip install 'openadapt-flow[grounder]'"
            ) from exc
        self._model = model
        self._client = client if client is not None else anthropic.Anthropic()

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Ask the model for the click point of the described target.

        Args:
            screen_png: Current frame as PNG bytes.
            intent: Human-readable description of the target/step.
            ocr_text: Text label at/near the target, if known.

        Returns:
            A :class:`GrounderMatch` centered on the model's point, or None
            if the model reports the target is not visible or replies in an
            unparseable format.
        """
        image_b64 = base64.standard_b64encode(screen_png).decode("utf-8")
        response = self._client.messages.create(
            model=self._model,
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": _PROMPT.format(
                                intent=intent, ocr_text=ocr_text or "(none)"
                            ),
                        },
                    ],
                }
            ],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            return None
        text = next(
            (b.text for b in response.content if getattr(b, "type", "") == "text"),
            "",
        )
        payload = _extract_json_object(text)
        if payload is None:
            return None
        x, y = payload.get("x"), payload.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return None
        px, py = int(round(x)), int(round(y))
        region: Region = (
            max(0, px - _REGION_HALF_W),
            max(0, py - _REGION_HALF_H),
            2 * _REGION_HALF_W,
            2 * _REGION_HALF_H,
        )
        return GrounderMatch(point=(px, py), region=region, confidence=0.5)


def _extract_json_object(text: str) -> Optional[dict]:
    """Extract the first JSON object embedded in ``text``, if any."""
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None

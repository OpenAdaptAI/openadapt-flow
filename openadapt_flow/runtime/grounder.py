"""Grounder protocol and implementations.

A Grounder is the last rung of the resolution ladder: given the current
screen, a step intent, and optional anchor text, it returns a Match-like
object (point/region/confidence) or None.

Ships:

- :class:`NullGrounder` — always None; keeps the ladder wiring uniform and is
  the safe default.
- :class:`AnthropicGrounder` — import-guarded behind the optional
  ``anthropic`` package (the ``grounder`` extra). Requires an API key; never
  exercised in tests. NOT on-prem.
- :class:`GuiOwlGrounder` — an OPEN GUI-grounding specialist
  (``mPLUG/GUI-Owl-1.5-8B-Instruct``, MIT) served either locally via
  ``mlx-vlm`` on Apple Silicon (the ``grounder-mlx`` extra) or against any
  OpenAI-compatible ``/chat/completions`` endpoint — a vLLM/SGLang server
  (the ``grounder-http`` extra). This is the "grounding rung" of
  ``docs/grounding_rung.md``.

The composition is the whole point: **the grounder is trusted for
availability, never for safety.** Whatever coordinate any grounder proposes
still passes through the pre-click identity band check
(:func:`openadapt_flow.runtime.identity.verify_target_identity`) in the
replayer, exactly as a geometry-rung estimate does — a grounder that points
at the wrong row is caught there and the run safe-halts. So a grounder can
convert false-aborts (safe-halts on a present target) into successes without
ever being able to buy a wrong target a click.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Callable, Optional, Protocol, runtime_checkable

from pydantic import BaseModel

from openadapt_flow.ir import Point, Region
from openadapt_flow.runtime.resolver import png_size


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


# -- open GUI-grounding rung (GUI-Owl-1.5) -----------------------------------

# GUI-Owl-1.5 is Qwen3-VL-based and emits target coordinates in the Qwen3-VL
# normalized 0-1000 space (x,y each in [0,1000], independent of pixel size).
# This is the coordinate gotcha called out in the OSS-model memo: Qwen2.5-VL
# used absolute pixels, Qwen3-VL switched to 0-1000, and several harnesses
# tanked ScreenSpot-Pro to <2% by parsing the wrong space. The scaling here
# is pinned and regression-tested (tests/test_grounding_rung.py); confirm the
# space against a served checkpoint before trusting a new model version.
COORD_NORM_1000 = "norm_1000"
COORD_PIXEL = "pixel"

_GUI_OWL_DEFAULT_MODEL = "mPLUG/GUI-Owl-1.5-8B-Instruct"
_GUI_OWL_MLX_MODEL = "clinan/GUI-Owl-1.5-2B-Instruct-MLX-4bit"

_GROUND_PROMPT = (
    "You are a GUI visual grounding model. Locate the single UI element the "
    "instruction refers to and output the pixel coordinate to click.\n"
    "Instruction: {intent}\n"
    "Nearby text label (may be stale OCR, use only as a hint): {ocr_text}\n"
    'Reply with ONLY a JSON object, e.g. {{"x": 512, "y": 337}}. If the '
    'element is not visible, reply with ONLY {{"x": null, "y": null}}.'
)


def parse_grounder_point(
    text: str, viewport: tuple[int, int], coord_space: str
) -> Optional[Point]:
    """Parse a model's textual reply into an absolute pixel ``Point``.

    Tolerant of the shapes open grounders emit: a JSON object with ``x``/``y``
    (or a ``point``/``coordinate`` list), a Qwen ``<|box_start|>(x1,y1),
    (x2,y2)<|box_end|>`` region (its center is used), or a bare ``(x, y)`` /
    ``x, y`` pair. Coordinates are interpreted in ``coord_space``
    (:data:`COORD_NORM_1000` scaled by the viewport, or :data:`COORD_PIXEL`
    taken as-is) and clamped to the frame.

    Args:
        text: The raw model reply.
        viewport: ``(width, height)`` of the frame the model saw.
        coord_space: :data:`COORD_NORM_1000` or :data:`COORD_PIXEL`.

    Returns:
        The pixel ``Point``, or None when nothing parseable / an explicit
        not-visible (null) reply is found.
    """
    w, h = viewport
    xy = _raw_xy(text)
    if xy is None:
        return None
    x, y = xy
    if coord_space == COORD_NORM_1000:
        x = x / 1000.0 * w
        y = y / 1000.0 * h
    px = max(0, min(w - 1, int(round(x))))
    py = max(0, min(h - 1, int(round(y))))
    return (px, py)


def _raw_xy(text: str) -> Optional[tuple[float, float]]:
    """Pull the first (x, y) pair out of ``text`` in model-native units."""
    obj = _extract_json_object(text)
    if obj is not None:
        x, y = obj.get("x"), obj.get("y")
        if x is None or y is None:
            # An explicit {"x": null, "y": null} not-visible reply.
            if "x" in obj and "y" in obj:
                return None
            for key in ("point", "coordinate", "coord", "click", "box_2d"):
                seq = obj.get(key)
                if isinstance(seq, (list, tuple)) and len(seq) >= 2:
                    return _num(seq[0]), _num(seq[1])
        elif isinstance(x, (int, float)) and isinstance(y, (int, float)):
            return float(x), float(y)
    # Qwen box tokens: use the box center.
    box = re.search(
        r"\(?\s*(\d+)\s*,\s*(\d+)\s*\)?\s*,\s*\(?\s*(\d+)\s*,\s*(\d+)\s*\)?",
        text,
    )
    if box is not None:
        x1, y1, x2, y2 = (int(g) for g in box.groups())
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0
    pair = re.search(r"\(?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)?", text)
    if pair is not None:
        return float(pair.group(1)), float(pair.group(2))
    return None


def _num(v: object) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


class GuiOwlGrounder:
    """Open GUI-grounding rung backed by GUI-Owl-1.5 (MIT).

    Two interchangeable backends, selected by ``backend``:

    - ``"mlx"`` — serve the model locally with ``mlx-vlm`` on Apple Silicon
      (MPS). On-prem, no API. Install the ``grounder-mlx`` extra.
    - ``"http"`` — POST to an OpenAI-compatible ``/chat/completions`` endpoint
      (a vLLM or SGLang server hosting the 8B checkpoint). Install the
      ``grounder-http`` extra (``requests``). This is the recommended
      production/deploy path for the 8B model on a GPU.

    The model's reply is parsed by :func:`parse_grounder_point` into an
    absolute pixel point (default coordinate space: GUI-Owl-1.5's Qwen3-VL
    normalized 0-1000). ``locate`` returns a :class:`GrounderMatch` for the
    ladder; the replayer's identity gate still verifies the target before any
    click. Determinism: ``temperature`` defaults to 0.

    A ``transport`` callable ``(screen_png, intent, ocr_text) -> str`` may be
    injected to unit-test parsing/scaling without loading a model or hitting a
    network (this is how the grounder is exercised in tests — no model call).
    """

    def __init__(
        self,
        *,
        backend: str = "mlx",
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        coord_space: str = COORD_NORM_1000,
        temperature: float = 0.0,
        max_tokens: int = 64,
        confidence: float = 0.5,
        region_half_w: int = _REGION_HALF_W,
        region_half_h: int = _REGION_HALF_H,
        transport: Optional[Callable[[bytes, str, Optional[str]], str]] = None,
    ) -> None:
        """Create the grounder.

        Args:
            backend: ``"mlx"`` (local Apple-Silicon serving) or ``"http"``
                (OpenAI-compatible endpoint). Ignored when ``transport`` is
                supplied.
            model: HF repo / local path (mlx) or served model id (http).
                Defaults to the MLX 2B repo for ``mlx`` and the 8B repo for
                ``http``.
            endpoint: Base URL of the OpenAI-compatible server (http backend),
                e.g. ``http://localhost:8000/v1``.
            api_key: Bearer token for the http backend, if the server needs one.
            coord_space: Model coordinate space (:data:`COORD_NORM_1000` or
                :data:`COORD_PIXEL`).
            temperature: Sampling temperature (0 = greedy/deterministic).
            max_tokens: Generation cap for the short coordinate reply.
            confidence: Confidence stamped on the returned match. Advisory
                only — the grounder rung is below OCR, so the identity gate,
                not this number, governs whether the click happens.
            region_half_w: Half-width of the returned match region (px).
            region_half_h: Half-height of the returned match region (px).
            transport: Optional injected ``(png, intent, ocr_text) -> reply``
                callable that bypasses backend loading (for tests).
        """
        self.backend = backend
        self.coord_space = coord_space
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.confidence = confidence
        self._region_half_w = region_half_w
        self._region_half_h = region_half_h
        self._endpoint = endpoint
        self._api_key = api_key
        if transport is not None:
            self._transport = transport
            self.model = model or "(injected-transport)"
            return
        if backend == "mlx":
            self.model = model or _GUI_OWL_MLX_MODEL
            self._transport = self._build_mlx_transport()
        elif backend == "http":
            self.model = model or _GUI_OWL_DEFAULT_MODEL
            self._transport = self._build_http_transport()
        else:
            raise ValueError(
                f"Unknown grounder backend {backend!r} (expected 'mlx', "
                "'http', or an injected transport)"
            )

    # -- backends -------------------------------------------------------------

    def _build_mlx_transport(
        self,
    ) -> Callable[[bytes, str, Optional[str]], str]:
        try:
            from mlx_vlm import generate, load
            from mlx_vlm.prompt_utils import apply_chat_template
            from mlx_vlm.utils import load_config
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError(
                "GuiOwlGrounder(backend='mlx') requires mlx-vlm. Install it "
                "with: pip install 'openadapt-flow[grounder-mlx]' (Apple "
                "Silicon only)."
            ) from exc
        model, processor = load(self.model)
        config = load_config(self.model)

        def transport(png: bytes, intent: str, ocr_text: Optional[str]) -> str:
            import tempfile

            prompt = _GROUND_PROMPT.format(
                intent=intent, ocr_text=ocr_text or "(none)"
            )
            formatted = apply_chat_template(
                processor, config, prompt, num_images=1
            )
            with tempfile.NamedTemporaryFile(suffix=".png") as fh:
                fh.write(png)
                fh.flush()
                result = generate(
                    model,
                    processor,
                    formatted,
                    image=[fh.name],
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    verbose=False,
                )
            return getattr(result, "text", str(result))

        return transport

    def _build_http_transport(
        self,
    ) -> Callable[[bytes, str, Optional[str]], str]:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError(
                "GuiOwlGrounder(backend='http') requires requests. Install it "
                "with: pip install 'openadapt-flow[grounder-http]'."
            ) from exc
        if not self._endpoint:
            raise ValueError(
                "GuiOwlGrounder(backend='http') requires an 'endpoint' "
                "(OpenAI-compatible base URL, e.g. http://localhost:8000/v1)."
            )
        url = self._endpoint.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        def transport(png: bytes, intent: str, ocr_text: Optional[str]) -> str:
            image_b64 = base64.standard_b64encode(png).decode("utf-8")
            payload = {
                "model": self.model,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_b64}"
                                },
                            },
                            {
                                "type": "text",
                                "text": _GROUND_PROMPT.format(
                                    intent=intent, ocr_text=ocr_text or "(none)"
                                ),
                            },
                        ],
                    }
                ],
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

        return transport

    # -- Grounder protocol ----------------------------------------------------

    def locate(
        self,
        screen_png: bytes,
        intent: str,
        ocr_text: Optional[str] = None,
    ) -> Optional[GrounderMatch]:
        """Propose a click point for ``intent`` on ``screen_png``.

        Args:
            screen_png: Current frame as PNG bytes.
            intent: Human-readable description of the target/step.
            ocr_text: Text label at/near the target, if known (a hint only).

        Returns:
            A :class:`GrounderMatch` centered on the model's point, or None
            when the model reports the target not visible or replies
            unparseably. The proposal is NOT trusted for safety — the caller's
            identity gate verifies it before any click.
        """
        reply = self._transport(screen_png, intent, ocr_text)
        if not reply:
            return None
        viewport = png_size(screen_png)
        point = parse_grounder_point(reply, viewport, self.coord_space)
        if point is None:
            return None
        px, py = point
        region: Region = (
            max(0, px - self._region_half_w),
            max(0, py - self._region_half_h),
            2 * self._region_half_w,
            2 * self._region_half_h,
        )
        return GrounderMatch(
            point=(px, py), region=region, confidence=self.confidence
        )

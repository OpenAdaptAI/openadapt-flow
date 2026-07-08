"""Claude computer-use agent baseline for the benchmark.

A minimal but fair agent loop: it drives the SAME ``PlaywrightBackend`` the
compiled replayer uses, vision-only — screenshots go to the model; clicks,
typed text, and key presses come back. The task is stated in user-intent
terms (no coordinates, no step-by-step script), the phrasing a real user
would give a computer-use agent.

Model and API facts (checked 2026-07-08 against the Anthropic docs):

- Model: ``claude-sonnet-5`` (the current Sonnet).
- Computer-use tool ``computer_20251124`` with beta header
  ``computer-use-2025-11-24``.
- List pricing: $3.00 / MTok input, $15.00 / MTok output. An introductory
  $2.00 / $10.00 rate applies through 2026-08-31; costs here are computed at
  the durable list price so the numbers stay comparable after the promo.

Loop mechanics:

- screenshot -> model -> execute tool actions -> repeat, until the model
  stops requesting actions or the ``max_actions`` budget (default 25) is hit.
- Every executed action returns a settled screenshot in its ``tool_result``
  (the same settle logic the replayer uses), so the model always sees the
  post-action state without spending an extra action on it.
- Conversation history is bounded: only the last ``keep_screenshots``
  (default 3) screenshot image blocks are kept; older ones are replaced with
  a small text stub.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODEL = "claude-sonnet-5"
COMPUTER_USE_BETA = "computer-use-2025-11-24"
COMPUTER_TOOL_TYPE = "computer_20251124"
#: List price, USD per million tokens (see module docstring re: intro rate).
INPUT_USD_PER_MTOK = 3.00
OUTPUT_USD_PER_MTOK = 15.00
MAX_ACTIONS = 25
KEEP_SCREENSHOTS = 3
MAX_TOKENS = 4096

_SUPPORTED_ACTIONS = (
    "screenshot, left_click, double_click, triple_click, type, key, "
    "wait, mouse_move"
)


def load_api_key() -> str:
    """Resolve the Anthropic API key.

    Reads ``ANTHROPIC_API_KEY`` from the environment, falling back to the
    file ``~/.anthropic/api_key``.

    Returns:
        The API key string.

    Raises:
        RuntimeError: If neither source provides a key.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    path = Path.home() / ".anthropic" / "api_key"
    if path.is_file():
        key = path.read_text().strip()
        if key:
            return key
    raise RuntimeError(
        "No Anthropic API key: set ANTHROPIC_API_KEY or write the key to "
        "~/.anthropic/api_key"
    )


def triage_task_prompt(note_text: str) -> str:
    """Build the natural-language task prompt for the MockMed triage task.

    The prompt describes the same task the demo recording performs, written
    from user intent — what a real user would tell an agent — not as
    coordinates or a click script.

    Args:
        note_text: The note the agent must enter in the encounter.

    Returns:
        The task prompt string.
    """
    return (
        "You are looking at MockMed, a demo clinic web app (fake data "
        "only). Complete this task:\n\n"
        "1. Sign in with username \"nurse.demo\" and password "
        "\"mockmed-demo-pass\".\n"
        "2. Open the first referral task in the list.\n"
        "3. From the patient's page, create a New Encounter and choose the "
        "type \"Triage\".\n"
        f"4. Enter exactly this note in the Note field: \"{note_text}\"\n"
        "5. Save the encounter.\n\n"
        "You are done when you are back on the patient's page and see the "
        "'Encounter saved' confirmation. Then stop and reply with a one-line "
        "summary. Start by taking a screenshot to see the current state."
    )


def compute_cost(input_tokens: int, output_tokens: int) -> float:
    """Cost in USD for a token usage pair at list pricing.

    Args:
        input_tokens: Total input tokens across all API calls.
        output_tokens: Total output tokens across all API calls.

    Returns:
        The cost in USD.
    """
    return (
        input_tokens / 1_000_000 * INPUT_USD_PER_MTOK
        + output_tokens / 1_000_000 * OUTPUT_USD_PER_MTOK
    )


@dataclass
class AgentRunResult:
    """Outcome of one agent run (success is judged separately by verify).

    Attributes:
        actions: Number of computer actions executed.
        api_calls: Number of Messages API calls made.
        input_tokens: Total input tokens (from API usage fields).
        output_tokens: Total output tokens (from API usage fields).
        cost_usd: Cost at list pricing (see module docstring).
        wall_s: Wall-clock seconds for the whole loop.
        stopped: Why the loop ended: ``"model_done"`` (the model stopped
            requesting actions) or ``"budget_exhausted"``.
        model_stop_reason: The final API response's ``stop_reason``.
        final_screenshot: PNG bytes of the final state (for verification).
        action_log: One short line per executed action (for debugging).
    """

    actions: int
    api_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    wall_s: float
    stopped: str
    model_stop_reason: str | None
    final_screenshot: bytes
    action_log: list[str] = field(default_factory=list)


def _capture(backend: Any) -> bytes:
    """Settled screenshot — the same settle logic the replayer uses."""
    from openadapt_flow.vision import wait_settled

    return wait_settled(backend, timeout_s=2.0)


def _screenshot_block(png: bytes) -> dict[str, Any]:
    """Wrap PNG bytes as a base64 image content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(png).decode("ascii"),
        },
    }


def _execute_action(backend: Any, block: Any) -> dict[str, Any]:
    """Execute one computer-use ``tool_use`` block against the backend.

    Args:
        backend: A ``Backend`` (screenshot/click/type_text/press).
        block: The ``tool_use`` content block from the API response.

    Returns:
        A ``tool_result`` dict. Successful actions carry a short text ack
        plus a settled screenshot; unsupported or failing actions carry
        ``is_error: true`` with an explanation.
    """
    inp = dict(block.input) if block.input else {}
    action = inp.get("action")

    def error(message: str) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": message,
            "is_error": True,
        }

    try:
        if action == "screenshot":
            pass  # screenshot is attached below for every action
        elif action in ("left_click", "double_click"):
            x, y = inp["coordinate"]
            backend.click(int(x), int(y), double=action == "double_click")
        elif action == "triple_click":
            x, y = inp["coordinate"]
            for _ in range(3):
                backend.click(int(x), int(y))
        elif action == "type":
            backend.type_text(str(inp["text"]))
        elif action == "key":
            backend.press(str(inp["text"]))
        elif action == "wait":
            time.sleep(min(float(inp.get("duration", 1.0)), 2.0))
        elif action == "mouse_move":
            pass  # cursor position has no effect on this backend
        else:
            return error(
                f"Action {action!r} is not supported in this environment. "
                f"Supported actions: {_SUPPORTED_ACTIONS}."
            )
    except Exception as exc:  # noqa: BLE001 - report to the model, not crash
        return error(f"Action {action!r} failed: {exc}")

    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": [
            {"type": "text", "text": f"Executed {action}."},
            _screenshot_block(_capture(backend)),
        ],
    }


def _truncate_screenshots(
    messages: list[dict[str, Any]], keep: int
) -> None:
    """Replace all but the last ``keep`` screenshot blocks with text stubs.

    Walks the ``tool_result`` blocks of user messages (the only place this
    agent puts images) in order and rewrites every image block except the
    final ``keep`` of them. Assistant messages are never touched.

    Args:
        messages: The conversation history (mutated in place).
        keep: Number of most-recent screenshot image blocks to keep.
    """
    slots: list[tuple[list[Any], int]] = []
    for msg in messages:
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            content = block.get("content")
            if not isinstance(content, list):
                continue
            for i, item in enumerate(content):
                if isinstance(item, dict) and item.get("type") == "image":
                    slots.append((content, i))
    excess = slots[:-keep] if keep > 0 else slots
    for content, i in excess:
        content[i] = {
            "type": "text",
            "text": "[screenshot removed to bound context]",
        }


def run_agent(
    backend: Any,
    task: str,
    *,
    client: Any = None,
    model: str = MODEL,
    max_actions: int = MAX_ACTIONS,
    keep_screenshots: int = KEEP_SCREENSHOTS,
    max_tokens: int = MAX_TOKENS,
) -> AgentRunResult:
    """Run the computer-use agent loop against a backend until done.

    Args:
        backend: A ``Backend`` (the same PlaywrightBackend the replayer
            uses) already navigated to the target app.
        task: Natural-language task prompt (see :func:`triage_task_prompt`).
        client: Anthropic client; when None, one is constructed with the key
            from ``ANTHROPIC_API_KEY`` or ``~/.anthropic/api_key``.
        model: Model ID.
        max_actions: Hard budget on executed computer actions.
        keep_screenshots: How many recent screenshots to keep in history.
        max_tokens: Per-response output token cap.

    Returns:
        An :class:`AgentRunResult` with counters, cost, and the final
        screenshot. Task success is judged separately (``verify``).
    """
    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=load_api_key())

    width, height = backend.viewport
    tools = [
        {
            "type": COMPUTER_TOOL_TYPE,
            "name": "computer",
            "display_width_px": width,
            "display_height_px": height,
        }
    ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]

    actions = 0
    api_calls = 0
    input_tokens = 0
    output_tokens = 0
    stopped = "model_done"
    model_stop_reason: str | None = None
    action_log: list[str] = []
    start = time.monotonic()

    while True:
        response = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            tools=tools,
            betas=[COMPUTER_USE_BETA],
            messages=messages,
        )
        api_calls += 1
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
        model_stop_reason = response.stop_reason

        tool_uses = [
            b for b in response.content if getattr(b, "type", None) == "tool_use"
        ]
        if response.stop_reason != "tool_use" or not tool_uses:
            break

        messages.append({"role": "assistant", "content": response.content})
        results: list[dict[str, Any]] = []
        for block in tool_uses:
            if actions >= max_actions:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Action budget exhausted.",
                        "is_error": True,
                    }
                )
                continue
            actions += 1
            action_log.append(
                f"{actions}: {dict(block.input) if block.input else {}}"
            )
            results.append(_execute_action(backend, block))
        messages.append({"role": "user", "content": results})
        _truncate_screenshots(messages, keep_screenshots)

        if actions >= max_actions:
            stopped = "budget_exhausted"
            break

    wall_s = time.monotonic() - start
    return AgentRunResult(
        actions=actions,
        api_calls=api_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=compute_cost(input_tokens, output_tokens),
        wall_s=wall_s,
        stopped=stopped,
        model_stop_reason=model_stop_reason,
        final_screenshot=_capture(backend),
        action_log=action_log,
    )

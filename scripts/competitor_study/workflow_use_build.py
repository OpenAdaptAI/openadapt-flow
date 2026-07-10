"""Build a workflow-use workflow from the recorded MockMed session (LLM phase).

Mirrors ``python cli.py build-from-recording`` at the pinned commit, with two
documented adaptations (recorded in COMPETITOR_STUDY.md):

- The CLI at this commit hardcodes ``ChatBrowserUse`` (requires a Browser-Use
  cloud API key we do not have). We use the library's documented programmatic
  path (README "Usage from python" / HealingService examples) with
  ``browser_use.llm.ChatAnthropic`` -- a first-class LLM class in the
  browser-use dependency -- on ``claude-sonnet-5``.
- The user goal is supplied non-interactively (verbatim in
  ``study_common.USER_GOAL``) instead of via the CLI prompt.

Exact token usage is captured from the API response and appended to the
spend ledger. Run from the workflow-use venv.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import study_common  # noqa: E402


async def build(recording_path: Path, out_path: Path, ledger_path: Path) -> None:
    from browser_use.llm import ChatAnthropic
    from workflow_use.builder.service import BuilderService
    from workflow_use.schema.views import WorkflowDefinitionSchema

    llm = ChatAnthropic(
        model="claude-sonnet-5",
        api_key=study_common.anthropic_key(),
        max_tokens=8192,
    )

    calls = {"n": 0, "in": 0, "out": 0}
    orig_ainvoke = llm.ainvoke

    async def counting_ainvoke(*args, **kwargs):
        result = await orig_ainvoke(*args, **kwargs)
        calls["n"] += 1
        usage = getattr(result, "usage", None)
        if usage is not None:
            calls["in"] += getattr(usage, "prompt_tokens", 0) or 0
            calls["out"] += getattr(usage, "completion_tokens", 0) or 0
        return result

    llm.ainvoke = counting_ainvoke  # type: ignore[method-assign]

    recording = WorkflowDefinitionSchema.model_validate(
        json.loads(recording_path.read_text())
    )
    builder = BuilderService(llm=llm)
    workflow = await builder.build_workflow(
        recording, user_goal=study_common.USER_GOAL
    )
    out_path.write_text(json.dumps(workflow.model_dump(mode="json"), indent=2))
    print(f"workflow saved: {out_path}")
    print(f"inputs_def: {[i.model_dump() for i in workflow.input_schema]}")
    for i, s in enumerate(workflow.steps):
        d = s.model_dump(mode="json", exclude_none=True)
        print(f"  step {i}: {json.dumps(d)[:220]}")

    ledger = study_common.SpendLedger(ledger_path)
    ledger.add(
        "workflow-use build_workflow",
        calls["n"],
        calls["in"] or None,
        calls["out"] or None,
        note="exact usage from API" if calls["in"] else "",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(build(args.recording, args.out, args.ledger))


if __name__ == "__main__":
    main()

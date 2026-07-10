"""Replay a workflow-use workflow against MockMed under one drift mode.

Two product execution modes at the pinned commit:

- ``det``  -- ``Workflow.run()`` (the ``run-workflow`` CLI path): cssSelector
  steps run deterministically, target_text-only steps route through the
  SemanticWorkflowExecutor, ``agent`` steps spin up a browser-use Agent
  (LLM), and the trailing ``extract`` step is a lightweight LLM extraction.
  NOTE: the README-advertised automatic agent FALLBACK on deterministic step
  failure is commented out in code at this commit (since eed1333, Jun 2025);
  a failed deterministic step raises and aborts the run.
- ``noai`` -- ``Workflow.run_with_no_ai()`` (the ``run-workflow-no-ai`` CLI
  path): every step runs through the semantic (visible-text) executor with
  zero LLM calls; extraction steps run without an LLM (their result is
  whatever the executor does without one -- observed and recorded).

The tool's own claimed outcome is the run completing without an exception
(plus any extraction output); the final-state verdict comes exclusively from
the study server's state log via ``verdict.py``.

Run from the workflow-use venv.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mockmed_study_server  # noqa: E402
import study_common  # noqa: E402
import verdict as verdict_mod  # noqa: E402

RUN_TIMEOUT_S = 420


async def replay(
    workflow_path: Path,
    mode: str,
    inputs: dict,
    use_llm: bool,
) -> dict:
    from browser_use import Browser
    from workflow_use.workflow.service import Workflow

    llm = None
    calls = {"n": 0, "in": 0, "out": 0}
    if use_llm:
        from browser_use.llm import ChatAnthropic

        llm = ChatAnthropic(
            model="claude-sonnet-5",
            api_key=study_common.anthropic_key(),
            max_tokens=4096,
        )
        orig_ainvoke = llm.ainvoke

        async def counting_ainvoke(*args, **kwargs):
            result = await orig_ainvoke(*args, **kwargs)
            calls["n"] += 1
            usage = getattr(result, "usage", None)
            if usage is not None:
                # Conservative: prompt_tokens already includes cache reads
                # (billed at 0.1x -- we count them at 1x); cache-creation
                # tokens are billed at 1.25x, counted here at 1.25x.
                cache_create = (
                    getattr(usage, "prompt_cache_creation_tokens", 0) or 0
                )
                calls["in"] += (getattr(usage, "prompt_tokens", 0) or 0) + int(
                    cache_create * 1.25
                )
                calls["out"] += getattr(usage, "completion_tokens", 0) or 0
            return result

        llm.ainvoke = counting_ainvoke  # type: ignore[method-assign]

    browser = Browser(headless=True)
    workflow = Workflow.load_from_file(
        str(workflow_path),
        llm=llm,
        browser=browser,
        page_extraction_llm=llm,
    )
    # Only pass inputs the workflow declares.
    declared = {d.name for d in workflow.inputs_def}
    run_inputs = {k: v for k, v in inputs.items() if k in declared}

    claim = {"claimed": "success", "error": None, "extraction": None}
    started = time.time()
    try:
        if mode == "det":
            output = await asyncio.wait_for(
                workflow.run(inputs=run_inputs), RUN_TIMEOUT_S
            )
        else:
            output = await asyncio.wait_for(
                workflow.run_with_no_ai(inputs=run_inputs), RUN_TIMEOUT_S
            )
        for result in output.step_results:
            extracted = getattr(result, "extracted_content", None)
            if extracted:
                claim["extraction"] = str(extracted)[:2000]
    except Exception as e:  # noqa: BLE001
        claim["claimed"] = "failure"
        claim["error"] = f"{type(e).__name__}: {e}"[:2000]
        traceback.print_exc()
    finally:
        try:
            browser.browser_profile.keep_alive = False
            await browser.stop()
        except Exception:  # noqa: BLE001
            pass
    claim["wall_time_s"] = round(time.time() - started, 1)
    claim["llm_calls"] = calls["n"]
    claim["llm_in_tokens"] = calls["in"]
    claim["llm_out_tokens"] = calls["out"]
    claim["declared_inputs"] = sorted(declared)
    return claim


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--mode", choices=["det", "noai"], required=True)
    parser.add_argument("--drift", default="")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument(
        "--expected-note",
        default=None,
        help="Ground-truth note (defaults to a fresh distinct note passed "
        "as the workflow's note input)",
    )
    args = parser.parse_args()

    tag = f"wfu-{args.mode}-{args.drift or 'baseline'}"
    note = args.expected_note or study_common.run_note(tag)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    state_file = args.out_dir / f"{tag}.state.jsonl"
    if state_file.exists():
        state_file.unlink()

    ledger = study_common.SpendLedger(args.ledger)
    if ledger.total() >= study_common.BUDGET_ABORT_USD:
        raise SystemExit("budget soft cap reached; refusing to run")

    httpd = mockmed_study_server.serve(
        study_common.STUDY_PORT, args.drift, state_file
    )
    try:
        claim = asyncio.run(
            replay(
                args.workflow,
                args.mode,
                {
                    "username": study_common.USERNAME,
                    "password": study_common.PASSWORD,
                    "encounter_note": note,
                    "note": note,
                },
                use_llm=(args.mode == "det"),
            )
        )
    finally:
        time.sleep(1.0)  # allow final beacons to land
        httpd.shutdown()

    v = verdict_mod.classify(state_file, note)
    result = {
        "tool": "workflow-use",
        "mode": args.mode,
        "drift": args.drift or "none",
        "expected_note": note,
        "tool_claim": claim,
        "ground_truth": v.to_dict(),
        "silent_wrong_action": (
            v.verdict == "wrong_action" and claim["claimed"] == "success"
        ),
    }
    out_file = args.out_dir / f"{tag}.result.json"
    out_file.write_text(json.dumps(result, indent=2))
    if claim["llm_calls"]:
        ledger.add(
            f"workflow-use replay {tag}",
            claim["llm_calls"],
            claim["llm_in_tokens"] or None,
            claim["llm_out_tokens"] or None,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

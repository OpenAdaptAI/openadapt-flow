"""Run the MockMed workflow on a local Skyvern server under one drift mode.

Skyvern's deterministic-replay surface is workflow-level ``run_with``:

- ``run_with=code`` with no cached script -> a ``code_generation`` run: the
  AI agent executes the task and records a cached Playwright script.
- subsequent ``run_with=code`` runs replay the cached script, with
  ``ai_fallback`` / script self-healing when the script fails.
- ``run_with=agent`` forces a pure agent run.

The tool's claimed outcome is the workflow run's terminal status (plus its
failure_reason); the final-state verdict comes exclusively from the study
server's state log. LLM spend is counted from the Skyvern server log (every
LLM call logs its token usage) between run start and end markers.

Requires: the Skyvern server already running (see COMPETITOR_STUDY.md),
``SKYVERN_HOME`` containing its .env.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mockmed_study_server  # noqa: E402
import study_common  # noqa: E402
import verdict as verdict_mod  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SKYVERN_VENV = (
    REPO / "runs" / "competitor_study" / "third_party" / "skyvern" / ".venv"
)
SKYVERN_HOME = REPO / "runs" / "competitor_study" / "evidence" / "skyvern_home"


def cli(*args: str, timeout: int = 900) -> dict:
    out = subprocess.run(
        [str(SKYVERN_VENV / "bin" / "skyvern"), *args, "--json"],
        cwd=SKYVERN_HOME,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    # The CLI prints log lines before the JSON payload; JSON starts at '{'.
    text = out.stdout
    start = text.find("{")
    if start < 0:
        raise RuntimeError(f"no JSON in CLI output: {text[-500:]} {out.stderr[-500:]}")
    return json.loads(text[start:])


def db_token_totals() -> dict:
    """Total LLM token counts recorded by Skyvern in its SQLite DB.

    Skyvern persists per-LLM-call token usage on ``steps`` (agent steps),
    ``observer_thoughts`` and ``ai_suggestions``-style rows. Summing the
    token-count columns across all tables that carry them gives an exact
    running total; per-run usage is the before/after delta.
    """
    import sqlite3

    db = SKYVERN_HOME / "skyvern.db"
    totals = {"rows": 0, "in": 0, "out": 0}
    if not db.exists():
        return totals
    conn = sqlite3.connect(db)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        for table in tables:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if {"input_token_count", "output_token_count"} <= cols:
                row = conn.execute(
                    f"SELECT COUNT(*), COALESCE(SUM(input_token_count),0),"
                    f" COALESCE(SUM(output_token_count),0) FROM {table}"
                    " WHERE input_token_count > 0 OR output_token_count > 0"
                ).fetchone()
                totals["rows"] += row[0]
                totals["in"] += row[1]
                totals["out"] += row[2]
    finally:
        conn.close()
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--drift", default="")
    parser.add_argument("--run-with", default=None, choices=[None, "agent", "code"])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    note = study_common.run_note(args.tag)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    state_file = args.out_dir / f"{args.tag}.state.jsonl"
    if state_file.exists():
        state_file.unlink()

    ledger = study_common.SpendLedger(args.ledger)
    if ledger.total() >= study_common.BUDGET_ABORT_USD:
        raise SystemExit("budget soft cap reached; refusing to run")

    tokens_before = db_token_totals()

    httpd = mockmed_study_server.serve(
        study_common.STUDY_PORT, args.drift, state_file
    )
    started = time.time()
    try:
        run_args = [
            "workflow", "run",
            "--id", args.workflow_id,
            "--params", json.dumps({"note": note}),
            "--wait", "--timeout", str(args.timeout),
        ]
        if args.run_with:
            run_args += ["--run-with", args.run_with]
        run_result = cli(*run_args, timeout=args.timeout + 120)
    finally:
        time.sleep(1.5)
        httpd.shutdown()
    wall = round(time.time() - started, 1)

    data = run_result.get("data") or {}
    status = data.get("status") or data.get("run_status") or "unknown"
    tokens_after = db_token_totals()
    usage = {
        "calls": tokens_after["rows"] - tokens_before["rows"],
        "in": tokens_after["in"] - tokens_before["in"],
        "out": tokens_after["out"] - tokens_before["out"],
    }
    v = verdict_mod.classify(state_file, note)
    claimed = "success" if status in ("completed",) else f"failure ({status})"
    result = {
        "tool": "skyvern",
        "tag": args.tag,
        "drift": args.drift or "none",
        "run_with": args.run_with,
        "expected_note": note,
        "tool_claim": {
            "claimed": claimed,
            "status": status,
            "failure_reason": data.get("failure_reason"),
            "run_id": data.get("run_id") or data.get("workflow_run_id"),
            "wall_time_s": wall,
            "llm_calls": usage["calls"],
            "llm_in_tokens": usage["in"],
            "llm_out_tokens": usage["out"],
        },
        "ground_truth": v.to_dict(),
        "silent_wrong_action": (
            v.verdict == "wrong_action" and status == "completed"
        ),
    }
    out_file = args.out_dir / f"{args.tag}.result.json"
    out_file.write_text(json.dumps(result, indent=2))
    if usage["calls"]:
        ledger.add(
            f"skyvern {args.tag}",
            usage["calls"],
            usage["in"] or None,
            usage["out"] or None,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

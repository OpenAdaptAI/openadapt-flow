"""Replay the UNEDITED Playwright-codegen script under one drift mode.

The no-AI incumbent's floor: the script `playwright codegen` emitted for the
canonical MockMed task (see ``codegen_record.js``), executed byte-for-byte
under each drift mode. The tool's "claim" is the script's exit status; the
final-state verdict comes from the study server's state log. $0, no LLM.
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--drift", default="")
    parser.add_argument("--expected-note", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    tag = f"codegen-{args.drift or 'baseline'}"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    state_file = args.out_dir / f"{tag}.state.jsonl"
    if state_file.exists():
        state_file.unlink()

    httpd = mockmed_study_server.serve(
        study_common.STUDY_PORT, args.drift, state_file
    )
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(args.script)],
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        time.sleep(1.0)
        httpd.shutdown()
    wall = round(time.time() - started, 1)

    claimed = "success" if proc.returncode == 0 else "failure"
    v = verdict_mod.classify(state_file, args.expected_note)
    result = {
        "tool": "playwright-codegen",
        "drift": args.drift or "none",
        "expected_note": args.expected_note,
        "tool_claim": {
            "claimed": claimed,
            "exit_code": proc.returncode,
            "error": proc.stderr.strip()[-600:] or None,
            "wall_time_s": wall,
            "llm_calls": 0,
        },
        "ground_truth": v.to_dict(),
        "silent_wrong_action": (
            v.verdict == "wrong_action" and claimed == "success"
        ),
    }
    (args.out_dir / f"{tag}.result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

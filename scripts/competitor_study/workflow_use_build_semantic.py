"""Build workflow-use's SEMANTIC (no-AI) workflow from the MockMed recording.

Runs the pinned commit's own converter --
``cli._convert_recording_to_semantic_workflow`` (the core of the
``build-semantic-from-recording`` CLI command, "optimized for no-AI
execution"). Zero LLM calls; the converter opens the tool's browser against
the live (drift-free) study app to extract semantic element mappings.

Adaptations (documented in COMPETITOR_STUDY.md): the CLI module initializes
``ChatBrowserUse`` at import time, which requires a Browser-Use cloud key;
we set a dummy ``BROWSER_USE_API_KEY`` so the import succeeds -- the LLM is
never invoked on this path -- and we call the converter directly instead of
answering the CLI's interactive prompts.

Run from the workflow-use venv.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mockmed_study_server  # noqa: E402
import study_common  # noqa: E402

WORKFLOWS_DIR = (
    Path(__file__).resolve().parents[2]
    / "runs" / "competitor_study" / "third_party" / "workflow-use" / "workflows"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args()

    os.environ.setdefault("BROWSER_USE_API_KEY", "dummy-never-invoked")
    sys.path.insert(0, str(WORKFLOWS_DIR))
    httpd = mockmed_study_server.serve(
        study_common.STUDY_PORT, "", args.state_file
    )
    try:
        import cli as wfu_cli  # noqa: PLC0415

        # Shim: cli.py at the pinned commit calls ``browser.close()`` but the
        # pinned browser-use (0.9.5) renamed it to ``stop()``. Pure rename
        # (upstream bitrot), no behavior change; recorded as a finding.
        from browser_use import Browser  # noqa: PLC0415

        if not hasattr(Browser, "close"):
            Browser.close = Browser.stop  # type: ignore[attr-defined]

        recording = json.loads(args.recording.read_text())
        workflow = asyncio.run(
            wfu_cli._convert_recording_to_semantic_workflow(
                recording,
                study_common.USER_GOAL,
                simulate_interactions=True,
            )
        )
    finally:
        httpd.shutdown()

    data = (
        workflow.model_dump(mode="json")
        if hasattr(workflow, "model_dump")
        else workflow
    )
    args.out.write_text(json.dumps(data, indent=2))
    print(f"semantic workflow saved: {args.out}")
    for i, s in enumerate(data.get("steps", [])):
        print(f"  step {i}: {json.dumps({k: v for k, v in s.items() if v is not None})[:220]}")
    print("input_schema:", json.dumps(data.get("input_schema")))


if __name__ == "__main__":
    main()

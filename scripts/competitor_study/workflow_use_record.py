"""Record the canonical MockMed task with workflow-use's own recorder extension.

workflow-use's documented recording flow (``python cli.py create-workflow``)
launches a browser with their recorder extension and waits for a HUMAN to
perform the task; the extension streams semantic workflow updates to a local
event server on port 7331 (see ``workflow_use/recorder/service.py`` and
``extension/src/entrypoints/background.ts`` at the pinned commit).

This script reproduces that flow with a SCRIPTED human: it runs the same
event receiver contract on port 7331, launches Chromium with the SAME built
extension (``extension/.output/chrome-mv3``), performs the canonical task via
paced Playwright input (CDP-injected events are trusted, so the extension's
content script records them exactly as it would a person), and saves the
extension's final workflow payload -- byte-identical in format to what
``create-workflow`` captures -- as the recording JSON.

$0 / zero LLM calls. Run from the workflow-use venv:
    third_party/workflow-use/workflows/.venv/bin/python \
        scripts/competitor_study/workflow_use_record.py --out recording.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mockmed_study_server  # noqa: E402
import study_common  # noqa: E402

STUDY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_USE_ROOT = (
    STUDY_ROOT / "runs" / "competitor_study" / "third_party" / "workflow-use"
)
EXT_DIR = WORKFLOW_USE_ROOT / "extension" / ".output" / "chrome-mv3"

RECORDER_PORT = 7331  # hardcoded in the extension's background script


class _Receiver:
    """Minimal stand-in for RecordingService's event server (same contract)."""

    def __init__(self) -> None:
        self.last_workflow = None
        self.events = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    event = json.loads(body)
                except json.JSONDecodeError:
                    event = None
                if event:
                    outer.events.append(event)
                    if event.get("type") == "WORKFLOW_UPDATE":
                        outer.last_workflow = event.get("payload")
                self.send_response(202)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", RECORDER_PORT), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


async def record(out_path: Path, note_text: str, user_data_dir: Path) -> None:
    from playwright.async_api import async_playwright

    receiver = _Receiver()
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=[
                f"--disable-extensions-except={EXT_DIR}",
                f"--load-extension={EXT_DIR}",
                "--no-first-run",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.wait_for_timeout(2000)  # let the extension initialize
        await study_common.perform_canonical_task(page, note_text)
        await page.wait_for_timeout(3000)  # let the extension flush updates
        await context.close()

    receiver.httpd.shutdown()
    if receiver.last_workflow is None:
        raise RuntimeError(
            f"extension sent no WORKFLOW_UPDATE ({len(receiver.events)} "
            "events received)"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(receiver.last_workflow, indent=2))
    steps = receiver.last_workflow.get("steps", [])
    print(f"recording saved: {out_path} ({len(steps)} steps)")
    for i, s in enumerate(steps):
        print(
            f"  step {i}: {s.get('type')} target_text={s.get('target_text')!r}"
            f" cssSelector={s.get('cssSelector')!r} url={s.get('url')!r}"
            f" value={s.get('value')!r}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--note", default=None)
    args = parser.parse_args()

    note = args.note or study_common.run_note("wfu-record")
    httpd = mockmed_study_server.serve(
        study_common.STUDY_PORT, "", args.state_file
    )
    user_data = args.out.parent / "record_user_data"
    try:
        asyncio.run(record(args.out, note, user_data))
    finally:
        httpd.shutdown()
    (args.out.parent / "record_note.txt").write_text(note)
    print(f"note used during recording: {note!r}")


if __name__ == "__main__":
    main()

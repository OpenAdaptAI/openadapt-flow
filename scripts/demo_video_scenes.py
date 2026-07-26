"""Deterministic scene runner for the OpenAdapt "verified execution" demo video.

Drives every on-screen sequence of the failure-first demo reproducibly against
the bundled MockMed reference application (synthetic data, localhost only,
zero model calls), so anyone can regenerate the exact footage the video shows:

    naive     - a position-based automation on a lookalike-drifted queue writes
                to the WRONG patient and still reports success; the system-of-
                record snapshot (GET /api/db) proves the wrong row changed.
    record    - the real Recorder films a human-style demonstration of the
                triage-note task (the same flow the public evidence pack uses).
    compile   - the recording compiles into a workflow bundle; the scene emits
                the program-graph HTML and films it, and prints the compiled
                steps, the mined ``note`` parameter, and the derived
                system-of-record effect contract.
    verified  - the compiled bundle replays deterministically (structural
                targeting, zero model calls) with an out-of-band
                ``RestRecordVerifier`` bound to the system of record; the
                run reports VERIFIED and the DB snapshot proves the RIGHT
                patient's record changed.
    halt      - the SAME lookalike drift from the cold open is injected; the
                governed replay's identity gate refuses to act, the run HALTS
                with an evidence report, and the DB snapshot proves NOTHING
                was written.

Every scene writes into ``--out``:

    <scene>.webm              raw browser capture (Playwright context video)
    <scene>.transcript.jsonl  timestamped terminal lines (t seconds from the
                              moment the scene's browser capture starts), the
                              source of truth for the video's terminal panel
    <scene>.*.json            machine-checkable proofs (DB snapshots, run
                              outcome), so no on-screen claim is hand-authored

This is a presentation driver over the SHIPPED product paths - the same
Recorder / compile_recording / Replayer / RestRecordVerifier wiring the
public evidence pack (``scripts/export_public_demo_evidence.py``) validates.
Nothing here alters runtime behavior; the only "actor" code is the NAIVE arm,
which exists to demonstrate the failure class OpenAdapt exists to prevent.

Usage::

    python -m scripts.demo_video_scenes --out demo-video-out
    python -m scripts.demo_video_scenes --out demo-video-out --scenes naive,halt

Requires the dev environment (playwright chromium installed). Headless by
default; ``--headed`` shows the browser live.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.request import Request, urlopen

# The exact note typed in the demonstration and replayed as the bound ``note``
# parameter. Deterministic on purpose: the DB proofs assert on it.
NOTE = "Follow-up in 2 weeks; BP recheck."

# The demonstration targets Jane Sample (patient id ``p1``), the first referral
# row at record time. Under ``?drift=lookalike`` the fixture inserts Taylor
# Duplicate (``p0``) directly above with the same reason and priority - the
# wrong-entity trap. The roster ships with the fixture
# (``openadapt_flow/mockmed/static/app.js``); mirrored here only to caption
# DB rows with a display name.
PATIENT_NAMES = {
    "p0": "Taylor Duplicate",
    "p1": "Jane Sample",
    "p2": "Alex Testcase",
    "p3": "Sam Specimen",
}
INTENDED_PATIENT = "p1"

QUERY_CLEAN = "?fault=ok&idempotency=demo"
QUERY_LOOKALIKE = "?fault=ok&drift=lookalike&idempotency=demo"

SCENES = ("naive", "record", "compile", "verified", "halt")

VIEWPORT = {"width": 1280, "height": 800}


class Transcript:
    """Print terminal lines and journal them with scene-relative timestamps.

    ``t`` is seconds since :meth:`start` (called when the scene's browser
    capture begins), so the video edit can sync the terminal panel to the
    browser footage without guessing.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._t0: Optional[float] = None
        self._lines: list[dict[str, Any]] = []

    def start(self) -> None:
        self._t0 = time.monotonic()

    def say(self, line: str = "") -> None:
        t = 0.0 if self._t0 is None else time.monotonic() - self._t0
        print(line, flush=True)
        self._lines.append({"t": round(t, 3), "line": line})

    def close(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as fh:
            for row in self._lines:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _http_json(url: str, *, method: str = "GET", body: Any = None) -> Any:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, method=method, headers=headers)
    with urlopen(req, timeout=10.0) as resp:  # noqa: S310 - localhost only
        return json.loads(resp.read().decode("utf-8"))


def _db_snapshot(base_url: str) -> dict[str, Any]:
    return _http_json(f"{base_url}api/db")


def _reset(base_url: str) -> None:
    _http_json(f"{base_url}api/reset", method="POST", body={})


def _newest_webm(directory: Path) -> Path:
    webms = sorted(directory.glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if not webms:
        raise RuntimeError(f"no video captured in {directory}")
    return webms[-1]


def _finish_video(video_tmp: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = _newest_webm(video_tmp)
    if target.exists():
        target.unlink()
    src.rename(target)
    for leftover in video_tmp.glob("*.webm"):
        leftover.unlink()
    return target


def _print_db_proof(tr: Transcript, snapshot: dict[str, Any], *, intended: str) -> None:
    """Render the system-of-record snapshot as the on-camera proof block."""
    records = snapshot.get("records", [])
    tr.say("$ curl -s http://127.0.0.1:PORT/api/db   # system of record")
    if not records:
        tr.say("  (no rows written)")
    for rec in records:
        pid = str(rec.get("patient_id", "?"))
        name = PATIENT_NAMES.get(pid, "unknown")
        marker = "INTENDED" if pid == intended else "WRONG PATIENT"
        tr.say(
            f"  row id={rec.get('id')} patient_id={pid} ({name})"
            f" type={rec.get('type')} note={json.dumps(rec.get('note', ''))}"
            f"  <- {marker}"
        )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# -- scene 1: the naive arm (the failure class) ------------------------------


def scene_naive(base_url: str, out: Path, *, headed: bool) -> None:
    """A position-based automation on the lookalike-drifted queue.

    This is the deliberately NAIVE arm: it replays fixed positional targets
    ("open the first referral row") exactly like a recorded macro or a
    selector-free RPA script, and it judges success from the on-screen banner.
    Under ``?drift=lookalike`` the first row is no longer the demonstrated
    patient - the script writes the note to the WRONG patient and still
    reports success. The DB snapshot is the proof.
    """
    from playwright.sync_api import sync_playwright

    tr = Transcript(out / "naive.transcript.jsonl")
    _reset(base_url)
    video_tmp = out / ".naive-video"
    video_tmp.mkdir(parents=True, exist_ok=True)
    entry = f"{base_url}{QUERY_LOOKALIKE}#tasks"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(video_tmp),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        tr.start()
        page.goto(entry)
        tr.say("$ python naive_macro.py   # position-based, screen-judged")
        tr.say("[naive] open referral queue")
        page.wait_for_selector(".open-btn", state="visible")
        page.wait_for_timeout(1200)
        tr.say('[naive] click first row "Open" (recorded position: row 1)')
        page.locator(".open-btn").first.click()
        page.wait_for_timeout(900)
        tr.say("[naive] new encounter -> Triage")
        page.locator("#new-encounter").click()
        page.wait_for_timeout(700)
        page.locator("#type-triage").click()
        page.wait_for_timeout(500)
        tr.say(f"[naive] type note: {NOTE!r}")
        page.locator("#note").click()
        page.locator("#note").type(NOTE, delay=28)
        page.wait_for_timeout(400)
        tr.say("[naive] click Save Encounter")
        page.locator("#save-encounter").click()
        page.wait_for_selector("#saved-banner", state="visible")
        banner = page.locator("#saved-banner").inner_text()
        page.wait_for_timeout(1500)
        tr.say(f"[naive] screen says: {banner!r}")
        tr.say("[naive] exit 0 -- SUCCESS (according to the screen)")
        tr.say("")
        snapshot = _db_snapshot(base_url)
        _print_db_proof(tr, snapshot, intended=INTENDED_PATIENT)
        wrote_wrong = any(
            r.get("source") == "replay" and r.get("patient_id") != INTENDED_PATIENT
            for r in snapshot.get("records", [])
        )
        tr.say("")
        tr.say(
            "RESULT: the automation reported success; the system of record"
            " shows the note on the WRONG patient."
            if wrote_wrong
            else "RESULT: unexpected - naive arm did not miswrite"
        )
        page.wait_for_timeout(1200)
        context.close()
        browser.close()
    _finish_video(video_tmp, out / "naive.webm")
    _write_json(
        out / "naive.db_proof.json",
        {
            "arm": "naive-positional",
            "entry_url_query": QUERY_LOOKALIKE,
            "screen_reported_success": True,
            "intended_patient": INTENDED_PATIENT,
            "wrote_wrong_patient": wrote_wrong,
            "db": snapshot,
        },
    )
    tr.close()
    if not wrote_wrong:
        raise RuntimeError("naive arm did not reproduce the wrong-patient write")


# -- scene 2: record the demonstration ---------------------------------------


def scene_record(base_url: str, out: Path, work: Path, *, headed: bool) -> Path:
    """Record the canonical triage demonstration through the real Recorder."""
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.recorder import Recorder

    tr = Transcript(out / "record.transcript.jsonl")
    _reset(base_url)
    recording_dir = work / "recording"
    video_tmp = out / ".record-video"
    video_tmp.mkdir(parents=True, exist_ok=True)
    entry = f"{base_url}{QUERY_CLEAN}#tasks"

    def records_reader() -> list[dict[str, Any]]:
        return list(_db_snapshot(base_url).get("records", []))

    backend, close = PlaywrightBackend.launch(
        entry, headless=not headed, record_video_dir=str(video_tmp)
    )
    try:
        page = backend.page
        tr.start()
        tr.say("$ openadapt-flow record --url $MOCKMED  # demonstrate once")

        def center(selector: str) -> tuple[int, int]:
            locator = page.locator(selector).first
            locator.wait_for(state="visible")
            box = locator.bounding_box()
            assert box is not None
            return (
                int(box["x"] + box["width"] / 2),
                int(box["y"] + box["height"] / 2),
            )

        recorder = Recorder(
            backend,
            recording_dir,
            app_url=entry,
            system_of_record_reader=records_reader,
        )
        page.wait_for_timeout(900)
        tr.say("[record] click Open on Jane Sample's referral")
        recorder.click(*center(".open-btn"))
        page.wait_for_timeout(700)
        tr.say("[record] click New Encounter")
        recorder.click(*center("#new-encounter"))
        page.wait_for_timeout(600)
        tr.say("[record] choose type: Triage")
        recorder.click(*center("#type-triage"))
        page.wait_for_timeout(500)
        tr.say("[record] click Note field")
        recorder.click(*center("#note-label"))
        tr.say(f"[record] type note (captured as parameter 'note'): {NOTE!r}")
        recorder.type_text(NOTE, param="note")
        page.wait_for_timeout(400)
        tr.say("[record] click Save Encounter")
        recorder.click(*center("#save-encounter"))
        page.wait_for_selector("#saved-banner", state="visible")
        page.wait_for_timeout(1200)
        recorder.finish()
        tr.say(f"[record] demonstration saved -> {recording_dir.name}/")
    finally:
        close()
    _finish_video(video_tmp, out / "record.webm")
    tr.close()
    return recording_dir


# -- scene 3: compile ---------------------------------------------------------


def scene_compile(recording_dir: Path, out: Path, work: Path, *, headed: bool) -> Path:
    """Compile the recording; film the program graph; print the contract."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.visualize import build_program_graph, render_html

    tr = Transcript(out / "compile.transcript.jsonl")
    bundle_dir = work / "bundle"
    tr.say("$ openadapt-flow compile recording/ -o bundle/")
    workflow = compile_recording(
        recording_dir, bundle_dir, name="mockmed-triage-note", mine_effects=True
    )
    tr.say(f"[compile] {len(workflow.steps)} steps compiled; zero model calls")
    for step in workflow.steps:
        risk = f" risk={step.risk}" if step.risk == "irreversible" else ""
        effects = (
            " effects=[" + ", ".join(e.kind.value for e in step.effects) + "]"
            if step.effects
            else ""
        )
        tr.say(f"  {step.id}: {step.intent}{risk}{effects}")
    params = sorted({s.param for s in workflow.steps if getattr(s, "param", None)})
    tr.say(f"[compile] parameters: {params or ['note']}")
    tr.say(
        "[compile] consequential save compiled with a system-of-record"
        " effect contract (record_written + field_equals)"
    )

    graph_html = out / "compile.graph.html"
    graph_html.write_text(
        render_html(build_program_graph(workflow), title="mockmed-triage-note"),
        encoding="utf-8",
    )

    # Film a short beauty pass over the compiled program graph.
    from playwright.sync_api import sync_playwright

    video_tmp = out / ".compile-video"
    video_tmp.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(video_tmp),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        tr.start()
        page.goto(graph_html.as_uri())
        page.wait_for_timeout(2500)
        page.mouse.wheel(0, 300)
        page.wait_for_timeout(1800)
        page.mouse.wheel(0, 300)
        page.wait_for_timeout(1800)
        context.close()
        browser.close()
    _finish_video(video_tmp, out / "compile.webm")
    tr.close()
    return bundle_dir


# -- governed replays ---------------------------------------------------------


def _governed_replay(
    base_url: str,
    bundle_dir: Path,
    run_dir: Path,
    video_tmp: Path,
    *,
    query: str,
    use_structural: bool,
    headed: bool,
    tr: Transcript,
    hold_ms: int = 1800,
):
    """One governed replay through the real Replayer + out-of-band verifier.

    Mirrors the public evidence pack's wiring
    (``scripts/export_public_demo_evidence.py``): the fail-closed run gate
    authorizes the bundle under the Standard execution profile, so a clean
    run earns the evidence-qualified ``VERIFIED`` outcome rather than a
    screen-only completion.
    """
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.deployment import DeploymentConfig, PolicySection
    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        execution_profile_contract,
    )
    from openadapt_flow.ir import Workflow
    from openadapt_flow.report import render_run_report
    from openadapt_flow.run_gate import (
        build_runtime_authorization,
        evaluate_run_gate,
    )
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.effects import RestRecordVerifier

    workflow = Workflow.load(bundle_dir)
    entry = f"{base_url}{query}#tasks"
    video_tmp.mkdir(parents=True, exist_ok=True)
    backend, close = PlaywrightBackend.launch(
        entry, headless=not headed, record_video_dir=str(video_tmp)
    )
    try:
        tr.start()
        verifier = RestRecordVerifier(
            base_url,
            records_path="/api/db",
            records_key="records",
            timeout_s=1.0,
            poll_interval_s=0.05,
        )
        gate = evaluate_run_gate(
            workflow,
            bundle_dir=bundle_dir,
            deployment=DeploymentConfig(policy=PolicySection(policy="clinical-write")),
            effect_verifier=verifier,
            profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
            effective_durable=True,
            effective_require_settled=True,
        )
        if not gate.passed:
            raise RuntimeError(gate.render())
        authorization = build_runtime_authorization(
            workflow,
            gate,
            approval_source="demo-video-scene-runner",
            params={"note": NOTE},
        )
        report = Replayer(
            backend,
            effect_verifier=verifier,
            governed_authorization=authorization,
            durable=True,
            require_settled=True,
            use_structural=use_structural,
        ).run(
            workflow.model_copy(deep=True),
            params={"note": NOTE},
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            execution_target_kind="web",
            execution_origin=base_url.rstrip("/"),
            execution_entry_url=entry,
        )
        backend.page.wait_for_timeout(hold_ms)
    finally:
        close()
    render_run_report(run_dir)
    return report


def scene_verified(
    base_url: str, bundle_dir: Path, out: Path, work: Path, *, headed: bool
) -> None:
    """Deterministic replay + out-of-band verification on the clean app."""
    tr = Transcript(out / "verified.transcript.jsonl")
    _reset(base_url)
    tr.say("$ openadapt-flow replay bundle/ --param note=... \\")
    tr.say("      --verify rest:/api/db   # out-of-band system-of-record oracle")
    run_dir = work / "run-verified"
    report = _governed_replay(
        base_url,
        bundle_dir,
        run_dir,
        out / ".verified-video",
        query=QUERY_CLEAN,
        use_structural=True,
        headed=headed,
        tr=tr,
    )
    snapshot = _db_snapshot(base_url)
    tr.say(f"[replay] outcome: {report.execution_outcome}")
    tr.say(f"[replay] model calls: {report.model_calls} (deterministic replay)")
    tr.say(
        "[replay] effect verifier: CONFIRMED against the system of record"
        " (an independent read, not the screen)"
    )
    tr.say("")
    _print_db_proof(tr, snapshot, intended=INTENDED_PATIENT)
    right = [
        r
        for r in snapshot.get("records", [])
        if r.get("source") == "replay"
        and r.get("patient_id") == INTENDED_PATIENT
        and r.get("note") == NOTE
    ]
    tr.say("")
    tr.say(
        "RESULT: exactly one row, on the intended patient, with the intended"
        " note - independently confirmed."
        if len(right) == 1
        else "RESULT: unexpected system-of-record state"
    )
    _finish_video(out / ".verified-video", out / "verified.webm")
    _write_json(
        out / "verified.outcome.json",
        {
            "arm": "governed-structural",
            "entry_url_query": QUERY_CLEAN,
            "execution_outcome": report.execution_outcome,
            "model_calls": report.model_calls,
            "intended_patient": INTENDED_PATIENT,
            "db": snapshot,
        },
    )
    tr.close()
    if report.execution_outcome != "VERIFIED" or len(right) != 1:
        raise RuntimeError("verified scene did not reproduce VERIFIED outcome")


def scene_halt(
    base_url: str, bundle_dir: Path, out: Path, work: Path, *, headed: bool
) -> None:
    """Same lookalike drift as the cold open; the governed run must HALT."""
    tr = Transcript(out / "halt.transcript.jsonl")
    _reset(base_url)
    tr.say("$ openadapt-flow replay bundle/ --param note=... \\")
    tr.say("      --verify rest:/api/db   # same drifted queue as the cold open")
    run_dir = work / "run-halt"
    report = _governed_replay(
        base_url,
        bundle_dir,
        run_dir,
        out / ".halt-video",
        query=QUERY_LOOKALIKE,
        use_structural=False,  # the pixel/identity path the drift attacks
        headed=headed,
        tr=tr,
        hold_ms=4200,  # hold on the refused queue so the halt is visible
    )
    snapshot = _db_snapshot(base_url)
    tr.say(f"[replay] outcome: {report.execution_outcome}")
    halt = report.halt
    if halt is not None:
        tr.say(f"[replay] halted at: {halt.intent or halt.state_id}")
        tr.say(f"[replay] reason: {halt.reason}")
    tr.say("")
    _print_db_proof(tr, snapshot, intended=INTENDED_PATIENT)
    replay_rows = [
        r for r in snapshot.get("records", []) if r.get("source") == "replay"
    ]
    tr.say("")
    tr.say(
        "RESULT: identity could not be proven, so the run STOPPED."
        " Nothing was written. Evidence report saved."
        if report.execution_outcome == "HALTED" and not replay_rows
        else "RESULT: unexpected outcome"
    )
    _finish_video(out / ".halt-video", out / "halt.webm")

    report_md = run_dir / "REPORT.md"
    evidence_html = out / "halt.evidence.html"
    _render_report_page(report_md, evidence_html)
    _film_scroll(
        evidence_html,
        out / ".halt-report-video",
        out / "halt_report.webm",
        headed=headed,
    )
    _write_json(
        out / "halt.outcome.json",
        {
            "arm": "governed-pixel-identity",
            "entry_url_query": QUERY_LOOKALIKE,
            "execution_outcome": report.execution_outcome,
            "model_calls": report.model_calls,
            "halt_reason": halt.reason if halt is not None else None,
            "rows_written_by_replay": len(replay_rows),
            "db": snapshot,
        },
    )
    tr.close()
    if report.execution_outcome != "HALTED" or replay_rows:
        raise RuntimeError("halt scene did not reproduce a clean HALT")


def _render_report_page(report_md: Path, out_html: Path) -> None:
    """Wrap the REAL run REPORT.md in a minimal page for on-camera scrolling.

    Presentation-only: the report text is shown verbatim, not edited.
    """
    text = (
        report_md.read_text(encoding="utf-8")
        if report_md.exists()
        else ("(REPORT.md was not generated)")
    )
    out_html.write_text(
        "<title>Run evidence report</title>"
        "<style>body{background:#0e121b;color:#e8ecf4;font:14px/1.55 "
        "ui-monospace,SFMono-Regular,Menlo,monospace;max-width:960px;"
        "margin:40px auto;padding:0 24px;white-space:pre-wrap}</style>"
        "<body>" + text.replace("&", "&amp;").replace("<", "&lt;") + "</body>",
        encoding="utf-8",
    )


def _film_scroll(html: Path, video_tmp: Path, target: Path, *, headed: bool) -> None:
    from playwright.sync_api import sync_playwright

    video_tmp.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(video_tmp),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        page.goto(html.as_uri())
        page.wait_for_timeout(2200)
        for _ in range(10):
            page.mouse.wheel(0, 260)
            page.wait_for_timeout(650)
        page.wait_for_timeout(1500)
        context.close()
        browser.close()
    _finish_video(video_tmp, target)


# -- entry point --------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, required=True, help="output directory for raw clips"
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=None,
        help="working directory for recording/bundle/runs (default: <out>/work)",
    )
    parser.add_argument(
        "--scenes",
        default="all",
        help=f"comma list from {','.join(SCENES)} (default all)",
    )
    parser.add_argument(
        "--headed", action="store_true", help="show the browser while filming"
    )
    args = parser.parse_args(argv)

    out: Path = args.out
    work: Path = args.work or out / "work"
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    want = SCENES if args.scenes == "all" else tuple(args.scenes.split(","))
    unknown = [s for s in want if s not in SCENES]
    if unknown:
        parser.error(f"unknown scenes: {unknown}")

    from openadapt_flow.mockmed.fault_server import serve

    base_url, _db, stop = serve()
    print(f"[demo] MockMed fault server: {base_url} (synthetic data, localhost)")
    try:
        recording_dir = work / "recording"
        bundle_dir = work / "bundle"
        if "naive" in want:
            print("\n=== scene: naive (the failure class) ===")
            scene_naive(base_url, out, headed=args.headed)
        if "record" in want:
            print("\n=== scene: record ===")
            recording_dir = scene_record(base_url, out, work, headed=args.headed)
        if "compile" in want:
            print("\n=== scene: compile ===")
            bundle_dir = scene_compile(recording_dir, out, work, headed=args.headed)
        if "verified" in want:
            print("\n=== scene: verified replay ===")
            scene_verified(base_url, bundle_dir, out, work, headed=args.headed)
        if "halt" in want:
            print("\n=== scene: halt (same drift, governed) ===")
            scene_halt(base_url, bundle_dir, out, work, headed=args.headed)
    finally:
        stop()
    print(f"\n[demo] raw clips + transcripts + proofs -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Operator console: read-only projections, governed actions, loopback boot.

Behavior under test (import-light -- no browser, no OCR):

1. The GET surface is a faithful projection of REAL engine artifacts: bundles
   written by ``Workflow.save``, run dirs written by ``RunReport.save`` +
   ``CheckpointStore``, and skill libraries written by ``SkillLibrary``.
2. Coverage numbers come from the same policy helpers the CLI uses
   (identity armed / effect contracts on irreversible steps) and degrade to
   None -- never a fabricated 100% -- when a bundle has no consequential step.
3. Mutating endpoints are FAIL-CLOSED: read-only servers refuse with 403 and
   the exact CLI command; non-executable verbs (teach) are never executed;
   artifact serving refuses path traversal.
4. With actions enabled, ``approve`` shells out to the real CLI verb and
   ``promote``/``rollback`` go through the same ``SkillLibrary`` entry points
   the teach pipeline uses.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from openadapt_flow.console.app import create_app  # noqa: E402
from openadapt_flow.ir import (  # noqa: E402
    ActionKind,
    Anchor,
    HaltObservation,
    IdentityCheck,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.learning.library import SkillLibrary  # noqa: E402
from openadapt_flow.runtime.durable.checkpoint import (  # noqa: E402
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)
from openadapt_flow.runtime.effects import Effect, EffectKind  # noqa: E402

# ---------------------------------------------------------------------------
# fixtures: a real bundle, a halted run, a paused run, a skill library
# ---------------------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\nfake-crop-bytes"


def _armed_click(step_id: str, *, risk: str = "reversible", effects=()) -> Step:
    return Step(
        id=step_id,
        intent=f"click {step_id}",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/btn.png",
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text="Save",
            context_text="Jane Roe 1980-01-01 MRN 12345",
        ),
        risk=risk,
        effects=list(effects),
    )


def _unarmed_click(step_id: str) -> Step:
    return Step(
        id=step_id,
        intent=f"click {step_id}",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/btn.png",
            region=(10, 10, 40, 18),
            click_point=(20, 15),
        ),
        identity_armed=False,
        identity_unarmed_reason="no identity context recorded at compile time",
    )


def _make_bundle(root: Path, dirname: str, *, extra_step: bool = False) -> Path:
    b = root / dirname
    (b / "templates").mkdir(parents=True)
    (b / "templates" / "btn.png").write_bytes(_PNG)
    steps = [
        _armed_click(
            "s1",
            risk="irreversible",
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={"patient_id": "p1"},
                )
            ],
        ),
        _unarmed_click("s2"),
        Step(id="s3", intent="press enter", action=ActionKind.KEY, key="Enter"),
    ]
    if extra_step:
        steps.append(
            Step(id="s4", intent="press tab", action=ActionKind.KEY, key="Tab")
        )
    wf = Workflow(name="triage-note", steps=steps, params={"note": "hello"})
    wf.save(b)
    return b


def _make_halted_run(runs_root: Path, dirname: str) -> Path:
    run = runs_root / dirname
    run.mkdir(parents=True)
    (run / "s1_before.png").write_bytes(_PNG)
    (run / "s1_after.png").write_bytes(_PNG)
    report = RunReport(
        workflow_name="triage-note",
        started_at="2026-07-17T10:00:00+00:00",
        success=False,
        results=[
            StepResult(
                step_id="s1",
                intent="click s1",
                ok=True,
                identity=IdentityCheck(status="verified", mode="context"),
                effect_verified=True,
                effect_results=["CONFIRMED record_written patient_id=p1"],
                before_png="s1_before.png",
                after_png="s1_after.png",
                elapsed_ms=812.0,
            ),
            StepResult(
                step_id="s2",
                intent="click s2",
                ok=False,
                error="resolution failed: template not found",
                elapsed_ms=15000.0,
            ),
        ],
        halt=HaltObservation(
            state_id="s2",
            intent="click s2",
            reason="resolution failed: template not found",
            observed_texts=["Session expired", "Log in again"],
            completed_intents=["click s1"],
        ),
        identity_applicable_steps=2,
        identity_armed_steps=1,
        total_ms=15900.0,
    )
    report.save(run)
    return run


def _make_paused_run(runs_root: Path, dirname: str, bundle: Path) -> Path:
    run = runs_root / dirname
    run.mkdir(parents=True)
    report = RunReport(
        workflow_name="triage-note",
        started_at="2026-07-17T11:00:00+00:00",
        success=False,
        results=[],
        total_ms=1000.0,
    )
    report.save(run)
    store = CheckpointStore(run)
    store.write_manifest(
        RunManifest(workflow_name="triage-note", bundle_dir=str(bundle))
    )
    store.write_checkpoint(
        RunCheckpoint(
            workflow_name="triage-note",
            step_index=0,
            step_id="s1",
            intent="click s1",
            next_step_index=1,
        )
    )
    store.write_pending(
        PendingEscalation(
            workflow_name="triage-note",
            step_index=1,
            step_id="s2",
            intent="click s2",
            category="effect_indeterminate",
            reason="verifier could not confirm the write",
            proposed_options=["approve and resume", "teach a fix"],
            resume_from_index=1,
        )
    )
    return run


def _make_library(root: Path) -> Path:
    # A minimal real ProgramGraph via the linear lift of a tiny workflow.
    from openadapt_flow.ir import lift_to_program

    wf = Workflow(name="lib-wf", steps=[_armed_click("t1")])
    graph = lift_to_program(wf)
    lib = SkillLibrary(root)
    lib.create_skill("triage-note", graph)
    lib.add_candidate("triage-note", graph, validation_score=0.9)
    return lib.path


@pytest.fixture()
def console_env(tmp_path: Path):
    bundles = tmp_path / "bundles"
    runs = tmp_path / "runs"
    bundles.mkdir()
    runs.mkdir()
    bundle = _make_bundle(bundles, "triage-note")
    bundle_v2 = _make_bundle(bundles, "triage-note-v2", extra_step=True)
    halted = _make_halted_run(runs, "replay-halted")
    paused = _make_paused_run(runs, "replay-paused", bundle)
    library = _make_library(bundles / "triage-note.skills")
    return {
        "bundles": bundles,
        "runs": runs,
        "bundle": bundle,
        "bundle_v2": bundle_v2,
        "halted": halted,
        "paused": paused,
        "library": library,
    }


def _client(env, *, allow_actions: bool = False) -> TestClient:
    app = create_app(env["bundles"], env["runs"], allow_actions=allow_actions)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. read surface
# ---------------------------------------------------------------------------


def test_health_reports_read_only(console_env):
    body = _client(console_env).get("/api/health").json()
    assert body["status"] == "ok"
    assert body["read_only"] is True


def test_index_serves_ui(console_env):
    r = _client(console_env).get("/")
    assert r.status_code == 200
    assert "Operator Console" in r.text


def test_workflow_list_with_last_run(console_env):
    body = _client(console_env).get("/api/workflows").json()
    ids = {w["id"] for w in body}
    assert {"triage-note", "triage-note-v2"} <= ids
    w = next(x for x in body if x["id"] == "triage-note")
    assert w["name"] == "triage-note"
    assert w["n_steps"] == 3
    assert w["compiler_version"]  # sealed by Workflow.save
    assert w["certified"] is False
    # newest run for the workflow name is the paused one (11:00 > 10:00)
    assert w["last_run"]["run_id"] == "replay-paused"
    assert w["last_run"]["paused"] is True


def test_workflow_detail_coverage_from_policy_helpers(console_env):
    body = _client(console_env).get("/api/workflows/triage-note").json()
    idc = body["identity_coverage"]
    assert idc["applicable"] == 2
    assert idc["armed"] == 1
    assert idc["unarmed"][0]["step_id"] == "s2"
    efc = body["effect_coverage"]
    assert efc["consequential"] == 1
    assert efc["consequential_with_contract"] == 1
    assert efc["coverage_pct"] == 100.0
    # lint flags the unarmed click (same helper the CLI lint verb uses)
    codes = {f["code"] for f in body["lint"]["findings"]}
    assert "unarmed_click" in codes
    steps = {s["id"]: s for s in body["steps"]}
    assert steps["s1"]["identity_armed"] is True
    assert steps["s1"]["effects"][0]["kind"] == "record_written"
    assert steps["s2"]["identity_armed"] is False
    assert steps["s3"]["identity_applicable"] is False


def test_effect_coverage_degrades_to_none_without_consequential_steps(tmp_path):
    bundles = tmp_path / "b"
    bundles.mkdir()
    b = bundles / "benign"
    (b / "templates").mkdir(parents=True)
    (b / "templates" / "btn.png").write_bytes(_PNG)
    Workflow(name="benign", steps=[_armed_click("s1")]).save(b)
    runs = tmp_path / "r"
    runs.mkdir()
    client = TestClient(create_app(bundles, runs))
    efc = client.get("/api/workflows/benign").json()["effect_coverage"]
    assert efc["consequential"] == 0
    assert efc["coverage_pct"] is None  # honest n/a, not a fake 100%


def test_workflow_detail_live_certification_via_policy_param(console_env):
    body = (
        _client(console_env)
        .get("/api/workflows/triage-note", params={"policy": "clinical-write"})
        .json()
    )
    cert = body["certification"]
    assert cert["sealed"]["certified"] is False
    live = cert["live"]
    assert live is not None and live["policy_name"] == "clinical-write"
    # the unarmed click must surface as a violation, not be hidden
    assert live["passed"] is False
    assert any(v["step_id"] == "s2" for v in live["violations"])


def test_workflow_diff(console_env):
    body = (
        _client(console_env)
        .get("/api/workflows/triage-note/diff/triage-note-v2")
        .json()
    )
    assert body["steps_added"] == ["s4"]
    assert body["steps_removed"] == []
    assert body["identical"] is False


def test_unreadable_bundle_degrades(console_env):
    bad = console_env["bundles"] / "corrupt"
    bad.mkdir()
    (bad / "workflow.json").write_text("{ not json")
    listed = _client(console_env).get("/api/workflows").json()
    entry = next(w for w in listed if w["id"] == "corrupt")
    assert entry["load_error"]
    body = _client(console_env).get("/api/workflows/corrupt").json()
    assert body["load_error"]


def test_unknown_ids_404(console_env):
    client = _client(console_env)
    assert client.get("/api/workflows/nope").status_code == 404
    assert client.get("/api/runs/nope").status_code == 404


# ---------------------------------------------------------------------------
# 2. runs: history, timeline, halt evidence, artifacts
# ---------------------------------------------------------------------------


def test_run_list_flags(console_env):
    runs = _client(console_env).get("/api/runs").json()
    by_id = {r["id"]: r for r in runs}
    halted = by_id["replay-halted"]
    assert halted["halted"] is True and halted["paused"] is False
    assert halted["n_failed"] == 1
    assert halted["identity_armed_steps"] == 1
    paused = by_id["replay-paused"]
    assert paused["paused"] is True and paused["approved"] is False
    # newest first
    assert runs[0]["id"] == "replay-paused"


def test_run_detail_timeline_and_halt(console_env):
    body = _client(console_env).get("/api/runs/replay-halted").json()
    t = body["timeline"]
    assert [x["step_id"] for x in t] == ["s1", "s2"]
    assert t[0]["identity"]["status"] == "verified"
    assert t[0]["effect_verified"] is True
    assert t[0]["before_png"] == "s1_before.png"
    halt = body["halt"]
    assert halt["reason"].startswith("resolution failed")
    assert "Session expired" in halt["observed_texts"]
    assert halt["completed_intents"] == ["click s1"]


def test_run_detail_pause_checkpoints_manifest(console_env):
    body = _client(console_env).get("/api/runs/replay-paused").json()
    pending = body["pending_escalation"]
    assert pending["category"] == "effect_indeterminate"
    assert pending["resume_from_index"] == 1
    assert body["manifest"]["bundle_dir"] == str(console_env["bundle"])
    assert body["checkpoints"][0]["step_id"] == "s1"


def test_artifact_served_and_traversal_refused(console_env):
    client = _client(console_env)
    ok = client.get(
        "/api/runs/replay-halted/artifact", params={"path": "s1_before.png"}
    )
    assert ok.status_code == 200
    assert ok.content == _PNG
    for evil in ("../replay-paused/report.json", "/etc/passwd", "..%2f..", ".."):
        r = client.get("/api/runs/replay-halted/artifact", params={"path": evil})
        assert r.status_code == 404, evil


# ---------------------------------------------------------------------------
# 3. governance actions: catalog + fail-closed read-only gate
# ---------------------------------------------------------------------------


def test_halted_run_offers_teach_as_copy_only(console_env):
    actions = _client(console_env).get("/api/runs/replay-halted/actions").json()
    by_id = {a["id"]: a for a in actions}
    teach = by_id["teach"]
    assert teach["executable"] is False
    assert "openadapt-flow teach" in teach["command"]
    assert "--fix" in teach["command"]
    # not paused => approve/resume do not apply
    assert "approve" not in by_id and "resume" not in by_id


def test_paused_run_offers_approve_resume_with_exact_commands(console_env):
    actions = _client(console_env).get("/api/runs/replay-paused/actions").json()
    by_id = {a["id"]: a for a in actions}
    assert by_id["approve"]["executable"] is True
    assert by_id["approve"]["command"].startswith("openadapt-flow approve ")
    assert by_id["resume"]["command"].endswith("--require-approval")


def test_read_only_refuses_mutation_with_command(console_env):
    r = _client(console_env).post("/api/runs/replay-paused/actions/approve", json={})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert "read-only" in detail["error"]
    assert detail["command"].startswith("openadapt-flow approve ")


def test_teach_never_executes_even_with_actions_enabled(console_env):
    r = _client(console_env, allow_actions=True).post(
        "/api/runs/replay-halted/actions/teach", json={}
    )
    assert r.status_code == 409
    assert "openadapt-flow teach" in r.json()["detail"]["command"]


def test_unknown_action_404(console_env):
    r = _client(console_env, allow_actions=True).post(
        "/api/runs/replay-paused/actions/delete-everything", json={}
    )
    assert r.status_code == 404


def test_approve_executes_cli_verb(console_env, monkeypatch):
    from openadapt_flow.console import actions as actions_mod

    calls = {}

    def fake_run(argv, capture_output, text, timeout):
        calls["argv"] = argv

        class P:
            returncode = 0
            stdout = "approval recorded"
            stderr = ""

        return P()

    monkeypatch.setattr(actions_mod.subprocess, "run", fake_run)
    r = _client(console_env, allow_actions=True).post(
        "/api/runs/replay-paused/actions/approve",
        json={"approver": "dr-smith", "resolution": "retry after login"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["returncode"] == 0
    # the console ran the REAL CLI verb, module-invoked, with the run dir
    assert calls["argv"][1:3] == ["-m", "openadapt_flow"]
    assert calls["argv"][3] == "approve"
    assert str(console_env["paused"]) in calls["argv"]
    assert "dr-smith" in calls["argv"]


def test_certify_action_runs_without_allow_actions(console_env, monkeypatch):
    """certify is non-mutating: executable even on a read-only server."""
    from openadapt_flow.console import actions as actions_mod

    def fake_run(argv, capture_output, text, timeout):
        class P:
            returncode = 2
            stdout = "FAIL: workflow 'triage-note'"
            stderr = ""

        return P()

    monkeypatch.setattr(actions_mod.subprocess, "run", fake_run)
    r = _client(console_env).post(
        "/api/workflows/triage-note/actions/certify",
        json={"policy": "clinical-write"},
    )
    assert r.status_code == 200
    assert r.json()["returncode"] == 2


# ---------------------------------------------------------------------------
# 4. skill library: lineage view + promote/rollback entry points
# ---------------------------------------------------------------------------


def test_skills_lineage_listed(console_env):
    libs = _client(console_env).get("/api/skills").json()
    assert len(libs) == 1
    skill = libs[0]["skills"][0]
    assert skill["skill_id"] == "triage-note"
    statuses = {v["version"]: v["status"] for v in skill["versions"]}
    assert statuses == {1: "active", 2: "candidate"}


def test_promote_via_library_entry_point(console_env):
    client = _client(console_env, allow_actions=True)
    lib_path = str(console_env["library"])
    r = client.post(
        "/api/skills/triage-note/actions/promote",
        json={"library": lib_path, "version": 2},
    )
    assert r.status_code == 200 and r.json()["returncode"] == 0
    lib = SkillLibrary(Path(lib_path).parent)
    assert lib.active_version("triage-note").version == 2
    assert lib.get("triage-note").by_version(1).status == "superseded"


def test_rollback_via_library_entry_point(console_env):
    client = _client(console_env, allow_actions=True)
    lib_path = str(console_env["library"])
    r = client.post(
        "/api/skills/triage-note/actions/rollback",
        json={"library": lib_path, "version": 2, "reason": "regressed on validation"},
    )
    assert r.status_code == 200 and r.json()["returncode"] == 0
    lib = SkillLibrary(Path(lib_path).parent)
    v2 = lib.get("triage-note").by_version(2)
    assert v2.status == "rolled_back"
    assert v2.reason == "regressed on validation"


def test_skill_actions_read_only_refused_with_command(console_env):
    r = _client(console_env).post(
        "/api/skills/triage-note/actions/promote",
        json={"library": str(console_env["library"]), "version": 2},
    )
    assert r.status_code == 403
    assert "SkillLibrary" in r.json()["detail"]["command"]


def test_skill_library_outside_roots_404(console_env, tmp_path):
    foreign = tmp_path / "elsewhere"
    _make_library(foreign)
    r = _client(console_env, allow_actions=True).post(
        "/api/skills/triage-note/actions/promote",
        json={"library": str(foreign / "skills.json"), "version": 2},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 5. smoke: the server boots on loopback against the fixture dirs
# ---------------------------------------------------------------------------


def test_server_boots_on_loopback(console_env):
    uvicorn = pytest.importorskip("uvicorn")
    import httpx

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    app = create_app(console_env["bundles"], console_env["runs"])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        health = None
        while time.time() < deadline:
            try:
                health = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1.0)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        assert health is not None, "server did not come up"
        assert health.json()["status"] == "ok"
        page = httpx.get(f"http://127.0.0.1:{port}/", timeout=5.0)
        assert "Operator Console" in page.text
        wf = httpx.get(f"http://127.0.0.1:{port}/api/workflows", timeout=5.0)
        assert any(w["id"] == "triage-note" for w in wf.json())
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ---------------------------------------------------------------------------
# 6. CLI wiring
# ---------------------------------------------------------------------------


def test_cli_console_parser_wired():
    from openadapt_flow.__main__ import build_parser

    args = build_parser().parse_args(
        ["console", "--bundles", "/b", "--runs", "/r", "--port", "9999"]
    )
    assert args.bundles == "/b"
    assert args.runs == "/r"
    assert args.port == 9999
    assert args.allow_actions is False
    assert args.func.__name__ == "_cmd_console"


def test_encrypted_pending_escalation_flagged(console_env):
    run = console_env["runs"] / "replay-enc"
    run.mkdir()
    RunReport(workflow_name="triage-note", started_at="2026-07-17T12:00:00+00:00").save(
        run
    )
    (run / "pending_escalation.json.enc").write_bytes(b"sealed")
    body = _client(console_env).get("/api/runs/replay-enc").json()
    assert body["summary"]["paused"] is True
    assert body["pending_escalation"] is None
    assert body["pending_escalation_encrypted"] is True

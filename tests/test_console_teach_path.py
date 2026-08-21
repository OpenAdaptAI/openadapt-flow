"""Compact invariants for console Teach candidate creation and metrics."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from openadapt_flow.console.app import create_app
from openadapt_flow.console.resolution_metrics import (
    emit_decision_metric,
    emit_teach_metric,
    resolution_metric_summary,
)
from openadapt_flow.console.teach import list_teach_demonstrations
from openadapt_flow.ir import HaltObservation, RunReport, Workflow
from openadapt_flow.repair.lifecycle import RepairStore
from openadapt_flow.repair.teach import (
    TeachRepairResult,
    create_teach_repair_candidate,
)
from openadapt_flow.runtime.durable.attended import AttendedDecision
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore, RunManifest
from tests.test_halt_learn_loop import (
    INTENT_DISMISS,
    INTENT_SAVE,
    INTENT_VERIFY,
    MODAL_FACT,
    SKILL_ID,
)
from tests.test_teach_cli import _base_bundle


def _halted_run(root: Path, bundle: Path) -> Path:
    run = root / "run"
    run.mkdir()
    report = RunReport(
        workflow_name=SKILL_ID,
        started_at="2026-07-28T00:00:00+00:00",
        success=False,
        halt=HaltObservation(
            state_id="s_verify",
            intent=INTENT_VERIFY,
            reason="a retained modal blocked the expected state",
            outcome="halt",
            observed_texts=[MODAL_FACT],
            completed_intents=[INTENT_SAVE],
        ),
    )
    report.save(run)
    CheckpointStore(run).write_manifest(
        RunManifest(workflow_name=SKILL_ID, bundle_dir=str(bundle))
    )
    return run


def _fix(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {"resolution_steps": [{"intent": INTENT_DISMISS, "action": "click"}]}
        )
    )
    return path


def test_teach_creates_inert_candidate_with_lineage(tmp_path: Path) -> None:
    base = _base_bundle(tmp_path)
    workflow = Workflow.load(base)
    assert workflow.program is not None
    assert workflow.program.states["s_save"].step is not None
    workflow.program.states["s_save"].step.risk = "irreversible"
    workflow.save(base)
    run = _halted_run(tmp_path, base)
    fix = _fix(tmp_path / "fix.json")
    store_root = tmp_path / "repair-store"

    result = create_teach_repair_candidate(
        run,
        fix,
        base,
        candidates_root=tmp_path / "candidates",
        repair_store=store_root,
    )

    assert result.outcome == "candidate"
    assert result.requires_human_approval is True
    assert result.candidate_id is not None
    candidate = RepairStore(store_root).load_candidate(result.candidate_id)
    assert candidate.state == "candidate"
    assert candidate.source == "teach"
    assert candidate.prior_content_digest != candidate.proposed_content_digest
    assert candidate.failure_evidence
    assert result.consequential is True
    assert RepairStore(store_root).active_pointer() is None


def test_console_uses_opaque_fix_id_and_emits_closed_metrics(
    tmp_path: Path, monkeypatch
) -> None:
    bundles = tmp_path / "bundles"
    runs = tmp_path / "runs"
    bundles.mkdir()
    runs.mkdir()
    base = _base_bundle(bundles)
    run = _halted_run(runs, base)
    demonstrations = run / "teach-demonstrations"
    demonstrations.mkdir()
    _fix(demonstrations / "local-fix.json")
    discovered = list_teach_demonstrations(run)
    assert len(discovered) == 1

    monkeypatch.setattr(
        "openadapt_flow.console.app._local_operator_identity", lambda: "operator"
    )
    app = create_app(bundles, runs, allow_actions=True)
    client = TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={
            "Authorization": f"Bearer {app.state.console_access_token}",
            "Origin": "http://127.0.0.1",
            "X-OpenAdapt-CSRF": app.state.console_csrf_token,
        },
    )
    run_id = client.get("/api/runs").json()[0]["id"]
    action = next(
        item
        for item in client.get(f"/api/runs/{run_id}/actions").json()
        if item["id"] == "teach"
    )
    assert action["executable"] is True
    assert action["inputs"][0]["choices"] == [
        {"value": discovered[0].id, "label": "Correction demonstration 1"}
    ]
    assert "local-fix" not in json.dumps(action)
    response = client.post(
        f"/api/runs/{run_id}/actions/teach",
        json={"fix_id": discovered[0].id},
    )
    assert response.status_code == 200
    assert response.json()["details"]["outcome"] == "candidate"
    assert RepairStore(bundles / ".repair-store").active_pointer() is None
    assert len(client.get("/api/workflows").json()) == 1

    now = datetime.now(timezone.utc)
    decision = AttendedDecision(
        pause_id="a" * 32,
        capability_digest="sha256:" + "b" * 64,
        request_digest="sha256:" + "c" * 64,
        idempotency_key="metric-test-request",
        action="continue",
        operator="protected-operator-name",
        status="completed",
        message="protected record text",
        created_at=now.isoformat(),
    )
    emit_decision_metric(
        run,
        category="resolution",
        pause_created_at=(now - timedelta(seconds=42)).isoformat(),
        decision=decision,
    )
    emit_teach_metric(
        run,
        category="resolution",
        result=TeachRepairResult(
            outcome="candidate",
            attempt_digest="d" * 64,
            candidate_id="e" * 16,
            candidate_state="candidate",
            candidate_record_sha256="f" * 64,
            policy_passed=True,
            qualification_passed=False,
            consequential=True,
        ),
    )
    summary = resolution_metric_summary(runs)
    assert summary.median_time_to_resolve_s == 42
    assert summary.teach_acceptance_rate == 1
    metric_bytes = b"".join(
        path.read_bytes() for path in (run / "resolution-metrics").glob("*.json")
    )
    assert b"protected-operator-name" not in metric_bytes
    assert b"protected record text" not in metric_bytes

#!/usr/bin/env python3
"""Run the first real-RDP multi-window vision qualification campaign.

The harness uses the production Recorder, compiler, FreeRDPBackend, resolver,
run gate, authorization, Replayer, and effect-verifier adapters.  The fixture
is synthetic.  Pixels and input still cross a real FreeRDP round trip.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import secrets
import sqlite3
import tempfile
import time
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Optional

from benchmark.multiapp_common import CsvRecordVerifier, SurfaceRoutedVerifier
from benchmark.rdp_ladder.run_rdp_ladder_qualification import (
    DockerX11RdpTransport,
)

VIEWPORT = (1280, 800)
TARGET_REQUEST = "REQ-LIVE-2048"
TARGET_RECORD = "REC-2048"
TARGET_NAME = "Jordan Lee"
SLOT_PARAM = "appointment_slot"
TYPE_PARAM = "appointment_type"
REQUEST_PARAM = "request_id"
DEMO_PARAMS = {
    SLOT_PARAM: "2026-08-12 09:30",
    TYPE_PARAM: "Cardiology follow-up",
    REQUEST_PARAM: TARGET_REQUEST,
}
REPLAY_PARAMS = {
    SLOT_PARAM: "2026-08-14 10:45",
    TYPE_PARAM: "Intake consultation",
    REQUEST_PARAM: TARGET_REQUEST,
}
TRIALS = 3

# Stable synthetic-fixture geometry. Runtime replay resolves visual anchors
# from the recording; these points are used only to make the demonstration.
INBOX_REQUEST = (380, 170)
WORKLIST_TAB = (555, 770)
SCHEDULER_TAB = (745, 770)
INBOX_TAB = (365, 770)
WORKLIST_VIEW = (400, 350)
WORKLIST_TARGET_AFTER_SCROLL = (430, 490)
MARK_SCHEDULED = (1110, 217)
TARGET_RECORD_ROW = (250, 155)
WRONG_RECORD_ROW = (250, 223)
SLOT_FIELD = (720, 222)
TYPE_FIELD = (720, 342)
REQUEST_FIELD = (720, 462)
SAVE_APPOINTMENT = (650, 568)
SEND_CONFIRMATION = (165, 447)

# Identity bands are recorded pixels, not live values sent out of the runner.
ACTIVE_RECORD_REGION = (510, 86, 740, 60)
WORKLIST_SELECTION_REGION = (35, 596, 920, 58)
INBOX_DETAIL_REGION = (35, 238, 915, 150)

POLICY_PATH = Path(__file__).with_name("policy.yaml")


def _read_ack(root: Path) -> Optional[str]:
    try:
        value = (root / "reset_ack.txt").read_text().strip()
        return value or None
    except OSError:
        return None


def _read_database(root: Path) -> Optional[list[dict[str, str]]]:
    path = root / "appointments.sqlite3"
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT appointment_id, request_id, record_id, entity_name, "
            "appointment_slot, appointment_type, status FROM appointments"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return None
    finally:
        if "connection" in locals():
            connection.close()


def _read_worklist(root: Path) -> Optional[list[dict[str, str]]]:
    try:
        with (root / "worklist.csv").open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))
    except (OSError, csv.Error, UnicodeDecodeError):
        return None


def _read_mail(root: Path) -> Optional[list[dict[str, str]]]:
    mail_root = root / "outbox"
    if not mail_root.is_dir():
        return None
    records: list[dict[str, str]] = []
    try:
        for folder in (mail_root / "new", mail_root / "cur"):
            if not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                if not path.is_file():
                    continue
                message = BytesParser(policy=email_policy.default).parsebytes(
                    path.read_bytes()
                )
                records.append(
                    {
                        "file": path.name,
                        "to": str(message.get("To", "")),
                        "subject": str(message.get("Subject", "")),
                        "request_id": str(message.get("X-Request-ID", "")),
                    }
                )
    except (OSError, ValueError):
        return None
    return records


def _reset(_container: str, root: Path, scenario: str = "healthy") -> None:
    token = secrets.token_hex(16)
    root.mkdir(parents=True, exist_ok=True)
    (root / "control.json").write_text(
        json.dumps({"reset_token": token, "scenario": scenario}) + "\n",
        encoding="utf-8",
    )
    deadline = time.monotonic() + 10
    diagnostics: dict[str, Any] = {}
    while time.monotonic() < deadline:
        after = _read_ack(root)
        acknowledged = after == token
        rows = _read_database(root)
        worklist = _read_worklist(root)
        mail = _read_mail(root)
        diagnostics = {
            "ack_expected": token,
            "ack_after": after,
            "acknowledged": acknowledged,
            "database": rows,
            "worklist_rows": None if worklist is None else len(worklist),
            "mail": mail,
            "root_entries": (
                sorted(path.name for path in root.iterdir())
                if root.is_dir()
                else None
            ),
        }
        if acknowledged and rows == [] and worklist and mail == []:
            return
        time.sleep(0.1)
    raise RuntimeError(
        "fixture reset did not produce clean persisted state: "
        + json.dumps(diagnostics, sort_keys=True, default=str)
    )


def _record(backend: Any, recording_dir: Path) -> None:
    from openadapt_flow.recorder import Recorder

    recorder = Recorder(
        backend,
        recording_dir,
        settle_interval_s=0.3,
        settle_stable_frames=2,
        settle_timeout_s=6.0,
    )
    recorder.click(*INBOX_REQUEST)
    recorder.click(*WORKLIST_TAB)
    recorder.click(*WORKLIST_VIEW)
    recorder.scroll(0, 800)
    recorder.click(*WORKLIST_TARGET_AFTER_SCROLL)
    recorder.click(*SCHEDULER_TAB)
    recorder.click(*TARGET_RECORD_ROW)
    recorder.click(*SLOT_FIELD)
    recorder.type_text(DEMO_PARAMS[SLOT_PARAM], param=SLOT_PARAM)
    recorder.click(*TYPE_FIELD)
    recorder.type_text(DEMO_PARAMS[TYPE_PARAM], param=TYPE_PARAM)
    recorder.click(*REQUEST_FIELD)
    recorder.type_text(DEMO_PARAMS[REQUEST_PARAM], param=REQUEST_PARAM)
    recorder.click(*SAVE_APPOINTMENT)
    recorder.click(*WORKLIST_TAB)
    recorder.click(*WORKLIST_VIEW)
    recorder.scroll(0, 800)
    recorder.click(*WORKLIST_TARGET_AFTER_SCROLL)
    recorder.click(*MARK_SCHEDULED)
    recorder.click(*INBOX_TAB)
    recorder.click(*INBOX_REQUEST)
    recorder.click(*SEND_CONFIRMATION)
    recorder.finish()


def _arm_recording(recording_dir: Path) -> None:
    path = recording_dir / "events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines() if line]
    expected = {
        13: ACTIVE_RECORD_REGION,
        18: WORKLIST_SELECTION_REGION,
        21: INBOX_DETAIL_REGION,
    }
    for index, region in expected.items():
        if index >= len(events) or events[index].get("kind") != "click":
            raise RuntimeError(f"recorded event {index} is not the expected click")
        events[index]["identifier_region"] = list(region)
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _build_verifier(root: Path) -> SurfaceRoutedVerifier:
    from openadapt_flow.runtime.effects import (
        MaildirDeliveryVerifier,
        SqlRecordVerifier,
    )

    database = root / "appointments.sqlite3"
    return SurfaceRoutedVerifier(
        {
            "appointments": SqlRecordVerifier(
                lambda: sqlite3.connect(f"file:{database}?mode=ro", uri=True),
                "SELECT appointment_id, request_id, record_id, entity_name, "
                "appointment_slot, appointment_type, status FROM appointments",
            ),
            "worklist": CsvRecordVerifier(root / "worklist.csv"),
            "outbox": MaildirDeliveryVerifier(
                str(root / "outbox"),
                content_probe=r"Jordan Lee is scheduled",
                poll_interval_s=0.05,
            ),
        },
        default_surface="appointments",
    )


def _qualify(workflow: Any, bundle_dir: Path, root: Path):
    from openadapt_flow import __version__
    from openadapt_flow.deployment import DeploymentConfig, PolicySection
    from openadapt_flow.ir import Postcondition, PostconditionKind, Workflow
    from openadapt_flow.qualification import (
        ActionRiskClass,
        ActionRiskClassification,
        EnvironmentBoundary,
        init_project,
        set_action_classification,
    )
    from openadapt_flow.run_gate import evaluate_run_gate
    from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

    if len(workflow.steps) != 22:
        raise RuntimeError(
            f"expected 22 compiled actions from the demonstration, got {len(workflow.steps)}"
        )
    save, reconcile, send = workflow.steps[13], workflow.steps[18], workflow.steps[21]
    for step, label in ((save, "save"), (reconcile, "reconcile"), (send, "send")):
        step.risk = "irreversible"
        step.risk_explanation = f"qualified {label} write"
        step.risk_review_required = False
    save.expect = [
        Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Appointment saved")
    ]
    save.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={
                "request_id": ValueExpr(param=REQUEST_PARAM),
                "record_id": ValueExpr(literal=TARGET_RECORD),
                "appointment_slot": ValueExpr(param=SLOT_PARAM),
                "appointment_type": ValueExpr(param=TYPE_PARAM),
                "status": ValueExpr(literal="scheduled"),
            },
            expected_count=1,
            count_new_only=True,
            key_field="request_id",
            idempotency_key=ValueExpr(param=REQUEST_PARAM),
            risk="irreversible",
            probe="surface=appointments|read-only exact appointment lookup",
        )
    ]
    reconcile.expect = [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT, text=f"Reconciled {TARGET_REQUEST}"
        )
    ]
    reconcile.effects = [
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"request_id": ValueExpr(param=REQUEST_PARAM)},
            field="status",
            value=ValueExpr(literal="Scheduled"),
            idempotency_key=ValueExpr(param=REQUEST_PARAM),
            key_field="request_id",
            risk="irreversible",
            probe="surface=worklist|CSV row re-read",
        )
    ]
    send.expect = [
        Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Confirmation queued")
    ]
    send.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={
                "to": ValueExpr(literal="referrals@example.test"),
                "subject": ValueExpr(literal=f"Scheduled {TARGET_REQUEST}"),
                "content_match": ValueExpr(literal="True"),
            },
            expected_count=1,
            count_new_only=True,
            idempotency_key=ValueExpr(literal=f"Scheduled {TARGET_REQUEST}"),
            key_field="subject",
            risk="irreversible",
            probe="surface=outbox|Maildir delivery capture",
        )
    ]

    environment_payload = json.dumps(
        {
            "application": "rdp-multiapp-suite",
            "policy_sha256": hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest(),
            "surface": "freerdp3-roundtrip",
            "viewport": VIEWPORT,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="rdp",
            application="RDP multi-window synthetic suite",
            application_version="v1",
            environment_digest=hashlib.sha256(environment_payload).hexdigest(),
            runtime_version=__version__,
            required_capabilities=[
                "vision-only-resolution",
                "window-switching",
                "read-only-sql-effect",
                "csv-effect",
                "maildir-effect",
            ],
        ),
    )
    for step in workflow.steps:
        classification = (
            ActionRiskClass.IRREVERSIBLE
            if step.id in {save.id, reconcile.id, send.id}
            else ActionRiskClass.READ_ONLY
        )
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=step.id,
                classification=classification,
                explanation=(
                    "Qualified persisted business write"
                    if classification is ActionRiskClass.IRREVERSIBLE
                    else "Navigation, focus, data entry, or scrolling before a qualified write"
                ),
                operator_confirmed=True,
            ),
        )

    key = secrets.token_urlsafe(32)
    workflow.save(bundle_dir, encrypt=True, key=key)
    workflow = Workflow.load(bundle_dir, key=key)
    verifier = _build_verifier(root)
    gate = evaluate_run_gate(
        workflow,
        bundle_dir=bundle_dir,
        deployment=DeploymentConfig(policy=PolicySection(policy=str(POLICY_PATH))),
        effect_verifier=verifier,
        policy_source=str(POLICY_PATH),
        strict_templates=True,
        require_encryption=True,
    )
    if not gate.passed:
        raise RuntimeError(gate.render())
    return (
        workflow,
        verifier,
        gate,
        {"save": save.id, "reconcile": reconcile.id, "send": send.id},
    )


def _oracle_result(root: Path, params: dict[str, str]) -> dict[str, Any]:
    database = _read_database(root)
    worklist = _read_worklist(root)
    mail = _read_mail(root)
    expected_db = {
        "request_id": TARGET_REQUEST,
        "record_id": TARGET_RECORD,
        "entity_name": TARGET_NAME,
        "appointment_slot": params[SLOT_PARAM],
        "appointment_type": params[TYPE_PARAM],
        "status": "scheduled",
    }
    db_ok = bool(
        database is not None
        and len(database) == 1
        and all(database[0].get(key) == value for key, value in expected_db.items())
    )
    target_rows = [
        row for row in worklist or [] if row.get("request_id") == TARGET_REQUEST
    ]
    adjacent_unchanged = bool(
        worklist is not None
        and all(
            row.get("status") == "New"
            for row in worklist
            if row.get("request_id") != TARGET_REQUEST
        )
    )
    csv_ok = (
        len(target_rows) == 1
        and target_rows[0].get("status") == "Scheduled"
        and adjacent_unchanged
    )
    mail_ok = bool(
        mail is not None
        and len(mail) == 1
        and mail[0].get("to") == "referrals@example.test"
        and mail[0].get("subject") == f"Scheduled {TARGET_REQUEST}"
        and mail[0].get("request_id") == TARGET_REQUEST
    )
    return {
        "database_ok": db_ok,
        "worklist_ok": csv_ok,
        "mail_ok": mail_ok,
        "all_effects_ok": db_ok and csv_ok and mail_ok,
        "database": database,
        "worklist_target": target_rows,
        "mail": mail,
        "adjacent_rows_unchanged": adjacent_unchanged,
    }


def _run_once(
    *,
    container: str,
    root: Path,
    workflow: Any,
    verifier: Any,
    gate: Any,
    bundle_dir: Path,
    run_dir: Path,
    condition: str,
    save_pointer_acquisition: int,
) -> dict[str, Any]:
    from openadapt_flow.backends.rdp_backend import FreeRDPBackend
    from openadapt_flow.run_gate import build_runtime_authorization
    from openadapt_flow.runtime.replayer import Replayer

    _reset(
        container, root, "row_reordered" if condition == "row_reordered" else "healthy"
    )
    params = dict(REPLAY_PARAMS)
    authorization = build_runtime_authorization(
        workflow,
        gate,
        approval_source="rdp-multiapp-qualification",
        params=params,
    )
    transport = DockerX11RdpTransport(container)
    backend = FreeRDPBackend(transport, connect=True)
    original_acquire = backend.acquire_actuation_frame
    acquisitions = 0
    injected = False

    def acquire_with_fault() -> bytes:
        nonlocal acquisitions, injected
        acquisitions += 1
        if acquisitions == save_pointer_acquisition:
            if condition == "wrong_record_before_write":
                transport.pointer(*WRONG_RECORD_ROW, "left", True)
                transport.pointer(*WRONG_RECORD_ROW, "left", False)
                time.sleep(0.45)
                injected = True
            elif condition == "focus_theft_before_write":
                transport.pointer(*INBOX_TAB, "left", True)
                transport.pointer(*INBOX_TAB, "left", False)
                time.sleep(0.45)
                injected = True
        return original_acquire()

    backend.acquire_actuation_frame = acquire_with_fault  # type: ignore[method-assign]
    started = time.monotonic()
    report = Replayer(
        backend,
        poll_interval_s=0.3,
        effect_verifier=verifier,
        governed_authorization=authorization,
        pixel_verify_enabled=True,
        durable=True,
        require_settled=True,
    ).run(
        workflow,
        params=params,
        bundle_dir=bundle_dir,
        run_dir=run_dir,
        idempotency_key=f"{condition}-{run_dir.name}",
    )
    oracle = _oracle_result(root, params)
    expected_halt = condition in {
        "wrong_record_before_write",
        "focus_theft_before_write",
    }
    safe_halt = expected_halt and not report.success and not oracle["database"]
    if condition == "row_reordered":
        passed = bool(
            (report.success and oracle["all_effects_ok"])
            or (not report.success and not oracle["database"])
        )
    elif expected_halt:
        passed = bool(injected and safe_halt)
    else:
        passed = bool(report.success and oracle["all_effects_ok"])
    return {
        "condition": condition,
        "passed": passed,
        "runtime_s": round(time.monotonic() - started, 3),
        "runtime_success": bool(report.success),
        "model_calls": int(report.model_calls),
        "fault_injected": injected,
        "safe_halt": safe_halt,
        "silent_incorrect_success": bool(
            report.success and not oracle["all_effects_ok"]
        ),
        "over_halt": bool(condition == "healthy" and not report.success),
        "oracle": oracle,
        "rung_counts": dict(report.rung_counts),
        "errors": [result.error for result in report.results if result.error],
    }


def run(container: str, root: Path, out: Path, work: Path) -> dict[str, Any]:
    from openadapt_flow.backends.rdp_backend import FreeRDPBackend
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import ActionKind

    root.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    _reset(container, root)
    record_backend = FreeRDPBackend(DockerX11RdpTransport(container), connect=True)
    recording_dir = work / "recording"
    bundle_dir = work / "bundle"
    _record(record_backend, recording_dir)
    _arm_recording(recording_dir)
    compiled = compile_recording(recording_dir, bundle_dir, name="rdp-multiapp-vision")
    workflow, verifier, gate, step_ids = _qualify(compiled, bundle_dir, root)
    pointer_actions = {
        ActionKind.CLICK,
        ActionKind.DOUBLE_CLICK,
        ActionKind.RIGHT_CLICK,
        ActionKind.DRAG,
    }
    save_index = next(
        i for i, step in enumerate(workflow.steps) if step.id == step_ids["save"]
    )
    save_pointer_acquisition = sum(
        step.action in pointer_actions for step in workflow.steps[: save_index + 1]
    )
    conditions = (
        "healthy",
        "row_reordered",
        "wrong_record_before_write",
        "focus_theft_before_write",
    )
    trials: list[dict[str, Any]] = []
    for condition in conditions:
        for index in range(1, TRIALS + 1):
            trials.append(
                _run_once(
                    container=container,
                    root=root,
                    workflow=workflow,
                    verifier=verifier,
                    gate=gate,
                    bundle_dir=bundle_dir,
                    run_dir=work / f"run-{condition}-{index}",
                    condition=condition,
                    save_pointer_acquisition=save_pointer_acquisition,
                )
            )
    result = {
        "schema_version": "openadapt.rdp-multiapp-results.v1",
        "campaign_contract": "benchmark/rdp_multiapp/campaign.json",
        "implemented_conditions": list(conditions),
        "full_campaign_complete": False,
        "full_campaign_pending_conditions": [
            "duplicate_save_control",
            "partial_render",
            "moderate_display_drift",
            "severe_display_drift",
            "commit_then_timeout",
        ],
        "run_count": len(trials),
        "accepted_subset": all(trial["passed"] for trial in trials),
        "silent_incorrect_successes": sum(
            bool(trial["silent_incorrect_success"]) for trial in trials
        ),
        "over_halts": sum(bool(trial["over_halt"]) for trial in trials),
        "model_calls": sum(int(trial["model_calls"]) for trial in trials),
        "trials": trials,
    }
    out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="oaflow-rdp-multiapp")
    parser.add_argument("--oracle-root", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("benchmark/rdp_multiapp/results.json")
    )
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="oaflow-rdp-multiapp-"))
    result = run(args.container, args.oracle_root.resolve(), args.output, work)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["accepted_subset"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

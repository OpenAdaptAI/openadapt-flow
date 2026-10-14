"""Readiness drills from a local campaign, end to end in a real browser (B276).

The made-up MockMed task is recorded and compiled the way the tutorial does
it. Its Check it project is set up with one practice case, and
``prepare_local_drill_campaign`` turns the project's five drills into a local
campaign from that practice case. Then every case runs through the real
Replayer, in Chromium, against MockMed's independent record store, under the
default per-item approval mode:

- the practice item waits for a person, and after the approval ends
  ``VERIFIED`` with the record written exactly once;
- each drill ends ``HALTED`` on the engine's own detector, with the exact
  stage and code its fault kind requires, before any input was delivered,
  with nothing written, and without asking anyone;
- Flow's own qualification evidence check accepts all five drill results,
  signed by a runner key, with the drill's signed receipt and mutation record.

Every read-back here is MockMed's test setup (its ``/api/db`` store), not a
customer verifier. All people and values are made up.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

pytest.importorskip("playwright.sync_api")

from scripts import export_public_demo_evidence as demo  # noqa: E402

from openadapt_flow.approval_mode.gate import ApprovalGate  # noqa: E402
from openadapt_flow.approval_mode.item_approval import (  # noqa: E402
    ItemApprovalStore,
    make_answer,
)
from openadapt_flow.approval_mode.model import ItemDecisionKind  # noqa: E402
from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.deployment import DeploymentConfig, PolicySection  # noqa: E402
from openadapt_flow.execution_profiles import (  # noqa: E402
    ExecutionProfile,
    execution_profile_contract,
)
from openadapt_flow.ir import Workflow  # noqa: E402
from openadapt_flow.mockmed.fault_server import serve  # noqa: E402
from openadapt_flow.qualification import (  # noqa: E402
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    EvidenceRef,
    IdentityEnforcement,
    IdentityPolicy,
    QualificationActionTarget,
    QualificationCase,
    QualificationCaseKind,
    QualificationCaseResult,
    QualificationOutcome,
    add_case,
    init_project,
    qualification_campaign_id_sha256,
    qualification_run_id_sha256,
    record_case_results,
    save_qualified_workflow,
    set_action_classification,
    set_case_scope,
    set_effect_policy,
    set_identity_policy,
    set_trusted_runner_key,
    sign_case_result,
    workflow_contract_sha256,
)
from openadapt_flow.qualification_drills import (  # noqa: E402
    LocalDrillDriver,
    local_drill_key,
    prepare_local_drill_campaign,
    write_drill_evidence,
)
from openadapt_flow.qualification_faults import (  # noqa: E402
    expected_fault_detector,
    fault_detector_contract_error,
)
from openadapt_flow.run_gate import (  # noqa: E402
    build_qualification_case_authorization,
    evaluate_run_gate,
)
from openadapt_flow.runtime import Replayer  # noqa: E402
from openadapt_flow.runtime.authorization import (  # noqa: E402
    runtime_inputs_bytes,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.effects import RestRecordVerifier  # noqa: E402
from openadapt_flow.tutorial import (  # noqa: E402
    TUTORIAL_ENTRY_QUERY,
    TUTORIAL_NOTE,
    consequential_step,
    record_tutorial,
)
from openadapt_flow.verification import VerificationTier  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_ID = "local-readiness-drills-e2e"
RUNNER_KEY_ID = "drills-e2e-runner"
APPROVE: dict[str, Any] = dict(
    decision="approve",
    decided_by="human",
    route="desktop_run_it",
    approver_id="cloud-user-0001",
    auth_method="cloud_aal2",
)


class Person:
    """Answers the waiting request in Desktop's Run it (or nobody does)."""

    def __init__(self, store: ItemApprovalStore, answer: bool) -> None:
        self.store = store
        self.answer = answer
        self.clock = 0.0
        self.asked = 0

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += seconds
        for request in self.store.requests():
            if self.store.status(request) == "waiting":
                self.asked += 1
                if self.answer:
                    self.store.write_answer(make_answer(request, **APPROVE))


def _records(base_url: str) -> list[dict[str, Any]]:
    return list(demo._http_json(f"{base_url}api/db").get("records", []))


def _run_case(
    *,
    base_url: str,
    workflow: Workflow,
    bundle: Path,
    case: QualificationCase,
    key: Any,
    app_digest: str,
    run_dir: Path,
) -> SimpleNamespace:
    demo._http_json(f"{base_url}api/reset", method="POST", body={})
    params = {"note": TUTORIAL_NOTE}
    entry_url = f"{base_url.rstrip('/')}/{TUTORIAL_ENTRY_QUERY}"
    run_id = f"{case.id}-trial-01"
    representative = case.kind is QualificationCaseKind.REPRESENTATIVE
    store = ItemApprovalStore(run_dir)
    person = Person(store, answer=representative)
    driver: Optional[LocalDrillDriver] = (
        None if representative else LocalDrillDriver(key, workflow=workflow, params=params)
    )
    backend, close = PlaywrightBackend.launch(entry_url, headless=True)
    try:
        verifier = RestRecordVerifier(
            base_url,
            records_path="/api/db",
            records_key="records",
            timeout_s=2.0,
            poll_interval_s=0.05,
        )
        gate = evaluate_run_gate(
            workflow,
            bundle_dir=bundle,
            deployment=DeploymentConfig(policy=PolicySection(policy="clinical-write")),
            effect_verifier=verifier,
            profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
            effective_durable=True,
            effective_require_settled=True,
            qualification_evidence_only=True,
        )
        assert gate.passed, gate.render()
        authorization = build_qualification_case_authorization(
            workflow,
            gate,
            case_id=case.id,
            params=params,
            worklists=None,
            campaign_id=CAMPAIGN_ID,
            run_id=run_id,
            fault_driver=driver,
        ).model_copy(update={"approval_mode": "each_item"})
        approval_gate = ApprovalGate(
            "each_item",
            approval_store=store,
            approval_wait_s=30,
            approval_poll_s=0.5,
            sleep=person.sleep,
            monotonic=person.monotonic,
        )
        report = Replayer(
            backend,
            effect_verifier=verifier,
            governed_authorization=authorization,
            qualification_fault_driver=driver,
            qualification_environment_observer=demo._MockMedEnvironmentObserver(
                app_digest=app_digest
            ),
            durable=True,
            require_settled=True,
            approval_gate=approval_gate,
        ).run(
            workflow.model_copy(deep=True),
            params=params,
            bundle_dir=bundle,
            run_dir=run_dir,
            execution_target_kind="web",
            execution_origin=base_url.rstrip("/"),
            execution_entry_url=entry_url,
            run_id=run_id,
        )
    finally:
        close()
    records = _records(base_url)
    evidence = (
        write_drill_evidence(run_dir, driver, report) if driver is not None else None
    )
    return SimpleNamespace(
        report=report,
        run_dir=run_dir,
        run_id=run_id,
        records=records,
        person=person,
        store=store,
        evidence=evidence,
        driver=driver,
        params=params,
    )


@pytest.fixture(scope="module")
def drilled(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("local-drills")
    previous_home = os.environ.get("OPENADAPT_HOME")
    os.environ["OPENADAPT_HOME"] = str(work / "openadapt-home")
    base_url, _db, stop = serve()
    try:
        recording = record_tutorial(base_url, work / "recording")
        bundle = work / "bundle"
        workflow = compile_recording(
            recording, bundle, name="drills-e2e", mine_effects=True
        )
        save = consequential_step(workflow)
        app_digest = demo._tree_digest(REPO_ROOT / "openadapt_flow" / "mockmed")
        backend, close = PlaywrightBackend.launch(
            f"{base_url.rstrip('/')}/{TUTORIAL_ENTRY_QUERY}", headless=True
        )
        try:
            payload = demo._environment_payload(backend=backend, app_digest=app_digest)
        finally:
            close()
        environment_digest = demo._sha256_bytes(demo._canonical_json(payload))
        init_project(
            workflow,
            environment=EnvironmentBoundary(
                target_kind="web",
                application="OpenAdapt MockMed synthetic reference",
                application_identity=demo._origin(base_url),
                application_version=f"sha256:{app_digest}",
                environment_observer_id=demo.ENVIRONMENT_OBSERVER_ID,
                environment_observer_contract_sha256=(
                    demo.ENVIRONMENT_OBSERVER_CONTRACT_SHA256
                ),
                environment_digest=environment_digest,
                runtime_version=demo.FLOW_VERSION,
                required_capabilities=[
                    "headless_chromium",
                    "independent_system_of_record",
                    "playwright_dom",
                ],
            ),
            minimum_effect_tier=VerificationTier.INDEPENDENT_SYSTEM,
        )
        for step in workflow.steps:
            irreversible = step.risk == "irreversible"
            set_action_classification(
                workflow,
                ActionRiskClassification(
                    step_id=step.id,
                    classification=(
                        ActionRiskClass.IRREVERSIBLE
                        if irreversible
                        else ActionRiskClass.READ_ONLY
                    ),
                    explanation=(
                        "the save writes the encounter"
                        if irreversible
                        else "navigation and form entry before the save"
                    ),
                    operator_confirmed=True,
                ),
            )
        set_identity_policy(
            workflow,
            IdentityPolicy(
                step_id=save.id, enforcement=IdentityEnforcement.CANONICAL_LADDER
            ),
        )
        for index, _effect in enumerate(save.effects):
            set_effect_policy(
                workflow,
                step_id=save.id,
                effect_index=index,
                tier=VerificationTier.INDEPENDENT_SYSTEM,
            )
        add_case(
            workflow,
            QualificationCase(
                id="practice",
                kind=QualificationCaseKind.REPRESENTATIVE,
                expected_outcome=QualificationOutcome.VERIFIED,
                description="The customer's practice item",
            ),
        )
        set_case_scope(
            workflow,
            case_id="practice",
            runtime_input_sha256=runtime_inputs_digest(
                workflow, {"note": TUTORIAL_NOTE}, None
            ),
            action_targets=[
                QualificationActionTarget(step_id=save.id, actuation_path="gui")
            ],
        )
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        runner = Ed25519PrivateKey.generate()
        runner_private = runner.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        set_trusted_runner_key(
            workflow,
            key_id=RUNNER_KEY_ID,
            public_key_base64=base64.b64encode(
                runner.public_key().public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                )
            ).decode("ascii"),
        )
        key = local_drill_key(create=True)
        campaign = prepare_local_drill_campaign(workflow, key=key)
        save_qualified_workflow(workflow, bundle)
        workflow = Workflow.load(bundle)
        cases = {case.id: case for case in workflow.qualification.cases}
        runs = {
            case_id: _run_case(
                base_url=base_url,
                workflow=workflow,
                bundle=bundle,
                case=case,
                key=key,
                app_digest=app_digest,
                run_dir=work / "runs" / case_id,
            )
            for case_id, case in cases.items()
        }
        yield SimpleNamespace(
            work=work,
            workflow=workflow,
            campaign=campaign,
            cases=cases,
            runs=runs,
            runner_private=runner_private,
        )
    finally:
        stop()
        if previous_home is None:
            os.environ.pop("OPENADAPT_HOME", None)
        else:
            os.environ["OPENADAPT_HOME"] = previous_home


def test_the_campaign_is_the_projects_five_drills_on_the_practice_inputs(drilled):
    drills = drilled.campaign.drills
    assert len(drills) == 5
    assert {drill.state for drill in drills} == {"scoped"}
    practice = drilled.cases["practice"]
    for drill in drills:
        case = drilled.cases[drill.case_id]
        assert case.runtime_input_sha256 == practice.runtime_input_sha256


def test_the_practice_item_saves_only_after_a_persons_approval(drilled):
    run = drilled.runs["practice"]
    report = run.report
    assert report.transaction_outcome == "VERIFIED", [
        (result.step_id, result.error) for result in report.results if not result.ok
    ]
    assert report.model_calls == 0
    assert report.approval.decision is ItemDecisionKind.APPROVED
    assert run.person.asked == 1
    assert len(run.records) == 1
    assert run.records[0]["note"] == TUTORIAL_NOTE


@pytest.mark.parametrize(
    "kind",
    ["ambiguity", "wrong_identity", "stale_identity", "weak_effect", "missing_effect"],
)
def test_each_drill_stops_on_the_engines_own_detector_and_saves_nothing(
    drilled, kind
):
    case_id = next(
        drill.case_id for drill in drilled.campaign.drills if drill.kind == kind
    )
    run = drilled.runs[case_id]
    report = run.report
    assert run.evidence["staged"] is True, run.evidence
    assert report.execution_outcome == "HALTED"
    # The engine's own words for "nothing was written": an identity conflict
    # is a policy refusal, the others stop before the effect.
    assert report.transaction_outcome in {"HALTED_BEFORE_EFFECT", "REJECTED_POLICY"}
    (receipt,) = report.qualification_fault_mutations
    assert fault_detector_contract_error(report, receipt) is None
    refused = next(
        result for result in report.results if result.safety_refusal_evidence
    )
    refusal = refused.safety_refusal_evidence
    assert (refusal.stage, refusal.code) == expected_fault_detector(kind)
    # The save itself was never delivered.
    assert refused.delivery_attempted is False
    assert run.records == [], "a drill never writes"
    assert run.person.asked == 0, "a drill never asks a person to approve"
    assert run.store.requests() == []
    mutation = json.loads(
        (run.run_dir / "qualification-fault" / "mutation.json").read_text()
    )
    assert TUTORIAL_NOTE not in json.dumps(mutation)


def test_flow_accepts_the_drill_results_as_passed_evidence(drilled):
    workflow = drilled.workflow
    project = workflow.qualification
    root = drilled.work
    results = []
    for drill in drilled.campaign.drills:
        run = drilled.runs[drill.case_id]
        inputs = run.run_dir.parent / f"{drill.case_id}-inputs.json"
        inputs.write_bytes(runtime_inputs_bytes(workflow, run.params, None))

        def ref(path: Path, kind: str) -> EvidenceRef:
            return EvidenceRef(
                kind=kind,
                sha256=demo._sha256(path),
                relative_path=path.relative_to(root).as_posix(),
            )

        result = QualificationCaseResult(
            case_id=drill.case_id,
            project_id=project.project_id,
            project_revision=project.revision,
            project_contract_sha256=project.contract_sha256(),
            workflow_contract_sha256=workflow_contract_sha256(workflow),
            environment_contract_sha256=project.environment.contract_sha256(),
            environment_digest=project.environment.environment_digest,
            runtime_version=project.environment.runtime_version,
            runner_id="drills-e2e-runner",
            runner_capabilities=list(project.environment.required_capabilities),
            status="passed",
            observed_outcome=QualificationOutcome.HALTED,
            campaign_id_sha256=qualification_campaign_id_sha256(CAMPAIGN_ID),
            case_input_sha256=runtime_inputs_digest(workflow, run.params, None),
            run_id_sha256=qualification_run_id_sha256(run.run_id),
            evidence=[
                ref(run.run_dir / "report.json", "run_report"),
                ref(inputs, "case_input"),
                ref(run.run_dir / "qualification-fault" / "receipt.json", "fault_receipt"),
                ref(
                    run.run_dir / "qualification-fault" / "mutation.json",
                    "fault_mutation",
                ),
                # The run's frames and durable evidence, hash-bound, as
                # Desktop retains them with the report.
                *(
                    ref(path, "other")
                    for path in sorted(run.run_dir.rglob("*"))
                    if path.is_file()
                    and path.name != "report.json"
                    and "qualification-fault" not in path.parts
                ),
            ],
            attestation_key_id=RUNNER_KEY_ID,
        )
        results.append(sign_case_result(result, private_key=drilled.runner_private))
    record_case_results(workflow, results, evidence_root=root)
    recorded = {
        case.id: case.results
        for case in workflow.qualification.cases
        if case.kind is not QualificationCaseKind.REPRESENTATIVE
    }
    assert all(len(rows) == 1 for rows in recorded.values())

"""Adversarial coverage for qualification fault and environment bindings."""

from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image, ImageDraw

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.deployment import build_replayer
from openadapt_flow.execution_profiles import (
    ExecutionOutcome,
    ExecutionProfile,
    classify_execution_outcome,
)
from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    ActionKind,
    Anchor,
    ApiBinding,
    ApiIdentityBinding,
    SafetyRefusalEvidence,
    Step,
    StepResult,
    StructuralHandle,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.qualification import (
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    QualificationActionTarget,
    QualificationCase,
    QualificationCaseKind,
    QualificationOutcome,
    add_case,
    init_project,
    set_action_classification,
)
from openadapt_flow.qualification_environment import (
    BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256,
    BackendQualificationEnvironmentObserver,
    QualificationEnvironmentObservation,
)
from openadapt_flow.qualification_faults import (
    FaultMutationReceipt,
    QualificationFaultContext,
    QualificationFaultMutation,
    effect_verifier_input_sha256,
    fault_detector_contract_error,
    sha256_bytes,
    sign_fault_mutation_receipt,
    verify_fault_mutation_receipt,
)
from openadapt_flow.runtime import Replayer
from openadapt_flow.runtime.actuators import ActuationStatus, ApiActuationResult
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    ValueExpr,
    Verdict,
)
from openadapt_flow.verification import VerificationTier

_SESSION = "a" * 64
_ENVIRONMENT = "b" * 64


def test_unbound_environment_and_empty_fault_store_preserve_legacy_shape() -> None:
    workflow = Workflow(name="legacy-qualification", steps=[])
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Legacy display label",
            application_version="1",
            environment_digest="0" * 64,
            runtime_version="legacy",
        ),
    )

    assert workflow.qualification is not None
    dumped = workflow.qualification.model_dump(mode="json")
    assert "trusted_fault_driver_keys" not in dumped
    assert "application_identity" not in dumped["environment"]
    assert "environment_observer_id" not in dumped["environment"]
    assert "environment_observer_contract_sha256" not in dumped["environment"]


def test_multi_action_fault_case_requires_one_explicit_fault_target() -> None:
    actions = [
        QualificationActionTarget(step_id="prepare", actuation_path="gui"),
        QualificationActionTarget(step_id="submit", actuation_path="gui"),
    ]

    with pytest.raises(ValueError, match="requires one exact fault target"):
        QualificationCase(
            id="fault-later-write",
            kind=QualificationCaseKind.MISSING_EFFECT,
            action_targets=actions,
            expected_outcome=QualificationOutcome.HALTED,
        )

    case = QualificationCase(
        id="fault-later-write",
        kind=QualificationCaseKind.MISSING_EFFECT,
        action_targets=actions,
        fault_target=actions[1],
        expected_outcome=QualificationOutcome.HALTED,
    )
    assert case.resolved_fault_target() == actions[1]


def _screen_png(*, ambiguous: bool = False, wrong_identity: bool = False) -> bytes:
    image = Image.new("RGB", (20, 20), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((5, 5, 10, 10), fill="black")
    if ambiguous:
        draw.rectangle((12, 5, 17, 10), fill="black")
    if wrong_identity:
        draw.rectangle((0, 12, 19, 19), fill="orange")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class _ObservedBackend:
    viewport = (20, 20)

    def __init__(self) -> None:
        self.actions: list[tuple[str, int, int]] = []
        self.ambiguous = False
        self.wrong_identity = False
        self._input_guard: Any = None

    def screenshot(self) -> bytes:
        return _screen_png(
            ambiguous=self.ambiguous,
            wrong_identity=self.wrong_identity,
        )

    def locate_structural(self, _locator: StructuralLocator) -> StructuralHandle:
        if self.ambiguous:
            raise StructuralResolutionRefused("two submit controls match")
        return StructuralHandle(
            point=(8, 8),
            region=(5, 5, 6, 6),
            target_fingerprint="c" * 64,
        )

    def set_qualification_input_guard(self, guard: Any) -> None:
        self._input_guard = guard

    def _guard(self) -> None:
        if self._input_guard is not None:
            self._input_guard()

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        assert not double
        self._guard()
        self.actions.append(("click", x, y))

    def act_structural(
        self,
        _locator: StructuralLocator,
        handle: StructuralHandle,
        *_args: Any,
        **_kwargs: Any,
    ) -> ActionDeliveryReceipt:
        self._guard()
        self.actions.append(("structural", *handle.point))
        return ActionDeliveryReceipt(
            receipt_id=f"fixture-{len(self.actions)}",
            operation="invoke",
            native=True,
            target_fingerprint=handle.target_fingerprint,
            delivered_at="2026-07-28T00:00:00+00:00",
        )

    def structured_text_at(self, _x: int, _y: int) -> str:
        return "Wrong record" if self.wrong_identity else "Synthetic record"

    def qualification_environment_identity(self) -> tuple[str, str, str, str]:
        return "https://fixture.example", "1", _SESSION, _ENVIRONMENT


@dataclass(frozen=True)
class _SettleResult:
    png: bytes
    settled: bool = True


class _Vision:
    def wait_settled(self, backend: _ObservedBackend, **_kwargs: Any) -> bytes:
        return backend.screenshot()

    def wait_settled_result(
        self, backend: _ObservedBackend, **_kwargs: Any
    ) -> _SettleResult:
        return _SettleResult(backend.screenshot())


class _StrongEffectVerifier:
    substrate = "test"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def capture_pre_state(self, _context: Any = None) -> EffectState:
        return EffectState(substrate=self.substrate, reachable=True)

    def verify(
        self,
        effect: Effect,
        _before: EffectState,
        _context: Any = None,
    ) -> EffectVerdict:
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=effect.kind,
            substrate=self.substrate,
        )


class _WeakEffectVerifier:
    verification_tier = VerificationTier.IMMEDIATE_SCREEN


class _NonRefusingEffectVerifier:
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def capture_pre_state(self) -> object:
        return object()


class _UnavailableApiActuator:
    def __init__(self) -> None:
        self.calls = 0

    def actuate(self, _binding: ApiBinding, _params: dict[str, str]):
        self.calls += 1
        return ApiActuationResult(
            status=ActuationStatus.UNAVAILABLE,
            reason="fixture request was not sent",
        )


class _FaultDriver:
    key_id = "test-fault-driver"

    def __init__(
        self,
        kind: QualificationCaseKind,
        *,
        decline: bool = False,
        change_binding: str | None = None,
        non_refusing_effect_mutation: bool = False,
        signing_key: Ed25519PrivateKey | None = None,
    ) -> None:
        self.kind = kind
        self._driver_id = f"test.{kind.value}-driver"
        self._contract_sha256 = hashlib.sha256(
            f"test {kind.value} driver v1".encode()
        ).hexdigest()
        self.decline = decline
        self.change_binding = change_binding
        self.non_refusing_effect_mutation = non_refusing_effect_mutation
        self.calls = 0
        self._identity_reads = {"id": 0, "contract": 0, "key": 0}
        self._signing_key = signing_key or Ed25519PrivateKey.generate()
        self.private_key = self._signing_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        self.public_key_base64 = base64.b64encode(
            self._signing_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        ).decode("ascii")

    def _bound_value(self, field: str, value: str) -> str:
        self._identity_reads[field] += 1
        if self.change_binding == field and self._identity_reads[field] > 1:
            return f"changed-{value}" if field != "contract" else "d" * 64
        return value

    @property
    def driver_id(self) -> str:
        return self._bound_value("id", self._driver_id)

    @property
    def contract_sha256(self) -> str:
        return self._bound_value("contract", self._contract_sha256)

    @property
    def attestation_key_id(self) -> str:
        return self._bound_value("key", self.key_id)

    def mutate(
        self, context: QualificationFaultContext
    ) -> QualificationFaultMutation | None:
        self.calls += 1
        if self.decline or context.fault_kind != self.kind.value:
            return None
        replacement = context.effect_verifier
        replace_effect_verifier = False
        if self.kind in {
            QualificationCaseKind.AMBIGUITY,
            QualificationCaseKind.STALE_IDENTITY,
        }:
            context.backend.ambiguous = True
            after_sha256 = sha256_bytes(context.backend.screenshot())
        elif self.kind is QualificationCaseKind.WRONG_IDENTITY:
            context.backend.wrong_identity = True
            after_sha256 = sha256_bytes(context.backend.screenshot())
        else:
            replace_effect_verifier = True
            if self.non_refusing_effect_mutation:
                replacement = _NonRefusingEffectVerifier()
            else:
                replacement = (
                    None
                    if self.kind is QualificationCaseKind.MISSING_EFFECT
                    else _WeakEffectVerifier()
                )
            after_sha256 = effect_verifier_input_sha256(
                replacement,
                context.effects,
            )
        receipt = FaultMutationReceipt(
            project_id=context.project_id,
            project_revision=context.project_revision,
            project_contract_sha256=context.project_contract_sha256,
            campaign_id_sha256=context.campaign_id_sha256,
            case_id_sha256=context.case_id_sha256,
            case_input_sha256=context.case_input_sha256,
            run_id_sha256=context.run_id_sha256,
            step_id_sha256=sha256_bytes(context.step_id.encode("utf-8")),
            actuation_path=context.actuation_path,
            fault_kind=context.fault_kind,
            gate=context.gate,
            driver_id=self._driver_id,
            driver_contract_sha256=self._contract_sha256,
            before_input_sha256=context.before_input_sha256,
            after_input_sha256=after_sha256,
            mutation_artifact_sha256=sha256_bytes(
                f"test {self.kind.value} fixture v1".encode()
            ),
            attestation_key_id=self.key_id,
        )
        return QualificationFaultMutation(
            receipt=sign_fault_mutation_receipt(
                receipt,
                private_key=self.private_key,
            ),
            replace_effect_verifier=replace_effect_verifier,
            effect_verifier=replacement,
        )


def _fault_workflow(
    tmp_path: Path,
    kind: QualificationCaseKind,
    driver: _FaultDriver,
    *,
    api_effect_only: bool = False,
    actuation_path: Literal["gui", "api"] = "gui",
) -> tuple[Workflow, Path]:
    bundle = tmp_path / f"bundle-{kind.value}"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "button.png").write_bytes(_screen_png())
    anchor = Anchor(
        template="templates/button.png",
        structural=StructuralLocator(selector="#submit"),
        region=(5, 5, 6, 6),
        click_point=(8, 8),
    )
    anchor.structured_identity = "Synthetic record"
    step = Step(
        id="submit",
        intent="Submit",
        action=ActionKind.CLICK,
        anchor=anchor,
        identity_armed=True,
        risk="irreversible",
    )
    if kind in {
        QualificationCaseKind.WEAK_EFFECT,
        QualificationCaseKind.MISSING_EFFECT,
    }:
        effects = [
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"record_id": ValueExpr(param="record_id")},
                risk="irreversible",
            )
        ]
        if api_effect_only:
            step.api_binding = ApiBinding(
                method="POST",
                url_template="/synthetic-records/{record_id}",
                effects=effects,
                identity=[
                    ApiIdentityBinding(
                        key="record_id",
                        param="record_id",
                        effect_field="record_id",
                        request_pointers=["/url/record_id"],
                    )
                ],
            )
        else:
            step.effects = effects
    backend = _ObservedBackend()
    observer = BackendQualificationEnvironmentObserver(backend)
    workflow = Workflow(name=f"fault-{kind.value}", surface="web", steps=[step])
    if api_effect_only or kind in {
        QualificationCaseKind.WEAK_EFFECT,
        QualificationCaseKind.MISSING_EFFECT,
    }:
        workflow.params["record_id"] = "synthetic-1"
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Fixture application",
            application_identity="https://fixture.example",
            application_version="1",
            environment_observer_id=observer.observer_id,
            environment_observer_contract_sha256=(
                BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256
            ),
            environment_digest=_ENVIRONMENT,
            runtime_version="test",
        ),
    )
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="submit",
            classification=ActionRiskClass.IRREVERSIBLE,
            explanation="qualification fault target changes business state",
            operator_confirmed=True,
        ),
    )
    add_case(
        workflow,
        QualificationCase(
            id="representative-1",
            kind=QualificationCaseKind.REPRESENTATIVE,
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    assert workflow.qualification is not None
    workflow.qualification.cases = [
        case.model_copy(
            update={
                "runtime_input_sha256": input_sha256,
                **(
                    {
                        "action_targets": [
                            QualificationActionTarget(
                                step_id="submit", actuation_path="gui"
                            )
                        ]
                    }
                    if case.kind is QualificationCaseKind.REPRESENTATIVE
                    else {
                        "action_targets": [
                            QualificationActionTarget(
                                step_id="submit", actuation_path=actuation_path
                            )
                        ]
                    }
                ),
            }
        )
        for case in workflow.qualification.cases
    ]
    workflow.qualification.trusted_fault_driver_keys[driver.key_id] = (
        driver.public_key_base64
    )
    workflow.save(bundle)
    return Workflow.load(bundle), bundle


def _fault_authorization(
    workflow: Workflow,
    kind: QualificationCaseKind,
    driver: _FaultDriver,
    *,
    run_id: str,
    actuation_path: Literal["gui", "api"] = "gui",
    action_paths: dict[str, Literal["gui", "api"]] | None = None,
    required_identity_step_ids: tuple[str, ...] = ("submit",),
    fault_step_id: str = "submit",
) -> GovernedRunAuthorization:
    assert workflow.manifest is not None and workflow.qualification is not None
    case_id = f"fault-{kind.value.replace('_', '-')}"
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    return GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=input_sha256,
        admitted_policy_name="clinical-write",
        admitted_policy_contract_sha256="e" * 64,
        execution_profile="standard",
        minimum_effect_tier=3,
        required_identity_step_ids=required_identity_step_ids,
        approval_source="qualification-campaign",
        qualification_project_id=workflow.qualification.project_id,
        qualification_project_revision=workflow.qualification.revision,
        qualification_project_contract_sha256=(
            workflow.qualification.contract_sha256()
        ),
        qualification_case_id=case_id,
        qualification_campaign_id_sha256=sha256_bytes(b"campaign"),
        qualification_case_input_sha256=input_sha256,
        qualification_run_id_sha256=sha256_bytes(run_id.encode("utf-8")),
        qualification_case_kind=kind.value,
        qualification_case_action_paths=(action_paths or {"submit": actuation_path}),
        qualification_fault_driver_id=driver._driver_id,
        qualification_fault_driver_contract_sha256=driver._contract_sha256,
        qualification_fault_driver_key_id=driver.key_id,
        qualification_fault_step_id_sha256=sha256_bytes(fault_step_id.encode("utf-8")),
    )


def _run_fault(
    tmp_path: Path,
    kind: QualificationCaseKind,
    driver: _FaultDriver,
    *,
    api_effect_only: bool = False,
    api_actuator: Any = None,
):
    actuation_path = "api" if api_effect_only and api_actuator is not None else "gui"
    workflow, bundle = _fault_workflow(
        tmp_path,
        kind,
        driver,
        api_effect_only=api_effect_only,
        actuation_path=actuation_path,
    )
    run_id = f"run-{kind.value}-{id(driver)}"
    backend = _ObservedBackend()
    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            kind,
            driver,
            run_id=run_id,
            actuation_path=actuation_path,
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        api_actuator=api_actuator,
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / f"run-{kind.value}",
        run_id=run_id,
        execution_target_kind="web",
    )
    return report, backend


def _two_write_fault_workflow(
    tmp_path: Path,
    driver: _FaultDriver,
) -> tuple[Workflow, Path]:
    bundle = tmp_path / "bundle-two-write-fault"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "button.png").write_bytes(_screen_png())

    def write_step(step_id: str) -> Step:
        return Step(
            id=step_id,
            intent=f"Write {step_id}",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="templates/button.png",
                structural=StructuralLocator(selector=f"#{step_id}"),
                region=(5, 5, 6, 6),
                click_point=(8, 8),
                structured_identity="Synthetic record",
            ),
            identity_armed=True,
            risk="irreversible",
            effects=[
                Effect(
                    kind=EffectKind.RECORD_WRITTEN,
                    match={"record_id": ValueExpr(literal=step_id)},
                    risk="irreversible",
                )
            ],
        )

    backend = _ObservedBackend()
    observer = BackendQualificationEnvironmentObserver(backend)
    workflow = Workflow(
        name="two-write-fault",
        surface="web",
        steps=[write_step("prepare"), write_step("submit")],
    )
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Fixture application",
            application_identity="https://fixture.example",
            application_version="1",
            environment_observer_id=observer.observer_id,
            environment_observer_contract_sha256=(
                BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256
            ),
            environment_digest=_ENVIRONMENT,
            runtime_version="test",
        ),
    )
    for step_id in ("prepare", "submit"):
        set_action_classification(
            workflow,
            ActionRiskClassification(
                step_id=step_id,
                classification=ActionRiskClass.IRREVERSIBLE,
                explanation="qualification fixture changes business state",
                operator_confirmed=True,
            ),
        )
    add_case(
        workflow,
        QualificationCase(
            id="representative-1",
            kind=QualificationCaseKind.REPRESENTATIVE,
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    paths = [
        QualificationActionTarget(step_id="prepare", actuation_path="gui"),
        QualificationActionTarget(step_id="submit", actuation_path="gui"),
    ]
    fault_target = QualificationActionTarget(
        step_id="submit",
        actuation_path="gui",
    )
    assert workflow.qualification is not None
    workflow.qualification.cases = [
        case.model_copy(
            update={
                "runtime_input_sha256": input_sha256,
                "action_targets": paths,
                "fault_target": (
                    None
                    if case.kind is QualificationCaseKind.REPRESENTATIVE
                    else fault_target
                ),
            }
        )
        for case in workflow.qualification.cases
    ]
    workflow.qualification.trusted_fault_driver_keys[driver.key_id] = (
        driver.public_key_base64
    )
    workflow.save(bundle)
    return Workflow.load(bundle), bundle


def test_later_fault_target_allows_prior_required_write_then_halts(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _two_write_fault_workflow(tmp_path, driver)
    run_id = "two-write-later-fault"
    backend = _ObservedBackend()
    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.MISSING_EFFECT,
            driver,
            run_id=run_id,
            action_paths={"prepare": "gui", "submit": "gui"},
            required_identity_step_ids=("prepare", "submit"),
            fault_step_id="submit",
        ),
        qualification_fault_driver=driver,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / "run-two-write-later-fault",
        run_id=run_id,
        execution_target_kind="web",
    )

    assert report.execution_outcome == "HALTED"
    assert [result.step_id for result in report.results] == ["prepare", "submit"]
    assert report.results[0].ok is True
    assert report.results[0].delivery_attempted is True
    assert report.results[0].effect_verified is True
    assert report.results[1].safety_halt is True
    assert report.results[1].delivery_attempted is False
    assert len(backend.actions) == 1
    assert len(report.qualification_fault_mutations) == 1
    receipt = report.qualification_fault_mutations[0]
    assert receipt.step_id_sha256 == sha256_bytes(b"submit")
    assert fault_detector_contract_error(report, receipt) is None

    assert (
        classify_execution_outcome(
            report,
            workflow,
            ExecutionProfile.STANDARD,
            _qualification_fault_target_step_id="submit",
        )
        is ExecutionOutcome.VERIFIED
    )
    insufficient_prior_evidence = report.model_copy(
        update={
            "results": [
                report.results[0].model_copy(update={"effect_evidence": []}),
                report.results[1],
            ]
        }
    )
    assert (
        classify_execution_outcome(
            insufficient_prior_evidence,
            workflow,
            ExecutionProfile.STANDARD,
            _qualification_fault_target_step_id="submit",
        )
        is ExecutionOutcome.COMPLETED_UNVERIFIED
    )


def test_authorization_rejects_fault_target_removed_from_permitted_scope(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    workflow, bundle = _two_write_fault_workflow(tmp_path, driver)
    assert workflow.qualification is not None
    case = next(
        item
        for item in workflow.qualification.cases
        if item.kind is QualificationCaseKind.MISSING_EFFECT
    )
    case.action_targets = [
        QualificationActionTarget(step_id="prepare", actuation_path="gui")
    ]
    workflow.save(bundle)
    authorization = _fault_authorization(
        workflow,
        QualificationCaseKind.MISSING_EFFECT,
        driver,
        run_id="fault-target-outside-scope",
        action_paths={"prepare": "gui"},
        required_identity_step_ids=("prepare", "submit"),
        fault_step_id="submit",
    )

    assert authorization.validate_workflow(workflow) == (
        "qualification fault target is outside its permitted action scope"
    )


@pytest.mark.parametrize(
    ("kind", "expected_stage", "expected_code"),
    [
        (QualificationCaseKind.AMBIGUITY, "target_resolution", "target_ambiguous"),
        (
            QualificationCaseKind.WRONG_IDENTITY,
            "identity_verification",
            "identity_conflict",
        ),
        (
            QualificationCaseKind.STALE_IDENTITY,
            "actuation_revalidation",
            "actuation_observation_changed",
        ),
        (
            QualificationCaseKind.WEAK_EFFECT,
            "effect_strength",
            "effect_strength_insufficient",
        ),
        (
            QualificationCaseKind.MISSING_EFFECT,
            "effect_verifier",
            "effect_verifier_missing",
        ),
    ],
)
def test_all_fault_cases_mutate_real_detector_input_and_halt_before_delivery(
    tmp_path: Path,
    kind: QualificationCaseKind,
    expected_stage: str,
    expected_code: str,
) -> None:
    driver = _FaultDriver(kind)
    report, backend = _run_fault(tmp_path, kind, driver)

    assert report.execution_outcome == "HALTED"
    assert len(report.qualification_fault_mutations) == 1
    receipt = report.qualification_fault_mutations[0]
    assert report.governed_qualification_project_id is not None
    assert report.governed_qualification_project_revision == receipt.project_revision
    assert report.governed_qualification_project_contract_sha256 is not None
    assert report.governed_qualification_fault_driver_key_id == driver.key_id
    assert report.governed_qualification_fault_step_id_sha256 == sha256_bytes(b"submit")
    result = report.results[0]
    assert result.delivery_attempted is False
    assert result.safety_refusal_evidence is not None
    assert result.safety_refusal_evidence.stage == expected_stage
    assert result.safety_refusal_evidence.code == expected_code
    assert (
        fault_detector_contract_error(report, report.qualification_fault_mutations[0])
        is None
    )
    assert backend.actions == []


@pytest.mark.parametrize("delivery_attempted", [None, True])
def test_detector_receipt_rejects_unknown_or_attempted_delivery(
    delivery_attempted: bool | None,
) -> None:
    receipt = FaultMutationReceipt(
        project_id="project",
        project_revision=1,
        project_contract_sha256="a" * 64,
        campaign_id_sha256="b" * 64,
        case_id_sha256="c" * 64,
        case_input_sha256="d" * 64,
        run_id_sha256="e" * 64,
        step_id_sha256=sha256_bytes(b"submit"),
        actuation_path="gui",
        fault_kind="ambiguity",
        gate="target_resolution",
        driver_id="driver",
        driver_contract_sha256="f" * 64,
        before_input_sha256="1" * 64,
        after_input_sha256="2" * 64,
        mutation_artifact_sha256="3" * 64,
        attestation_key_id="key",
    )
    result = StepResult(
        step_id="submit",
        intent="Submit",
        ok=False,
        safety_halt=True,
        delivery_attempted=delivery_attempted,
        safety_refusal_evidence=SafetyRefusalEvidence(
            stage="target_resolution",
            code="target_ambiguous",
            detector_input_sha256="2" * 64,
        ),
    )
    report = SimpleNamespace(
        execution_outcome="HALTED",
        results=[result],
        governed_qualification_case_action_paths={"submit": "gui"},
    )

    assert (
        fault_detector_contract_error(report, receipt)
        == "fault_detector_delivery_boundary_crossed"
    )


def test_detector_receipt_rejects_any_result_after_the_fault_refusal() -> None:
    receipt = FaultMutationReceipt(
        project_id="project",
        project_revision=1,
        project_contract_sha256="a" * 64,
        campaign_id_sha256="b" * 64,
        case_id_sha256="c" * 64,
        case_input_sha256="d" * 64,
        run_id_sha256="e" * 64,
        step_id_sha256=sha256_bytes(b"submit"),
        actuation_path="gui",
        fault_kind="ambiguity",
        gate="target_resolution",
        driver_id="driver",
        driver_contract_sha256="f" * 64,
        before_input_sha256="1" * 64,
        after_input_sha256="2" * 64,
        mutation_artifact_sha256="3" * 64,
        attestation_key_id="key",
    )
    refusal = StepResult(
        step_id="submit",
        intent="Submit",
        ok=False,
        safety_halt=True,
        delivery_attempted=False,
        safety_refusal_evidence=SafetyRefusalEvidence(
            stage="target_resolution",
            code="target_ambiguous",
            detector_input_sha256="2" * 64,
        ),
    )
    later_action = StepResult(
        step_id="later-write",
        intent="Later write",
        ok=True,
        delivery_attempted=True,
        effect_verified=True,
        effect_results=["later effect was reported as verified"],
    )
    report = SimpleNamespace(
        execution_outcome="HALTED",
        results=[refusal, later_action],
        governed_qualification_case_action_paths={"submit": "gui"},
    )

    assert (
        fault_detector_contract_error(report, receipt)
        == "fault_detector_refusal_not_terminal"
    )

    program_terminal = StepResult(
        step_id="<terminal>",
        intent="program halt",
        ok=False,
        safety_halt=True,
    )
    program_report = SimpleNamespace(
        execution_outcome="HALTED",
        terminal_outcome="halt",
        results=[refusal, program_terminal],
        governed_qualification_case_action_paths={"submit": "gui"},
    )
    assert fault_detector_contract_error(program_report, receipt) is None


def test_fault_receipt_signature_rejects_wrong_key_and_tampering() -> None:
    key = Ed25519PrivateKey.generate()
    private_key = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_key = base64.b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    receipt = FaultMutationReceipt(
        project_id="project",
        project_revision=1,
        project_contract_sha256="a" * 64,
        campaign_id_sha256="b" * 64,
        case_id_sha256="c" * 64,
        case_input_sha256="d" * 64,
        run_id_sha256="e" * 64,
        step_id_sha256="f" * 64,
        actuation_path="gui",
        fault_kind="ambiguity",
        gate="target_resolution",
        driver_id="driver",
        driver_contract_sha256="1" * 64,
        before_input_sha256="2" * 64,
        after_input_sha256="3" * 64,
        mutation_artifact_sha256="4" * 64,
        attestation_key_id="key",
    )
    signed = sign_fault_mutation_receipt(receipt, private_key=private_key)
    wrong_key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )

    assert verify_fault_mutation_receipt(signed, trusted_public_key_base64=public_key)
    assert not verify_fault_mutation_receipt(
        signed,
        trusted_public_key_base64=base64.b64encode(wrong_key).decode("ascii"),
    )
    assert not verify_fault_mutation_receipt(
        signed.model_copy(update={"case_input_sha256": "5" * 64}),
        trusted_public_key_base64=public_key,
    )


def test_qualification_authorization_rejects_caller_supplied_case_input_digest(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.AMBIGUITY)
    workflow, _bundle = _fault_workflow(
        tmp_path,
        QualificationCaseKind.AMBIGUITY,
        driver,
    )
    authorization = _fault_authorization(
        workflow,
        QualificationCaseKind.AMBIGUITY,
        driver,
        run_id="exact-input-run",
    )

    with pytest.raises(ValueError, match="exact governed runtime-input digest"):
        GovernedRunAuthorization.model_validate(
            {
                **authorization.model_dump(mode="json"),
                "qualification_case_input_sha256": "0" * 64,
            }
        )


@pytest.mark.parametrize("field", ["id", "contract", "key"])
def test_driver_binding_change_is_rejected_before_mutation(
    tmp_path: Path, field: str
) -> None:
    driver = _FaultDriver(QualificationCaseKind.AMBIGUITY, change_binding=field)
    report, backend = _run_fault(tmp_path, QualificationCaseKind.AMBIGUITY, driver)

    assert report.execution_outcome in {"FAILED", "HALTED"}
    assert report.qualification_fault_mutations == []
    assert backend.actions == []
    assert "qualification_fault_driver_binding_changed" in (
        report.results[0].error or ""
    )


def test_declined_fault_cannot_cross_the_bound_input_edge(tmp_path: Path) -> None:
    driver = _FaultDriver(QualificationCaseKind.AMBIGUITY, decline=True)
    report, backend = _run_fault(tmp_path, QualificationCaseKind.AMBIGUITY, driver)

    assert report.execution_outcome == "HALTED"
    assert report.qualification_fault_mutations == []
    assert backend.actions == []
    assert "did not produce its required detector refusal" in (
        report.results[0].error or ""
    )


def test_inactive_api_path_does_not_consume_a_fault_mutation(tmp_path: Path) -> None:
    driver = _FaultDriver(QualificationCaseKind.MISSING_EFFECT)
    report, backend = _run_fault(
        tmp_path,
        QualificationCaseKind.MISSING_EFFECT,
        driver,
        api_effect_only=True,
    )

    assert report.execution_outcome == "HALTED"
    assert driver.calls == 0
    assert report.qualification_fault_mutations == []
    assert backend.actions == []


def test_api_fault_mutation_cannot_fall_through_to_gui_when_api_is_unavailable(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(
        QualificationCaseKind.WEAK_EFFECT,
        non_refusing_effect_mutation=True,
    )
    actuator = _UnavailableApiActuator()

    report, backend = _run_fault(
        tmp_path,
        QualificationCaseKind.WEAK_EFFECT,
        driver,
        api_effect_only=True,
        api_actuator=actuator,
    )

    assert report.execution_outcome == "HALTED"
    assert actuator.calls == 1
    assert len(report.qualification_fault_mutations) == 1
    assert backend.actions == []
    assert report.results[0].delivery_attempted is False
    assert report.results[0].safety_refusal_evidence is None
    assert "requires the API actuation path" in (report.results[0].error or "")
    assert (
        fault_detector_contract_error(
            report,
            report.qualification_fault_mutations[0],
        )
        == "fault_detector_refusal_not_observed"
    )


def test_gui_qualification_path_bypasses_a_configured_api_tier(
    tmp_path: Path,
) -> None:
    driver = _FaultDriver(QualificationCaseKind.AMBIGUITY)
    workflow, bundle = _fault_workflow(
        tmp_path,
        QualificationCaseKind.AMBIGUITY,
        driver,
        actuation_path="gui",
    )
    workflow.steps[0].api_binding = ApiBinding(
        method="POST",
        url_template="/synthetic-submit",
    )
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    run_id = "gui-path-bypasses-api"
    actuator = _UnavailableApiActuator()
    backend = _ObservedBackend()

    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=_fault_authorization(
            workflow,
            QualificationCaseKind.AMBIGUITY,
            driver,
            run_id=run_id,
            actuation_path="gui",
        ),
        qualification_fault_driver=driver,
        api_actuator=actuator,
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / "run-gui-bypasses-api",
        run_id=run_id,
        execution_target_kind="web",
    )

    assert actuator.calls == 0
    assert report.execution_outcome == "HALTED"
    assert len(report.qualification_fault_mutations) == 1
    assert report.qualification_fault_mutations[0].actuation_path == "gui"


def test_qualification_run_halts_before_an_undeclared_write(tmp_path: Path) -> None:
    driver = _FaultDriver(QualificationCaseKind.WEAK_EFFECT)
    workflow, bundle = _fault_workflow(
        tmp_path,
        QualificationCaseKind.WEAK_EFFECT,
        driver,
    )
    workflow.steps.append(Step(id="inspect", intent="Inspect", action=ActionKind.WAIT))
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="inspect",
            classification=ActionRiskClass.READ_ONLY,
            explanation="The inspection step does not change business state",
            operator_confirmed=True,
        ),
    )
    project = workflow.qualification
    assert project is not None
    representative = next(
        case
        for case in project.cases
        if case.kind is QualificationCaseKind.REPRESENTATIVE
    )
    representative.action_targets = [
        QualificationActionTarget(step_id="inspect", actuation_path="gui")
    ]
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    assert workflow.manifest is not None and workflow.qualification is not None
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    run_id = "undeclared-write"
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=input_sha256,
        admitted_policy_name="clinical-write",
        admitted_policy_contract_sha256="e" * 64,
        execution_profile="standard",
        minimum_effect_tier=3,
        required_identity_step_ids=("submit",),
        approval_source="qualification-campaign",
        qualification_project_id=workflow.qualification.project_id,
        qualification_project_revision=workflow.qualification.revision,
        qualification_project_contract_sha256=(
            workflow.qualification.contract_sha256()
        ),
        qualification_case_id=representative.id,
        qualification_campaign_id_sha256=sha256_bytes(b"campaign"),
        qualification_case_input_sha256=input_sha256,
        qualification_run_id_sha256=sha256_bytes(run_id.encode()),
        qualification_case_kind="representative",
        qualification_case_action_paths={"inspect": "gui"},
    )
    backend = _ObservedBackend()

    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=authorization,
        effect_verifier=_StrongEffectVerifier(),
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / "run-undeclared-write",
        run_id=run_id,
        execution_target_kind="web",
    )

    assert report.execution_outcome == "HALTED"
    assert backend.actions == []
    assert "outside its exact authorized actuation-path map" in (
        report.results[0].error or ""
    )


class _ExternalObserver:
    def __init__(
        self,
        *,
        target_kind: str,
        change_field: str | None = None,
    ) -> None:
        self.target_kind = target_kind
        self.change_field = change_field
        self.calls = 0
        self.observer_id_reads = 0
        self.contract_reads = 0
        self._shared_observation: QualificationEnvironmentObservation | None = None

    @property
    def observer_id(self) -> str:
        self.observer_id_reads += 1
        if self.change_field == "observer_id" and self.observer_id_reads > 4:
            return "test.replacement-environment"
        return "test.external-environment"

    @property
    def contract_sha256(self) -> str:
        self.contract_reads += 1
        if self.change_field == "observer_contract" and self.contract_reads > 4:
            return "8" * 64
        return "9" * 64

    def observe(self, _backend: Any, _target_kind: str):
        self.calls += 1
        values = {
            "target_kind": self.target_kind,
            "application_identity": "third-party-app",
            "application_version": "2026.7",
            "session_identity_sha256": "6" * 64,
            "environment_digest": "7" * 64,
        }
        if self.change_field == "reused_object":
            if self._shared_observation is None:
                self._shared_observation = (
                    QualificationEnvironmentObservation.model_validate(values)
                )
            elif self.calls > 1:
                self._shared_observation.environment_digest = "8" * 64
            return self._shared_observation
        if self.calls > 1 and self.change_field is not None:
            if self.change_field == "application_identity":
                values[self.change_field] = "replacement-app"
            elif self.change_field == "application_version":
                values[self.change_field] = "2026.8"
            else:
                values[self.change_field] = "8" * 64
        return QualificationEnvironmentObservation.model_validate(values)


@pytest.mark.parametrize("surface", ["web", "windows", "macos", "linux"])
@pytest.mark.parametrize(
    "change_field",
    [
        "application_identity",
        "application_version",
        "session_identity_sha256",
        "environment_digest",
        "observer_id",
        "observer_contract",
        "reused_object",
    ],
)
def test_external_observer_rechecks_every_environment_signal_before_input(
    tmp_path: Path,
    surface: str,
    change_field: str,
) -> None:
    backend = _ObservedBackend()
    observer = _ExternalObserver(target_kind=surface, change_field=change_field)
    bundle = tmp_path / f"environment-{surface}-{change_field}"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "button.png").write_bytes(_screen_png())
    workflow = Workflow(
        name="environment-bound",
        surface="web",
        steps=[
            Step(
                id="submit",
                intent="Submit",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/button.png",
                    structural=StructuralLocator(selector="#submit"),
                    region=(5, 5, 6, 6),
                    click_point=(8, 8),
                    structured_identity="Synthetic record",
                ),
                identity_armed=True,
                risk="irreversible",
            )
        ],
    )
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind=surface,
            application="Third-party application",
            application_identity="third-party-app",
            application_version="2026.7",
            environment_observer_id=observer.observer_id,
            environment_observer_contract_sha256=observer.contract_sha256,
            environment_digest="7" * 64,
            runtime_version="test",
        ),
    )
    set_action_classification(
        workflow,
        ActionRiskClassification(
            step_id="submit",
            classification=ActionRiskClass.IRREVERSIBLE,
            explanation="qualification environment target changes business state",
            operator_confirmed=True,
        ),
    )
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    add_case(
        workflow,
        QualificationCase(
            id="representative-1",
            kind=QualificationCaseKind.REPRESENTATIVE,
            runtime_input_sha256=input_sha256,
            action_targets=[
                QualificationActionTarget(step_id="submit", actuation_path="gui")
            ],
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    assert workflow.manifest is not None and workflow.qualification is not None
    run_id = f"environment-{surface}-{change_field}"
    input_sha256 = runtime_inputs_digest(workflow, None, None)
    authorization = GovernedRunAuthorization(
        bundle_content_digest=workflow.manifest.content_digest,
        runtime_inputs_digest=input_sha256,
        admitted_policy_name="clinical-write",
        admitted_policy_contract_sha256="5" * 64,
        execution_profile="standard",
        minimum_effect_tier=3,
        required_identity_step_ids=("submit",),
        approval_source="qualification-campaign",
        qualification_project_id=workflow.qualification.project_id,
        qualification_project_revision=workflow.qualification.revision,
        qualification_project_contract_sha256=(
            workflow.qualification.contract_sha256()
        ),
        qualification_case_id="representative-1",
        qualification_campaign_id_sha256="1" * 64,
        qualification_case_input_sha256=input_sha256,
        qualification_run_id_sha256=sha256_bytes(run_id.encode("utf-8")),
        qualification_case_kind="representative",
        qualification_case_action_paths={"submit": "gui"},
    )
    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=authorization,
        qualification_environment_observer=observer,
        durable=True,
        require_settled=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=tmp_path / f"environment-run-{surface}-{change_field}",
        run_id=run_id,
        execution_target_kind=surface,
    )

    assert report.execution_outcome in {"FAILED", "HALTED"}
    assert backend.actions == []
    assert report.observed_environment_digest == "7" * 64
    assert report.observed_environment_binding_sha256 is not None
    expected_refusal = (
        "qualification environment observer binding changed before input"
        if change_field in {"observer_id", "observer_contract"}
        else "qualification environment changed before input"
    )
    assert expected_refusal in (report.results[0].error or "")
    assert backend._input_guard is None


def test_playwright_external_observer_does_not_require_target_owned_meta_tags() -> None:
    class _Page:
        url = "https://third-party.example/private/path"

    backend = PlaywrightBackend(_Page())  # type: ignore[arg-type]
    observer = _ExternalObserver(target_kind="web")

    observed = observer.observe(backend, "web")

    assert observed.application_identity == "third-party-app"
    assert observed.environment_digest == "7" * 64


def test_real_deployment_constructor_wires_backend_environment_observer() -> None:
    backend = _ObservedBackend()
    replayer = build_replayer(
        backend,
        allow_egress=False,
        effect_verifier=None,
        api_actuator=None,
        durable=True,
        use_structural=False,
        governed_authorization=SimpleNamespace(
            qualification_case_id="case-1",
            execution_profile=None,
        ),
    )

    assert isinstance(
        replayer.qualification_environment_observer,
        BackendQualificationEnvironmentObserver,
    )

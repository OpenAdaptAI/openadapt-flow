"""Build a public, synthetic Frappe Lending governed-presentation pack.

This exporter runs the real pinned Frappe fixture.  It records and compiles one
browser demonstration, qualifies the compiled workflow, then executes three
fresh Standard-profile trials under both the baseline and the qualified
cosmetic-drift condition.  Every trial is checked by an authenticated read-only
Frappe REST session, direct SQL, exact table deltas, and a non-target-record
digest.  Presentation overlays are derived only from runtime-retained frames.

The Frappe/Lending fixture remains repository-only under its upstream license;
it is never copied into a wheel, sdist, or the generated presentation pack.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image

from benchmark.frappe_lending.fixture import FrappeFixture
from openadapt_flow import __version__ as FLOW_VERSION
from openadapt_flow.benchmark.frappe_lending import (
    CONDITIONS,
    FrappeLoanApplicationOracle,
    LoanApplicationSpec,
    classify_trial,
)
from openadapt_flow.deployment import DeploymentConfig, PolicySection
from openadapt_flow.execution_profiles import (
    ExecutionProfile,
    execution_profile_contract,
)
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.policy import load_policy
from openadapt_flow.qualification import (
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    EvidenceRef,
    IdentityEnforcement,
    IdentityPolicy,
    QualificationCase,
    QualificationCaseKind,
    QualificationCaseResult,
    QualificationOutcome,
    QualificationReport,
    add_case,
    certify_project,
    init_project,
    record_case_results,
    save_qualified_workflow,
    set_action_classification,
    set_effect_policy,
    set_identity_policy,
    set_trusted_runner_key,
    sign_case_result,
    workflow_contract_sha256,
)
from openadapt_flow.run_gate import build_runtime_authorization, evaluate_run_gate
from openadapt_flow.runtime import Replayer
from openadapt_flow.verification import VerificationTier
from scripts.export_public_demo_evidence import (
    PRESENTATION_PTS_SCHEMA_VERSION,
    _PresentationCapture,
    _probe_frame_presentation_times_us,
    _strip_run_authority,
    _write_presentation_clip,
)
from scripts.frappe_lending_demo import (
    FrappePlaywrightBackend,
    _apply_condition,
    _capture_post_evidence,
    _login_session,
    _preauthenticate_browser,
    _tree_manifest_sha256,
    compile_recording,
    record,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_SCHEMA = "openadapt.public-cross-surface-presentation/v1"
INVENTORY_SCHEMA = "openadapt.public-pack-inventory/v1"
BINDING_SCHEMA = "openadapt.public-presentation-media-binding/v1"
CONTEXT_SCHEMA = "openadapt.public-presentation-contexts/v2"
RUNNER_KEY_ID = "frappe-public-evidence-runner"
TRIALS_PER_CONDITION = 3
PACK_ID = "frappe-lending-loan-application-standard-synthetic-v1"


class ExportError(RuntimeError):
    """The pack could not be produced without weakening its evidence contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _source_provenance(*, allow_dirty: bool) -> dict[str, Any]:
    dirty = bool(_git("status", "--porcelain", "--untracked-files=normal"))
    if dirty and not allow_dirty:
        raise ExportError(
            "commit the exporter before creating immutable public evidence, "
            "or pass --allow-dirty for local development only"
        )
    return {
        "runtime_repository": "https://github.com/OpenAdaptAI/openadapt-flow",
        "runtime_source_commit": _git("rev-parse", "HEAD"),
        "runtime_source_tree_clean": not dirty,
        "exporter": "scripts/export_frappe_lending_evidence.py",
        "openadapt_flow_version": FLOW_VERSION,
    }


def _asset(path: Path, root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": path.relative_to(root).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        with Image.open(path) as image:
            result.update(width=image.width, height=image.height)
    elif path.suffix.lower() in {".mp4", ".webm"}:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,codec_name",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        streams = json.loads(probe.stdout).get("streams")
        if not isinstance(streams, list) or len(streams) != 1:
            raise ExportError(f"expected one video stream in {path}")
        result.update(
            width=streams[0]["width"],
            height=streams[0]["height"],
            codec=streams[0]["codec_name"],
        )
    return result


def _risk_class(step: Any, *, final_step_id: str) -> ActionRiskClassification:
    if step.id == final_step_id:
        classification = ActionRiskClass.CONSEQUENTIAL
        explanation = (
            "Save creates the Loan Application in the system of record and "
            "therefore requires identity and independent effect coverage."
        )
    else:
        classification = ActionRiskClass.READ_ONLY
        explanation = (
            "This is a preparatory draft-form action. It has no independent "
            "persisted business effect; the final Save owns the write contract."
        )
    return ActionRiskClassification(
        step_id=step.id,
        classification=classification,
        explanation=explanation,
        operator_confirmed=True,
    )


def _configure_qualification(
    workflow: Workflow,
    *,
    fixture: FrappeFixture,
) -> tuple[bytes, str]:
    image = fixture.image_identity()
    lock = json.loads(
        (REPO_ROOT / "benchmark/frappe_lending/environment.lock.json").read_text()
    )
    environment_payload = {
        "target_kind": "web",
        "application": "Frappe Lending",
        "application_version": lock["upstreams"]["lending"]["ref"],
        "source_commits": {
            name: item["commit"] for name, item in lock["upstreams"].items()
        },
        "image": image,
        "runtime_version": FLOW_VERSION,
        "viewport": [1280, 800],
    }
    environment_digest = _sha256_json(environment_payload)
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Frappe Lending synthetic reference",
            application_version=lock["upstreams"]["lending"]["ref"],
            environment_digest=environment_digest,
            runtime_version=FLOW_VERSION,
            required_capabilities=[
                "direct_sql_delta_audit",
                "independent_authenticated_rest",
                "playwright_dom",
            ],
        ),
        minimum_effect_tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    final = workflow.steps[-1]
    if not final.effects or not final.identity_armed:
        raise ExportError("compiled Save lacks effect or identity evidence")
    for step in workflow.steps:
        set_action_classification(
            workflow,
            _risk_class(step, final_step_id=final.id),
        )
    set_identity_policy(
        workflow,
        IdentityPolicy(
            step_id=final.id,
            enforcement=IdentityEnforcement.CANONICAL_LADDER,
        ),
    )
    for effect_index in range(len(final.effects)):
        set_effect_policy(
            workflow,
            step_id=final.id,
            effect_index=effect_index,
            tier=VerificationTier.INDEPENDENT_SYSTEM,
        )
    for condition in CONDITIONS:
        add_case(
            workflow,
            QualificationCase(
                id=f"representative-{condition.replace('_', '-')}",
                kind=QualificationCaseKind.REPRESENTATIVE,
                expected_outcome=QualificationOutcome.VERIFIED,
                description=(
                    f"Pinned Frappe Lending Loan Application under {condition}"
                ),
            ),
        )

    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    set_trusted_runner_key(
        workflow,
        key_id=RUNNER_KEY_ID,
        public_key_base64=base64.b64encode(public_raw).decode("ascii"),
    )
    return private_raw, environment_digest


def _evidence_ref(root: Path, path: Path, *, kind: str) -> EvidenceRef:
    return EvidenceRef(
        kind=kind,  # type: ignore[arg-type]
        sha256=_sha256(path),
        relative_path=path.relative_to(root).as_posix(),
    )


def _signed_case_result(
    *,
    root: Path,
    workflow: Workflow,
    case_id: str,
    observed_outcome: QualificationOutcome,
    evidence_paths: list[tuple[Path, str]],
    detail_code: str,
    private_key: bytes,
) -> QualificationCaseResult:
    project = workflow.qualification
    if project is None:
        raise ExportError("qualification project disappeared")
    result = QualificationCaseResult(
        case_id=case_id,
        project_id=project.project_id,
        project_revision=project.revision,
        project_contract_sha256=project.contract_sha256(),
        workflow_contract_sha256=workflow_contract_sha256(workflow),
        environment_contract_sha256=project.environment.contract_sha256(),
        environment_digest=project.environment.environment_digest,
        runtime_version=project.environment.runtime_version,
        runner_id="openadapt-flow/frappe-public-evidence",
        runner_capabilities=list(project.environment.required_capabilities),
        status="passed",
        observed_outcome=observed_outcome,
        evidence=[
            _evidence_ref(root, path, kind=kind) for path, kind in evidence_paths
        ],
        detail_code=detail_code,
        attestation_key_id=RUNNER_KEY_ID,
    )
    return sign_case_result(result, private_key=private_key)


def _case_result(
    *,
    root: Path,
    workflow: Workflow,
    condition: str,
    trial_dirs: list[Path],
    private_key: bytes,
) -> QualificationCaseResult:
    evidence: list[tuple[Path, str]] = []
    for trial_dir in trial_dirs:
        evidence.extend(
            [
                (trial_dir / "run/report.json", "run_report"),
                (trial_dir / "oracle.json", "effect"),
            ]
        )
    return _signed_case_result(
        root=root,
        workflow=workflow,
        case_id=f"representative-{condition.replace('_', '-')}",
        observed_outcome=QualificationOutcome.VERIFIED,
        evidence_paths=evidence,
        detail_code=f"{condition}.three-trial-contract",
        private_key=private_key,
    )


class _StaleFrappeIdentityBackend(FrappePlaywrightBackend):
    """Change the live form identity after returning one exact observation."""

    def __init__(self, page: Any) -> None:
        super().__init__(page)
        self._changed = False

    def structured_text_at(self, x: int, y: int) -> str | None:
        observed = super().structured_text_at(x, y)
        if observed is not None and not self._changed:
            self._changed = True
            self.page.evaluate(
                """() => {
                    window.cur_frm.doc.applicant = 'Changed Synthetic Applicant';
                    window.cur_frm.refresh_field('applicant');
                }"""
            )
        return observed


def _fault_oracle(
    *,
    fixture: FrappeFixture,
    oracle: FrappeLoanApplicationOracle,
    before_counts: dict[str, int],
    before_non_target: str,
) -> dict[str, Any]:
    evidence = _capture_post_evidence(
        oracle,
        fixture,
        before_counts=before_counts,
        before_non_target_loan_sha256=before_non_target,
        arm="compiled",
    )
    nonzero = {
        name: value
        for name, value in (evidence.all_table_deltas or {}).items()
        if value
    }
    no_write = (
        evidence.readable
        and evidence.rest_records == []
        and evidence.db_records == []
        and nonzero == {}
        and evidence.errors == []
        and evidence.before_non_target_loan_sha256
        == evidence.after_non_target_loan_sha256
    )
    return {
        "schema_version": "openadapt.frappe-fault-oracle/v1",
        "no_target_write": no_write,
        "rest_record_count": len(evidence.rest_records or []),
        "sql_record_count": len(evidence.db_records or []),
        "nonzero_table_deltas": nonzero,
        "delta_violations": evidence.delta_violations,
        "oracle_errors": evidence.errors,
        "non_target_state_unchanged": (
            evidence.before_non_target_loan_sha256
            == evidence.after_non_target_loan_sha256
        ),
    }


def _run_ui_fault_case(
    *,
    fixture: FrappeFixture,
    workflow: Workflow,
    bundle_dir: Path,
    case_id: str,
    fault_dir: Path,
) -> list[tuple[Path, str]]:
    fixture.reset()
    reader = _login_session(
        fixture.base_url,
        "openadapt.oracle@example.invalid",
        "openadapt-local-oracle",
    )
    oracle = FrappeLoanApplicationOracle(fixture.base_url, reader)
    backend, close = FrappePlaywrightBackend.launch(fixture.base_url, headless=True)
    report: RunReport | None = None
    before_counts: dict[str, int] | None = None
    before_non_target: str | None = None
    try:
        try:
            _preauthenticate_browser(backend, fixture)
            if case_id == "fault-ambiguity":
                backend.page.evaluate(
                    """() => {
                        const original = document.querySelector(
                            'button.primary-action[data-label="Save"]'
                        );
                        const clone = original.cloneNode(true);
                        clone.id = 'openadapt-ambiguous-save';
                        original.parentElement.appendChild(clone);
                    }"""
                )
                active_backend: FrappePlaywrightBackend = backend
            elif case_id == "fault-wrong-identity":
                backend.page.evaluate(
                    """() => {
                        window.cur_frm.doc.applicant = 'Wrong Synthetic Applicant';
                        window.cur_frm.refresh_field('applicant');
                    }"""
                )
                active_backend = backend
            elif case_id == "fault-stale-identity":
                active_backend = _StaleFrappeIdentityBackend(backend.page)
            else:
                raise ExportError(f"unknown UI fault case {case_id}")
            before_counts = fixture.table_counts()
            before_non_target = fixture.non_target_loan_applications_sha256()
            gate = evaluate_run_gate(
                workflow,
                bundle_dir=bundle_dir,
                deployment=DeploymentConfig(
                    policy=PolicySection(policy="clinical-write")
                ),
                effect_verifier=oracle.verifier,
                profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
                effective_durable=True,
                effective_require_settled=True,
            )
            if not gate.passed:
                raise ExportError(gate.render())
            params = LoanApplicationSpec().params()
            authorization = build_runtime_authorization(
                workflow,
                gate,
                approval_source=f"frappe-public-{case_id}",
                params=params,
            )
            report = Replayer(
                active_backend,
                effect_verifier=oracle.verifier,
                governed_authorization=authorization,
                durable=True,
                require_settled=True,
            ).run(
                workflow.model_copy(deep=True),
                params=params,
                bundle_dir=bundle_dir,
                run_dir=fault_dir / "run",
                execution_target_kind="web",
                execution_origin=fixture.base_url,
                execution_entry_url=backend.page.url,
            )
        finally:
            close()
        if report is None or report.execution_outcome != "HALTED":
            raise ExportError(f"{case_id} did not produce a governed HALTED report")
        _strip_run_authority(fault_dir / "run")
        assert before_counts is not None and before_non_target is not None
        fault_oracle = _fault_oracle(
            fixture=fixture,
            oracle=oracle,
            before_counts=before_counts,
            before_non_target=before_non_target,
        )
        fault_oracle.update(
            case_id=case_id,
            execution_outcome=report.execution_outcome,
            model_calls=report.model_calls,
        )
        oracle_path = _write_json(fault_dir / "oracle.json", fault_oracle)
        if not fault_oracle["no_target_write"] or report.model_calls != 0:
            raise ExportError(f"{case_id} violated its no-write oracle")
        return [
            (fault_dir / "run/report.json", "run_report"),
            (oracle_path, "effect"),
        ]
    finally:
        reader.close()


def _run_gate_fault_case(
    *,
    workflow: Workflow,
    bundle_dir: Path,
    oracle: FrappeLoanApplicationOracle,
    case_id: str,
    fault_dir: Path,
) -> list[tuple[Path, str]]:
    candidate = workflow.model_copy(deep=True)
    verifier: object | None = oracle.verifier
    if case_id == "fault-weak-effect":

        class WeakVerifier:
            verification_tier = VerificationTier.IMMEDIATE_SCREEN

        verifier = WeakVerifier()
    elif case_id == "fault-missing-effect":
        candidate.steps[-1].effects = []
    else:
        raise ExportError(f"unknown gate fault case {case_id}")
    gate = evaluate_run_gate(
        candidate,
        bundle_dir=bundle_dir,
        deployment=DeploymentConfig(policy=PolicySection(policy="clinical-write")),
        effect_verifier=verifier,
        profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
        effective_durable=True,
        effective_require_settled=True,
    )
    if gate.passed:
        raise ExportError(f"{case_id} was unexpectedly admitted")
    evidence_path = _write_json(
        fault_dir / "gate-refusal.json",
        {
            "schema_version": "openadapt.frappe-gate-fault/v1",
            "case_id": case_id,
            "execution_outcome": "HALTED",
            "actuation_attempted": False,
            "gate": gate.model_dump(mode="json"),
        },
    )
    return [(evidence_path, "fault_campaign")]


def _run_trial(
    *,
    fixture: FrappeFixture,
    workflow: Workflow,
    bundle_dir: Path,
    condition: str,
    trial: int,
    trial_dir: Path,
    presentation_dir: Path,
    pack_id: str,
) -> tuple[RunReport, dict[str, Any]]:
    baseline_hash = fixture.reset()
    reader = _login_session(
        fixture.base_url,
        "openadapt.oracle@example.invalid",
        "openadapt-local-oracle",
    )
    oracle = FrappeLoanApplicationOracle(fixture.base_url, reader)
    backend, close = FrappePlaywrightBackend.launch(fixture.base_url, headless=True)
    presentation = (
        _PresentationCapture(mode="governed")
        if condition == "baseline" and trial == 1
        else None
    )
    before_counts = None
    before_non_target = None
    report: RunReport | None = None
    entry_url = ""
    try:
        try:
            _preauthenticate_browser(backend, fixture)
            _apply_condition(backend, condition)
            before_counts = fixture.table_counts()
            before_non_target = fixture.non_target_loan_applications_sha256()
            gate = evaluate_run_gate(
                workflow,
                bundle_dir=bundle_dir,
                deployment=DeploymentConfig(
                    policy=PolicySection(policy="clinical-write")
                ),
                effect_verifier=oracle.verifier,
                profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
                effective_durable=True,
                effective_require_settled=True,
            )
            if not gate.passed:
                raise ExportError(gate.render())
            params = LoanApplicationSpec().params()
            authorization = build_runtime_authorization(
                workflow,
                gate,
                approval_source="frappe-public-qualified-campaign",
                params=params,
            )
            entry_url = backend.page.url
            report = Replayer(
                backend,
                effect_verifier=oracle.verifier,
                governed_authorization=authorization,
                durable=True,
                require_settled=True,
                control_overlay=(presentation.emitter if presentation else None),
            ).run(
                workflow.model_copy(deep=True),
                params=params,
                bundle_dir=bundle_dir,
                run_dir=trial_dir / "run",
                execution_target_kind="web",
                execution_origin=fixture.base_url,
                execution_entry_url=entry_url,
            )
        finally:
            close()
        if report is None:
            raise ExportError(f"{condition} trial {trial} produced no report")
        _strip_run_authority(trial_dir / "run")
        evidence = _capture_post_evidence(
            oracle,
            fixture,
            before_counts=before_counts,
            before_non_target_loan_sha256=before_non_target,
            arm="compiled",
        )
    finally:
        reader.close()
    verdict = classify_trial(
        actor_reported_success=report.success,
        halted=not report.success,
        rest_records=evidence.rest_records,
        db_records=evidence.db_records if evidence.readable else None,
        unexpected_db_deltas=evidence.delta_violations,
    )
    audit = {
        "schema_version": "openadapt.frappe-public-oracle/v1",
        "condition": condition,
        "trial": trial,
        "baseline_snapshot_sha256": baseline_hash,
        "execution_outcome": report.execution_outcome,
        "execution_profile": report.execution_profile,
        "model_calls": report.model_calls,
        "heal_count": report.heal_count,
        "runtime_ms": report.total_ms,
        "rest_record_count": len(evidence.rest_records or []),
        "sql_record_count": len(evidence.db_records or []),
        "rest_records_sha256": (
            hashlib.sha256(
                json.dumps(
                    evidence.rest_records,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if evidence.rest_records is not None
            else None
        ),
        "sql_records_sha256": (
            hashlib.sha256(
                json.dumps(
                    evidence.db_records,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if evidence.db_records is not None
            else None
        ),
        "non_target_loan_sha256_before": evidence.before_non_target_loan_sha256,
        "non_target_loan_sha256_after": evidence.after_non_target_loan_sha256,
        "nonzero_table_deltas": {
            name: value
            for name, value in (evidence.all_table_deltas or {}).items()
            if value
        },
        "delta_violations": evidence.delta_violations,
        "oracle_errors": evidence.errors,
        "oracle_success": verdict.success,
        "silent_incorrect_success": verdict.silent_incorrect_success,
        "over_halt": verdict.over_halt,
        "entry_origin": "local pinned synthetic Frappe fixture",
    }
    _write_json(trial_dir / "oracle.json", audit)
    if (
        report.execution_outcome != "VERIFIED"
        or report.execution_profile != "standard"
        or report.model_calls != 0
        or report.heal_count != 0
        or not verdict.success
        or not evidence.readable
        or evidence.delta_violations
        or evidence.errors
        or audit["nonzero_table_deltas"] != {"tabLoan Application": 1}
        or evidence.before_non_target_loan_sha256
        != evidence.after_non_target_loan_sha256
    ):
        raise ExportError(f"{condition} trial {trial} failed its exact oracle")
    if presentation is not None:
        _write_presentation_clip(
            capture=presentation,
            pack_id=pack_id,
            clip_id="frappe-replay",
            presentation_dir=presentation_dir,
        )
    return report, audit


def _transcode_recording(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        check=True,
    )


def _poster(media: Path, destination: Path, *, seek: str = "0") -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            seek,
            "-i",
            str(media),
            "-frames:v",
            "1",
            str(destination),
        ],
        check=True,
    )


def _presentation_sidecars(replay_dir: Path, final_report: RunReport) -> None:
    media = replay_dir / "frappe-replay.mp4"
    timeline_path = replay_dir / "frappe-replay.control-overlay.v2.json"
    pts_path = replay_dir / "frappe-replay.frame-pts-us.json"
    poster_path = replay_dir / "frappe-replay.poster.png"
    _poster(media, poster_path)
    timeline = json.loads(timeline_path.read_text())
    pts = json.loads(pts_path.read_text())
    presentation_times = pts.get("presentation_times_us")
    if (
        pts.get("schema_version") != PRESENTATION_PTS_SCHEMA_VERSION
        or pts.get("media_sha256") != _sha256(media)
        or not isinstance(presentation_times, list)
        or timeline.get("media_sha256") != _sha256(media)
    ):
        raise ExportError("runtime presentation timeline is not media-bound")
    with Image.open(poster_path) as poster:
        width, height = poster.size
    _write_json(
        replay_dir / "frappe-replay.media-binding.json",
        {
            "schema_version": BINDING_SCHEMA,
            "clip_id": "frappe-replay",
            "media_file": media.name,
            "media_sha256": _sha256(media),
            "timeline_file": timeline_path.name,
            "timeline_sha256": _sha256(timeline_path),
            "poster_file": poster_path.name,
            "poster_sha256": _sha256(poster_path),
            "decoded_frame_count": len(presentation_times),
            "decoded_width": width,
            "decoded_height": height,
            "presentation_times_us": presentation_times,
        },
    )
    contexts = []
    for event in timeline.get("events", []):
        frame = event["frame"]
        context: dict[str, Any] = {
            "state_id": frame["state_id"],
            "event_sequence": frame["event_sequence"],
            "surface": "browser",
            "runtime": "local",
            "minimum_effect_tier": 1,
            "model_calls": 0,
            "external_network_calls": final_report.external_network_calls,
        }
        if frame.get("phase") == "verified":
            context["effect_evidence"] = "passed"
        contexts.append(context)
    _write_json(
        replay_dir / "frappe-replay.contexts.json",
        {
            "schema_version": CONTEXT_SCHEMA,
            "data_classification": "synthetic",
            "contexts": contexts,
        },
    )


def _validate_pack(pack: Path) -> dict[str, Any]:
    manifest = json.loads((pack / "manifest.json").read_text())
    if manifest.get("schema_version") != PACK_SCHEMA:
        raise ExportError("unexpected pack schema")
    metrics = manifest.get("metrics", {})
    if metrics != {
        "fresh_trials": 6,
        "verified_trials": 6,
        "halted_trials": 0,
        "observed_silent_incorrect_success": 0,
        "model_calls": 0,
        "independent_rest_sql_parity_trials": 6,
    }:
        raise ExportError(f"unexpected aggregate metrics: {metrics}")
    replay = pack / manifest["replay"]["media"]["path"]
    timeline_path = pack / manifest["replay"]["timeline"]["path"]
    binding_path = pack / manifest["replay"]["binding"]["path"]
    contexts_path = pack / manifest["replay"]["contexts"]["path"]
    timeline = json.loads(timeline_path.read_text())
    binding = json.loads(binding_path.read_text())
    contexts = json.loads(contexts_path.read_text())
    pts = _probe_frame_presentation_times_us("ffprobe", replay)
    if (
        binding.get("media_sha256") != _sha256(replay)
        or binding.get("timeline_sha256") != _sha256(timeline_path)
        or binding.get("presentation_times_us") != pts
        or binding.get("decoded_frame_count") != len(pts)
        or timeline.get("media_frame_count") != len(pts)
        or len(contexts.get("contexts", [])) != len(timeline.get("events", []))
    ):
        raise ExportError("replay media/timeline/binding/context mismatch")
    for sequence, event in enumerate(timeline["events"]):
        frame = event["frame"]
        if (
            frame.get("event_sequence") != sequence
            or event.get("media_frame_index") != sequence
            or event.get("at_ms") * 1_000 != pts[sequence]
        ):
            raise ExportError("overlay event is not bound to its decoded frame")
        target = frame.get("target_tracking")
        if target is not None and target.get("binding") != {
            "kind": "media_frame",
            "media_sha256": _sha256(replay),
            "frame_index": sequence,
        }:
            raise ExportError("target geometry was not retained for this exact frame")
    if timeline["events"][-1]["frame"].get("phase") != "verified":
        raise ExportError("guided replay lacks a runtime VERIFIED terminal frame")
    inventory = json.loads((pack / "inventory.json").read_text())
    expected = {
        path.relative_to(pack).as_posix(): (path.stat().st_size, _sha256(path))
        for path in pack.rglob("*")
        if path.is_file() and path.name != "inventory.json"
    }
    observed = {
        item["path"]: (item["bytes"], item["sha256"])
        for item in inventory.get("files", [])
    }
    if expected != observed:
        raise ExportError("pack inventory does not bind every public artifact")
    forbidden = (
        "/private/tmp",
        "127.0.0.1",
        "openadapt-local-actor",
        "openadapt-local-oracle",
        "password",
        "checkpoint",
        "attended_capability",
        "approval.json",
    )
    for path in pack.rglob("*.json"):
        text = path.read_text().lower()
        for needle in forbidden:
            if needle.lower() in text:
                raise ExportError(f"private value leaked into {path}: {needle}")
    return manifest


def _freeze_public_pack(
    *,
    work_root: Path,
    output_root: Path,
    pack_id: str,
    provenance: dict[str, Any] | None = None,
) -> Path:
    """Create the public derivative from a complete private campaign.

    Keeping this as a separate, repeatable phase lets presentation encoding and
    public-manifest validation improve without rerunning or mutating execution
    evidence. Every input needed for the derivative is revalidated here.
    """

    destination = output_root.resolve() / pack_id
    if destination.exists():
        raise ExportError(f"immutable pack already exists: {destination}")
    recording_dir = work_root / "recording"
    raw_recording = work_root / "media/frappe-source-recording.raw.webm"
    presentation_dir = work_root / "presentation"
    qualification_path = work_root / "qualification/report.json"
    provenance_path = work_root / "campaign/source-provenance.json"
    required = [recording_dir, raw_recording, presentation_dir, qualification_path]
    if any(not path.exists() for path in required):
        raise ExportError("campaign is incomplete and cannot be frozen")
    if provenance is None:
        if not provenance_path.exists():
            raise ExportError("campaign lacks source provenance")
        provenance = json.loads(provenance_path.read_text())
    reports: list[RunReport] = []
    audits: list[dict[str, Any]] = []
    for condition in CONDITIONS:
        for trial in range(1, TRIALS_PER_CONDITION + 1):
            trial_dir = work_root / "trials" / condition / f"trial-{trial:02d}"
            reports.append(
                RunReport.model_validate_json((trial_dir / "run/report.json").read_text())
            )
            audits.append(json.loads((trial_dir / "oracle.json").read_text()))
    qualification = QualificationReport.model_validate_json(
        qualification_path.read_text()
    )
    workflow = Workflow.load(work_root / "bundle")
    project = workflow.qualification
    if (
        not qualification.passed
        or project is None
        or project.last_certification is None
        or not project.last_certification.passed
        or any(report.execution_outcome != "VERIFIED" for report in reports)
        or any(report.execution_profile != "standard" for report in reports)
        or any(report.model_calls or report.heal_count for report in reports)
        or any(not audit.get("oracle_success") for audit in audits)
    ):
        raise ExportError("campaign does not satisfy the frozen v1 contract")

    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{pack_id}.", dir=output_root))
    try:
        recording_out = staging / "recording"
        replay_out = staging / "replay"
        recording_out.mkdir()
        replay_out.mkdir()
        recording_mp4 = recording_out / "frappe-source-recording.unbound.mp4"
        _transcode_recording(raw_recording, recording_mp4)
        recording_poster = recording_out / "frappe-source-recording.poster.png"
        _poster(recording_mp4, recording_poster, seek="5")
        for path in presentation_dir.glob("frappe-replay.*"):
            shutil.copy2(path, replay_out / path.name)
        _presentation_sidecars(replay_out, reports[0])

        lock = json.loads(
            (REPO_ROOT / "benchmark/frappe_lending/environment.lock.json").read_text()
        )
        trials = []
        for report, audit in zip(reports, audits, strict=True):
            trials.append(
                {
                    "condition": audit["condition"],
                    "trial": audit["trial"],
                    "execution_outcome": report.execution_outcome,
                    "execution_profile": report.execution_profile,
                    "model_calls": report.model_calls,
                    "heal_count": report.heal_count,
                    "runtime_ms": report.total_ms,
                    "rest_record_count": audit["rest_record_count"],
                    "sql_record_count": audit["sql_record_count"],
                    "non_target_state_unchanged": (
                        audit["non_target_loan_sha256_before"]
                        == audit["non_target_loan_sha256_after"]
                    ),
                    "report_sha256": _sha256(
                        work_root
                        / "trials"
                        / audit["condition"]
                        / f"trial-{audit['trial']:02d}"
                        / "run/report.json"
                    ),
                }
            )
        replay_media = replay_out / "frappe-replay.mp4"
        replay_poster = replay_out / "frappe-replay.poster.png"
        replay_timeline = replay_out / "frappe-replay.control-overlay.v2.json"
        replay_binding = replay_out / "frappe-replay.media-binding.json"
        replay_contexts = replay_out / "frappe-replay.contexts.json"
        replay_pts = replay_out / "frappe-replay.frame-pts-us.json"
        manifest = {
            "schema_version": PACK_SCHEMA,
            "pack_id": pack_id,
            "application": {
                "name": "Frappe Lending",
                "version": lock["upstreams"]["lending"]["ref"],
                "frappe_version": lock["upstreams"]["frappe"]["ref"],
                "erpnext_version": lock["upstreams"]["erpnext"]["ref"],
                "source_commits": {
                    name: item["commit"] for name, item in lock["upstreams"].items()
                },
                "environment": "local pinned synthetic reference fixture",
                "surface": "browser",
            },
            "phase": "qualified_compiled_replay",
            "data_classification": "synthetic",
            "recording": {
                "relationship": "unbound source recording for the compiled workflow",
                "overlay_timeline": None,
                "outcome_claim": None,
                "bundle_source_recording_sha256": _tree_manifest_sha256(recording_dir),
                "media": _asset(recording_mp4, staging),
                "poster": _asset(recording_poster, staging),
            },
            "replay": {
                "relationship": "exact runtime-observation presentation derivative",
                "execution_profile": "standard",
                "outcome": "VERIFIED",
                "evidence_class": "reference_qualification",
                "minimum_effect_tier": 1,
                "effect_verification": "authenticated read-only REST plus direct SQL and non-target delta audit",
                "media": _asset(replay_media, staging),
                "poster": _asset(replay_poster, staging),
                "timeline": _asset(replay_timeline, staging),
                "binding": _asset(replay_binding, staging),
                "contexts": _asset(replay_contexts, staging),
                "frame_inventory": _asset(replay_pts, staging),
            },
            "metrics": {
                "fresh_trials": len(trials),
                "verified_trials": sum(
                    item["execution_outcome"] == "VERIFIED" for item in trials
                ),
                "halted_trials": 0,
                "observed_silent_incorrect_success": sum(
                    audit["silent_incorrect_success"] for audit in audits
                ),
                "model_calls": sum(item["model_calls"] for item in trials),
                "independent_rest_sql_parity_trials": sum(
                    bool(audit["oracle_success"])
                    and audit["rest_record_count"] == audit["sql_record_count"] == 1
                    for audit in audits
                ),
            },
            "qualification": {
                "passed": qualification.passed,
                "environment_digest": project.environment.environment_digest,
                "action_count": qualification.action_count,
                "consequential_action_count": qualification.consequential_action_count,
                "identity_covered_action_count": qualification.identity_covered_action_count,
                "effect_covered_action_count": qualification.effect_covered_action_count,
                "minimum_effect_tier": int(qualification.minimum_effect_tier),
                "required_conditions": list(CONDITIONS),
                "trials_per_condition": TRIALS_PER_CONDITION,
                "fault_cases": [
                    "ambiguity",
                    "wrong_identity",
                    "stale_identity",
                    "weak_effect",
                    "missing_effect",
                ],
                "report_sha256": _sha256(qualification_path),
            },
            "trials": trials,
            "methodology_links": [
                "https://github.com/OpenAdaptAI/openadapt-flow/tree/main/benchmark/frappe_lending",
                "https://docs.openadapt.ai/concepts/effect-verification/",
                "https://docs.openadapt.ai/concepts/execution-profiles/",
            ],
            "provenance": {
                **provenance,
                "fixture": "pinned Frappe/Lending stack with synthetic data only",
                "execution_boundary": "local browser plus independent authenticated REST and direct SQL readback",
                "presentation_note": (
                    "Overlay is post-composed from exact runtime-retained "
                    "observations; runtime evidence media remains unmodified."
                ),
                "fixture_distribution": (
                    "repository-only; no upstream copyleft fixture files are "
                    "included in this pack or package artifacts"
                ),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generator_platform": platform.platform(),
        }
        _write_json(staging / "manifest.json", manifest)
        _write_json(
            staging / "inventory.json",
            {
                "schema_version": INVENTORY_SCHEMA,
                "files": [
                    _asset(path, staging)
                    for path in sorted(staging.rglob("*"))
                    if path.is_file() and path.name != "inventory.json"
                ],
            },
        )
        _validate_pack(staging)
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def export_pack(
    *,
    fixture_state: Path,
    work_root: Path,
    output_root: Path,
    pack_id: str,
    allow_dirty: bool,
) -> Path:
    destination = output_root.resolve() / pack_id
    if destination.exists():
        raise ExportError(f"immutable pack already exists: {destination}")
    if work_root.exists():
        raise ExportError(f"campaign work path already exists: {work_root}")
    provenance = _source_provenance(allow_dirty=allow_dirty)
    fixture = FrappeFixture(fixture_state)
    fixture.up()
    work_root.mkdir(parents=True, mode=0o700)
    work_root.chmod(0o700)
    _write_json(work_root / "campaign/source-provenance.json", provenance)
    recording_dir = work_root / "recording"
    bundle_dir = work_root / "bundle"
    raw_recording = work_root / "media/frappe-source-recording.raw.webm"
    raw_recording.parent.mkdir(parents=True)
    presentation_dir = work_root / "presentation"
    record(
        fixture,
        recording_dir,
        headed=False,
        raw_media_path=raw_recording,
        presentation_pack_id=pack_id,
    )
    compile_recording(recording_dir, bundle_dir)
    workflow = Workflow.load(bundle_dir)
    private_key, environment_digest = _configure_qualification(
        workflow,
        fixture=fixture,
    )
    save_qualified_workflow(workflow, bundle_dir)
    workflow = Workflow.load(bundle_dir)

    reports: list[RunReport] = []
    audits: list[dict[str, Any]] = []
    case_results: list[QualificationCaseResult] = []
    for condition in CONDITIONS:
        trial_dirs: list[Path] = []
        for trial in range(1, TRIALS_PER_CONDITION + 1):
            trial_dir = work_root / "trials" / condition / f"trial-{trial:02d}"
            trial_dirs.append(trial_dir)
            report, audit = _run_trial(
                fixture=fixture,
                workflow=workflow,
                bundle_dir=bundle_dir,
                condition=condition,
                trial=trial,
                trial_dir=trial_dir,
                presentation_dir=presentation_dir,
                pack_id=pack_id,
            )
            reports.append(report)
            audits.append(audit)
        case_results.append(
            _case_result(
                root=work_root,
                workflow=workflow,
                condition=condition,
                trial_dirs=trial_dirs,
                private_key=private_key,
            )
        )

    for case_id in (
        "fault-ambiguity",
        "fault-wrong-identity",
        "fault-stale-identity",
    ):
        fault_dir = work_root / "faults" / case_id
        evidence_paths = _run_ui_fault_case(
            fixture=fixture,
            workflow=workflow,
            bundle_dir=bundle_dir,
            case_id=case_id,
            fault_dir=fault_dir,
        )
        case_results.append(
            _signed_case_result(
                root=work_root,
                workflow=workflow,
                case_id=case_id,
                observed_outcome=QualificationOutcome.HALTED,
                evidence_paths=evidence_paths,
                detail_code=f"{case_id}.runtime-refusal",
                private_key=private_key,
            )
        )

    reader = _login_session(
        fixture.base_url,
        "openadapt.oracle@example.invalid",
        "openadapt-local-oracle",
    )
    try:
        oracle = FrappeLoanApplicationOracle(fixture.base_url, reader)
        for case_id in ("fault-weak-effect", "fault-missing-effect"):
            evidence_paths = _run_gate_fault_case(
                workflow=workflow,
                bundle_dir=bundle_dir,
                oracle=oracle,
                case_id=case_id,
                fault_dir=work_root / "faults" / case_id,
            )
            case_results.append(
                _signed_case_result(
                    root=work_root,
                    workflow=workflow,
                    case_id=case_id,
                    observed_outcome=QualificationOutcome.HALTED,
                    evidence_paths=evidence_paths,
                    detail_code=f"{case_id}.admission-refusal",
                    private_key=private_key,
                )
            )
    finally:
        reader.close()

    record_case_results(workflow, case_results, evidence_root=work_root)
    qualification = certify_project(
        workflow,
        policy=load_policy("clinical-write"),
        evidence_root=work_root,
    )
    if not qualification.passed:
        raise ExportError(qualification.render())
    save_qualified_workflow(workflow, bundle_dir)
    _write_json(
        work_root / "qualification/report.json", qualification.model_dump(mode="json")
    )
    _write_json(
        work_root / "qualification/project.json",
        workflow.qualification.model_dump(mode="json")
        if workflow.qualification
        else {},
    )

    return _freeze_public_pack(
        work_root=work_root,
        output_root=output_root,
        pack_id=pack_id,
        provenance=provenance,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        type=Path,
        default=REPO_ROOT / "benchmark/frappe_lending/state",
    )
    parser.add_argument("--work", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--pack-id", default=PACK_ID)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--validate", type=Path)
    parser.add_argument(
        "--freeze-existing",
        action="store_true",
        help="freeze a completed campaign without re-running execution",
    )
    args = parser.parse_args(argv)
    if args.validate is not None:
        _validate_pack(args.validate.resolve())
        print(args.validate.resolve())
        return 0
    if args.work is None or args.out is None:
        parser.error("--work and --out are required unless --validate is used")
    if args.freeze_existing:
        provenance_path = args.work.resolve() / "campaign/source-provenance.json"
        provenance = (
            json.loads(provenance_path.read_text())
            if provenance_path.exists()
            else _source_provenance(allow_dirty=args.allow_dirty)
        )
        destination = _freeze_public_pack(
            work_root=args.work.resolve(),
            output_root=args.out.resolve(),
            pack_id=args.pack_id,
            provenance=provenance,
        )
    else:
        destination = export_pack(
            fixture_state=args.state.resolve(),
            work_root=args.work.resolve(),
            output_root=args.out.resolve(),
            pack_id=args.pack_id,
            allow_dirty=args.allow_dirty,
        )
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

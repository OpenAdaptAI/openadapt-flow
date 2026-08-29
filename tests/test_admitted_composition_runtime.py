"""Runtime of a ProcessContract: admissions first, then Execute."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from openadapt_flow.admitted_composition import (
    ProcessContract,
    author_process_contract,
)
from openadapt_flow.compiler.compose_authoring import author_composition
from openadapt_flow.composition import HandoffBinding
from openadapt_flow.qualification_admission import QualificationAdmissionEnvelope
from openadapt_flow.runtime.admitted_composition import (
    AdmittedCapability,
    execute,
    execute_process_contract,
)
from openadapt_flow.runtime.composition import ChildRunResult
from tests.test_admitted_composition_authoring import (
    INTAKE_ADMISSION,
    POSTING_ADMISSION,
    _reader,
    _save,
    _two_admitted,
    _writer,
)
from tests.test_qualification_admission import NOW, _trust


def _author_two(tmp_path: Path, **kwargs) -> tuple[ProcessContract, Path]:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    out = tmp_path / "process"
    contract = author_process_contract(
        [
            ("intake", intake_env, intake),
            ("posting", posting_env, posting),
        ],
        handoffs=kwargs.pop(
            "handoffs",
            [
                HandoffBinding(
                    from_child="intake",
                    source="patient_id",
                    to_child="posting",
                    target="patient_id",
                )
            ],
        ),
        name="two-child",
        out=out,
        **kwargs,
    )
    return contract, out


def test_missing_handoff_halts(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)
    seen: list[str] = []

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        seen.append(child)
        assert isinstance(capability, AdmittedCapability)
        assert isinstance(admission, QualificationAdmissionEnvelope)
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={},
                effect_facts={},
                success=True,
            )
        raise AssertionError("posting must not start without handoff evidence")

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.outcome == "HALTED"
    assert report.halted_at == "posting"
    assert "missing handoff evidence" in report.reason
    assert seen == ["intake"]


def test_expired_admission_halts_before_execute(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        raise AssertionError("Execute must not run on an expired admission")

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW + timedelta(days=31),
    )
    assert report.outcome == "HALTED"
    assert report.halted_at == "intake"
    assert "expired" in report.reason
    assert report.children == []


def test_revoked_admission_halts_before_execute(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        raise AssertionError("Execute must not run on a revoked admission")

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        revoked_admission_ids={INTAKE_ADMISSION},
        now=NOW,
    )
    assert report.outcome == "HALTED"
    assert report.halted_at == "intake"
    assert "revoked" in report.reason
    assert report.children == []


def test_predecessor_not_verified_halts(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)
    seen: list[str] = []

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        seen.append(child)
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="HALTED",
                bound_params={"patient_id": "alice"},
                effect_facts={"patient_id": "alice"},
                success=False,
            )
        raise AssertionError("posting must not start after an unverified predecessor")

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.outcome == "HALTED"
    assert report.halted_at == "intake"
    assert seen == ["intake"]
    assert report.children[0].effect_facts == {}


def test_allowed_halt_class_continues_without_minting_facts(tmp_path: Path) -> None:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    out = tmp_path / "process"
    contract = author_process_contract(
        [
            ("intake", intake_env, intake),
            ("posting", posting_env, posting),
        ],
        allow_halt={"intake": ["HALTED"]},
        out=out,
    )
    seen: list[str] = []

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        seen.append(child)
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="HALTED",
                bound_params={"patient_id": "alice"},
                effect_facts={"patient_id": "alice"},
                success=False,
            )
        return ChildRunResult(
            child=child,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            success=True,
        )

    report = execute_process_contract(
        contract,
        parent_dir=out,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
    )
    assert seen == ["intake", "posting"]
    assert report.outcome != "VERIFIED"
    assert report.children[0].effect_facts == {}
    assert [item.child for item in report.children] == ["intake", "posting"]


def test_scripted_verified_handoff_copies_bound_param(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)
    received: dict[str, str] = {}

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        assert capability.admission_id in {INTAKE_ADMISSION, POSTING_ADMISSION}
        assert admission.payload.admission_id == capability.admission_id
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={"patient_id": "alice", "note": "triage"},
                effect_facts={"patient_id": "alice"},
                success=True,
            )
        received.update(inputs)
        return ChildRunResult(
            child=child,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            success=True,
        )

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
        inputs={"note": "triage"},
    )
    assert report.outcome == "VERIFIED"
    assert report.model_calls == 0
    assert received["patient_id"] == "alice"
    assert received["note"] == "triage"


def test_model_call_on_child_forbids_parent_verified(tmp_path: Path) -> None:
    contract, parent = _author_two(tmp_path)

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={"patient_id": "alice"},
                effect_facts={"patient_id": "alice"},
                model_calls=1,
                success=True,
            )
        return ChildRunResult(
            child=child,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            success=True,
        )

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.outcome == "COMPLETED_UNVERIFIED"
    assert report.model_calls == 1
    assert report.success is False


def test_receipt_names_admissions_redacts_handoffs_omits_window_titles(
    tmp_path: Path,
) -> None:
    contract, parent = _author_two(tmp_path)

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        if child == "intake":
            return ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={"patient_id": "alice", "window_title": "Chart"},
                effect_facts={"patient_id": "alice"},
                success=True,
            )
        return ChildRunResult(
            child=child,
            outcome="VERIFIED",
            bound_params=dict(inputs),
            effect_facts={},
            success=True,
        )

    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run",
        child_run=child_run,
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.outcome == "VERIFIED"
    path = tmp_path / "run" / "process-report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    assert INTAKE_ADMISSION in text
    assert POSTING_ADMISSION in text
    assert payload["children"][0]["admission_id"] == INTAKE_ADMISSION
    assert payload["children"][1]["admission_id"] == POSTING_ADMISSION
    assert payload["children"][0]["bundle_content_digest"]
    assert payload["children"][1]["bundle_content_digest"]
    assert "alice" not in text
    assert "<bound>" in text
    assert "window_title" not in text
    assert "Chart" not in text
    assert "window titles" not in text.lower()


def test_execute_refuses_none_admission(tmp_path: Path) -> None:
    intake = _save(tmp_path, _writer(), "intake")
    workflow = _writer()

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        raise AssertionError("must not run")

    with pytest.raises(Exception, match="real AdmittedCapability"):
        execute(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            {},
            workflow=workflow,
            bundle_dir=intake,
            run_dir=tmp_path / "run",
            child="intake",
            child_run=child_run,
        )


def test_pointing_runtime_at_compose_recordings_fails_admission(
    tmp_path: Path,
) -> None:
    intake = _save(tmp_path, _writer(), "intake")
    posting = _save(tmp_path, _reader(), "posting")
    composed = tmp_path / "composed"
    author_composition(
        [("intake", intake), ("posting", posting)],
        out=composed,
    )

    def child_run(
        capability, admission, inputs, *, workflow, bundle_dir, run_dir, child
    ):
        raise AssertionError("compose recordings must not reach Execute")

    with pytest.raises(Exception, match="process-contract"):
        ProcessContract.load(composed)
    # Green path: two envelopes still execute.
    admitted = tmp_path / "admitted"
    admitted.mkdir()
    contract, parent = _author_two(admitted)
    report = execute_process_contract(
        contract,
        parent_dir=parent,
        run_dir=tmp_path / "run-ok",
        child_run=lambda capability, admission, inputs, *, workflow, bundle_dir, run_dir, child: (
            ChildRunResult(
                child=child,
                outcome="VERIFIED",
                bound_params={"patient_id": "alice"}
                if child == "intake"
                else dict(inputs),
                effect_facts={"patient_id": "alice"} if child == "intake" else {},
                success=True,
            )
        ),
        trusted_signers=_trust(),
        now=NOW,
    )
    assert report.outcome == "VERIFIED"

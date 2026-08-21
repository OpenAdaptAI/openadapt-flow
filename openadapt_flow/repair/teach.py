"""Turn a governed Teach result into a repair-lifecycle candidate.

The learning loop can induce and validate a corrected program, but that result
is not deployment authority.  This module preserves the prior bundle's safety
and qualification contracts, registers the proposed bundle as a detached
repair candidate, and adds it to :class:`RepairStore` without advancing it.

No function in this module reviews, approves, stages, canaries, or activates a
candidate.  Consequential changes therefore remain inert until the existing
human-governed repair lifecycle completes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.ir import Workflow
from openadapt_flow.policy import evaluate_policy, load_policy
from openadapt_flow.qualification import evaluate_qualification
from openadapt_flow.repair.lifecycle import RepairStore
from openadapt_flow.repair.registration import (
    load_detached_candidate,
    register_bundle_candidate,
)
from openadapt_flow.traversal import iter_workflow_steps

_MAX_FIX_FILES = 10_000
_MAX_FIX_BYTES = 2 * 1024 * 1024 * 1024


class TeachRepairError(ValueError):
    """The correction could not become a governed repair candidate."""


class TeachRepairResult(BaseModel):
    """Closed result safe for a local console response and metric projection."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["candidate", "banked", "refused"]
    attempt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_id: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{8,64}$")
    candidate_state: Optional[Literal["candidate", "rejected"]] = None
    candidate_record_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    policy_passed: Optional[bool] = None
    qualification_passed: Optional[bool] = None
    consequential: bool = False
    requires_human_approval: bool = True


def correction_digest(path: Path | str) -> str:
    """Digest one local correction without following a symlink or exporting it."""
    path = Path(path)
    if path.is_symlink() or not path.exists():
        raise TeachRepairError("the selected correction is unavailable")
    files = (
        [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    )
    if len(files) > _MAX_FIX_FILES:
        raise TeachRepairError("the selected correction has too many files")
    digest = hashlib.sha256()
    total = 0
    root = path if path.is_dir() else path.parent
    for item in files:
        if item.is_symlink():
            raise TeachRepairError("the selected correction contains a symlink")
        try:
            item.resolve(strict=True).relative_to(root.resolve(strict=True))
            size = item.stat().st_size
        except (OSError, ValueError) as exc:
            raise TeachRepairError(
                "the selected correction could not be verified"
            ) from exc
        total += size
        if total > _MAX_FIX_BYTES:
            raise TeachRepairError("the selected correction is too large")
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with item.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _copy_prior_assets(prior_bundle: Path, staging: Path) -> None:
    """Carry qualification evidence and templates without an old repair record."""

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {"repair"} & set(names)

    shutil.copytree(prior_bundle, staging, ignore=ignore)


def _preserve_contract(prior_bundle: Path, proposed_bundle: Path) -> Workflow:
    """Restore the prior non-program contract around the learned program.

    ``learning.teach`` materializes the learned graph as a new Workflow.  The
    repair must also retain the prior compatibility steps, identity/effect
    declarations, execution boundary, PHI flags, interstitials, parameters,
    and qualification project.  The learned program and subflows remain the
    proposed change.  The new bundle remains uncertified.
    """
    prior = Workflow.load(prior_bundle)
    proposed = Workflow.load(proposed_bundle)
    proposed = proposed.model_copy(
        deep=True,
        update={
            "recording_id": prior.recording_id,
            "contains_phi": prior.contains_phi,
            "phi_scrubbed": prior.phi_scrubbed,
            "viewport": prior.viewport,
            "backend_hints": prior.backend_hints,
            "surface": prior.surface,
            "execution_mode": prior.execution_mode,
            "params": dict(prior.params),
            "param_specs": {
                key: value.model_copy(deep=True)
                for key, value in prior.param_specs.items()
            },
            "secret_params": list(prior.secret_params),
            "steps": [step.model_copy(deep=True) for step in prior.steps],
            "interstitials": [
                item.model_copy(deep=True) for item in prior.interstitials
            ],
            "data_sources": {
                key: value.model_copy(deep=True)
                for key, value in prior.data_sources.items()
            },
            "qualification": (
                prior.qualification.model_copy(deep=True)
                if prior.qualification is not None
                else None
            ),
        },
    )
    proposed.save(proposed_bundle)
    return proposed


def _is_consequential(workflow: Workflow) -> bool:
    for step in iter_workflow_steps(workflow):
        if step.risk == "irreversible" or getattr(step, "consequential", False):
            return True
        if step.effects:
            return True
        project = workflow.qualification
        if project is not None:
            classification = project.action_classifications.get(step.id)
            if classification is not None and classification.classification.value in {
                "state_changing",
                "consequential",
                "irreversible",
            }:
                return True
    return False


def _evaluate_candidate(
    candidate_bundle: Path,
    *,
    policy_name: str,
) -> tuple[bool, Optional[bool], bool]:
    workflow = Workflow.load(candidate_bundle)
    try:
        policy = load_policy(policy_name)
    except (FileNotFoundError, ValueError) as exc:
        raise TeachRepairError(
            "the selected qualification policy is unavailable"
        ) from exc
    policy_passed = evaluate_policy(workflow, policy).passed
    qualification_passed: Optional[bool] = None
    if workflow.qualification is not None:
        qualification_passed = evaluate_qualification(
            workflow,
            policy=policy,
            evidence_root=candidate_bundle,
        ).passed
    return policy_passed, qualification_passed, _is_consequential(workflow)


def create_teach_repair_candidate(
    run_dir: Path | str,
    correction: Path | str,
    prior_bundle: Path | str,
    *,
    candidates_root: Path | str,
    repair_store: Path | str,
    policy_name: str = "clinical-write",
) -> TeachRepairResult:
    """Induce one correction and stop at an auditable repair candidate.

    The correction and all generated bundle bytes stay local.  A successful
    learning result is registered in ``RepairStore`` in state ``candidate`` (or
    ``rejected`` when contract invariants fail).  This function performs no
    lifecycle transition and never writes ``ACTIVE.json``.
    """
    from openadapt_flow.learning.teach import TeachError, teach

    run = Path(run_dir)
    fix = Path(correction)
    prior = Path(prior_bundle)
    root = Path(candidates_root)
    store = RepairStore(repair_store)
    if any(path.is_symlink() for path in (run, prior, root)):
        raise TeachRepairError("the Teach path cannot use a symlink root")
    if not run.is_dir() or not prior.is_dir():
        raise TeachRepairError("the halted run or prior bundle is unavailable")
    base = Workflow.load(prior)
    if base.encrypted:
        raise TeachRepairError(
            "an encrypted bundle requires a sealed candidate builder; Teach refused"
        )

    fix_digest = correction_digest(fix)
    prior_digest = base.manifest.content_digest if base.manifest else ""
    if not prior_digest:
        raise TeachRepairError("the prior bundle has no sealed content digest")
    attempt_digest = hashlib.sha256(
        f"{prior_digest}:{fix_digest}".encode("ascii")
    ).hexdigest()
    final = root / attempt_digest[:24]
    if final.is_dir():
        candidate = load_detached_candidate(final)
        candidate = store.add_candidate(candidate)
        policy_passed, qualification_passed, consequential = _evaluate_candidate(
            final, policy_name=policy_name
        )
        return TeachRepairResult(
            outcome="refused" if candidate.state == "rejected" else "candidate",
            attempt_digest=attempt_digest,
            candidate_id=candidate.candidate_id,
            candidate_state=candidate.state,
            candidate_record_sha256=candidate.record_sha256(),
            policy_passed=policy_passed,
            qualification_passed=qualification_passed,
            consequential=consequential,
        )
    if final.exists():
        raise TeachRepairError("the repair candidate destination is not a directory")

    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".{attempt_digest[:24]}.pending-{os.getpid()}"
    if staging.exists():
        raise TeachRepairError("the repair candidate is already being prepared")
    lineage = root / ".lineage" / prior_digest
    try:
        _copy_prior_assets(prior, staging)
        try:
            learned = teach(
                run,
                fix,
                staging,
                bundle=prior,
                library_dir=lineage,
            )
        except TeachError as exc:
            raise TeachRepairError(str(exc)) from exc
        if not learned.promoted:
            return TeachRepairResult(
                outcome=(
                    "banked" if learned.outcome.action == "no_change" else "refused"
                ),
                attempt_digest=attempt_digest,
            )

        _preserve_contract(prior, staging)
        os.replace(staging, final)
        register_bundle_candidate(
            prior,
            final,
            source="teach",
            evidence_run_dir=run,
        )
        candidate = store.add_candidate(load_detached_candidate(final))
        policy_passed, qualification_passed, consequential = _evaluate_candidate(
            final, policy_name=policy_name
        )
        return TeachRepairResult(
            outcome="refused" if candidate.state == "rejected" else "candidate",
            attempt_digest=attempt_digest,
            candidate_id=candidate.candidate_id,
            candidate_state=candidate.state,
            candidate_record_sha256=candidate.record_sha256(),
            policy_passed=policy_passed,
            qualification_passed=qualification_passed,
            consequential=consequential,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)


__all__ = [
    "TeachRepairError",
    "TeachRepairResult",
    "correction_digest",
    "create_teach_repair_candidate",
]

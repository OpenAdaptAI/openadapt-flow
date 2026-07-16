"""Run-bound authorization for governed deployment execution.

The permissive ``replay`` path does not use this object.  ``run`` creates one
only after every admission gate passes, then hands it to the shared replayer.
Binding the decision to the sealed bundle and exact effect contracts prevents
an approval intended for one workflow from becoming a reusable bypass.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.ir import Step, Workflow
from openadapt_flow.traversal import iter_workflow_steps


class UnverifiedWriteApproval(BaseModel):
    """Approval for one GUI step whose effects lack an independent verifier."""

    model_config = ConfigDict(frozen=True)

    step_id: str
    effect_contract_hashes: tuple[str, ...] = Field(min_length=1)


class GovernedRunAuthorization(BaseModel):
    """Ephemeral capability carrying admission decisions into replay.

    ``approval_source`` is deliberately descriptive, not an authentication
    claim.  The local CLI can prove that its explicit flag was supplied, but it
    cannot identify a human.  Hosted callers can replace the source with their
    authenticated approval reference when they construct the capability.
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    bundle_content_digest: str = Field(pattern="^[a-f0-9]{64}$")
    required_identity_step_ids: tuple[str, ...] = Field(default_factory=tuple)
    unverified_write_approvals: tuple[UnverifiedWriteApproval, ...] = Field(
        default_factory=tuple
    )
    approval_source: str = "local-cli-explicit-flag"

    def validate_workflow(self, workflow: Workflow) -> str | None:
        """Return a refusal reason when this capability does not fit ``workflow``."""
        actual_digest = (
            workflow.manifest.content_digest if workflow.manifest is not None else None
        )
        if actual_digest != self.bundle_content_digest:
            return (
                "governed run authorization is bound to bundle digest "
                f"{self.bundle_content_digest[:16]}..., but the loaded bundle is "
                f"{(actual_digest or 'unsealed')[:16]}..."
            )

        steps = {step.id: step for step in iter_workflow_steps(workflow)}
        missing_identity = sorted(
            set(self.required_identity_step_ids).difference(steps)
        )
        if missing_identity:
            return (
                "governed run authorization requires unknown identity step(s): "
                + ", ".join(missing_identity)
            )

        seen: set[str] = set()
        for approval in self.unverified_write_approvals:
            if approval.step_id in seen:
                return (
                    "governed run authorization contains duplicate write approval "
                    f"for step {approval.step_id!r}"
                )
            seen.add(approval.step_id)
            step = steps.get(approval.step_id)
            if step is None:
                return (
                    "governed run authorization approves unknown write step "
                    f"{approval.step_id!r}"
                )
            expected = sorted(effect.contract_hash() for effect in step.effects)
            if sorted(approval.effect_contract_hashes) != expected:
                return (
                    "governed run authorization effect contract mismatch for step "
                    f"{approval.step_id!r}"
                )
        return None

    def requires_verified_identity(self, step_id: str) -> bool:
        return step_id in self.required_identity_step_ids

    def approves_unverified_write(self, step: Step) -> bool:
        """Whether this capability exactly approves ``step``'s current effects."""
        expected = sorted(effect.contract_hash() for effect in step.effects)
        return any(
            approval.step_id == step.id
            and sorted(approval.effect_contract_hashes) == expected
            for approval in self.unverified_write_approvals
        )

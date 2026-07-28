"""Resume a durably-paused run from its last verified checkpoint (RFC §5).

:func:`resume` is the Tier-3 resume entrypoint, and P0-5 makes it an
AUTHENTICATED APPROVAL workflow. Given a ``run_dir`` that holds a
:class:`~.checkpoint.PendingEscalation` (and the checkpoints written during the
original run), it:

1. ENFORCES an authenticated approval (RFC §5, P0-5): an
   :class:`~.approval.ApprovalRecord` (approver / timestamp / resolution /
   bundle version) must accompany the resume, the pause must not have expired
   (stale-pause window), and the approval must match the current bundle version.
   A caller without a valid approval record CANNOT resume;
2. reads the :class:`~.checkpoint.RunManifest` to recover the bundle and the
   run's parameter bindings (so the caller need only supply a live ``Replayer``);
3. REVALIDATES the live app is still in the checkpoint's expected state (and that
   the already-confirmed effects still hold) before continuing;
4. RESTORES the run from its last verified checkpoint and re-drives it from
   there onward -- NEVER from step 0.

For a Phase-2 PROGRAM run the resume point is not a step index but the whole
INTERPRETER STATE (:class:`~.program_checkpoint.ProgramCheckpoint`): the frame
stack, loop cursors, bound params, and completed effect keys are RESTORED so an
already-confirmed consequential write is never re-performed and a mid-loop pause
finishes the in-progress row and runs the remaining rows. For a linear run the
resume point is the last verified step index (unchanged from before).

Explicit non-goal (RFC §5): resume is DETERMINISTIC. It hands the remaining
workflow to the SAME deterministic replayer, never to a free-form agent.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.ir import ExecutionTargetKind, RunReport, Workflow
from openadapt_flow.runtime.durable.approval import (
    ApprovalRecord,
    ApprovalRequired,
    BundleMismatch,
    StateDiverged,
    enforce_resume_authorization,
)
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)
from openadapt_flow.runtime.durable.program_checkpoint import (
    ProgramCheckpoint,
    bundle_version,
)


def resume_point(run_dir: Path | str, *, key: Optional[str] = None) -> int:
    """The step index a resume would continue from for a LINEAR ``run_dir``.

    The last verified checkpoint's ``next_step_index``; 0 when nothing has
    verified yet (a run that halted on its very first step resumes from 0). Not
    meaningful for a program run (whose resume point is an interpreter state);
    use :meth:`CheckpointStore.last_program_checkpoint` there. ``key`` decrypts
    encrypted checkpoints (see :class:`CheckpointStore`).
    """
    last = CheckpointStore(run_dir, key=key).last_checkpoint()
    return last.next_step_index if last is not None else 0


def _created_no_later(checkpoint_created_at: str, pause_created_at: str) -> bool:
    """Return whether one checkpoint predates its retained pause."""

    try:
        return datetime.fromisoformat(checkpoint_created_at) <= datetime.fromisoformat(
            pause_created_at
        )
    except (TypeError, ValueError):
        return False


def _linear_resume_checkpoint(
    *,
    checkpoints: list[RunCheckpoint],
    pending: PendingEscalation,
    manifest: RunManifest,
    workflow: Workflow,
) -> Optional[RunCheckpoint]:
    """Validate the exact linear history retained by one active pause."""

    attended = (
        bool(checkpoints)
        and pending.status == "approved"
        and checkpoints[-1].step_index == pending.step_index
        and checkpoints[-1].step_id == pending.step_id
        and checkpoints[-1].actuation in {"human_attended", "human_attended_skip"}
    )
    expected_count = pending.resume_from_index + (1 if attended else 0)
    if len(checkpoints) != expected_count:
        raise StateDiverged(
            "the linear checkpoint history does not match the durable pause cursor"
        )
    for index, checkpoint in enumerate(checkpoints):
        if (
            index >= len(workflow.steps)
            or checkpoint.workflow_name != workflow.name
            or checkpoint.step_index != index
            or checkpoint.next_step_index != index + 1
            or checkpoint.step_id != workflow.steps[index].id
            or checkpoint.params != manifest.params
        ):
            raise StateDiverged(
                "the linear checkpoint history does not match the workflow"
            )
        is_attended_tail = attended and index == len(checkpoints) - 1
        if not is_attended_tail and not _created_no_later(
            checkpoint.created_at, pending.created_at
        ):
            raise StateDiverged(
                "the linear checkpoint history changed after the durable pause"
            )
    prior = (
        checkpoints[-2]
        if attended and len(checkpoints) > 1
        else (checkpoints[-1] if checkpoints and not attended else None)
    )
    if pending.resume_from_step_id != (prior.step_id if prior is not None else None):
        raise StateDiverged(
            "the linear checkpoint cursor does not match the durable pause"
        )
    return checkpoints[-1] if checkpoints else None


def resume(
    run_dir: Path | str,
    replayer: Any,
    *,
    approval: Optional[ApprovalRecord] = None,
    bundle_dir: Optional[Path | str] = None,
    params: Optional[dict[str, str]] = None,
    worklists: Optional[dict[str, list[dict[str, str]]]] = None,
    save_healed_to: Optional[Path | str] = None,
    execution_target_kind: Optional[ExecutionTargetKind] = None,
    now: Optional[datetime] = None,
    key: Optional[str] = None,
) -> RunReport:
    """Resume a durably-paused run from its last verified checkpoint.

    Args:
        run_dir: The original run directory (holds the checkpoints, the
            manifest, the pending escalation, and any approval record).
        replayer: A live :class:`~openadapt_flow.runtime.replayer.Replayer`
            (its backend/vision cannot be serialized, so the caller provides a
            fresh one bound to the recovered system). Durability is force-
            enabled on it so the resumed leg keeps checkpointing.
        approval: The authenticated authorization to resume (P0-5). When omitted,
            an ``approval.json`` written by the ``approve`` command is used. If
            neither is present (or the record carries no approver), resume is
            REFUSED with :class:`~.approval.ApprovalRequired`.
        bundle_dir: Override the bundle recorded in the manifest (rarely
            needed); defaults to the manifest's ``bundle_dir``.
        params: Override the parameter bindings recorded in the manifest;
            defaults to the manifest's ``params`` so the resume re-binds
            identically.
        worklists: Override the worklists recorded in the manifest; defaults to
            the original frozen worklists so program loops resume identically.
        save_healed_to: Override the manifest's healed-bundle path.
        execution_target_kind: Resolved backend token for the resumed leg's
            report and substrate-aware runtime attestation.
        now: Injectable clock for the stale-pause check (defaults to UTC now).
        key: At-rest passphrase for an ENCRYPTED run (its checkpoints and/or its
            bundle). Resolved from ``key`` or ``OPENADAPT_BUNDLE_KEY``. Used to
            decrypt the durable checkpoints, load an encrypted bundle, and keep
            the resumed leg sealing new checkpoints. None => plaintext.

    Returns:
        The :class:`~openadapt_flow.ir.RunReport` for the resumed leg.

    Raises:
        FileNotFoundError: when ``run_dir`` has no manifest (it was not run
            durably) and no ``bundle_dir`` override is supplied.
        ResumeRefused: (``ApprovalRequired`` / ``PauseExpired`` /
            ``BundleMismatch`` / ``StateDiverged``) when the resume is not
            authorized, the pause expired, the bundle changed, or the live app
            diverged from the checkpoint's expected state.
    """
    from openadapt_flow import crypto as _crypto

    key = _crypto.resolve_key(key)
    run_dir = Path(run_dir)
    store = CheckpointStore(run_dir, key=key)
    manifest = store.read_manifest()
    pending = store.read_pending()

    if manifest is None:
        raise FileNotFoundError(
            f"Cannot resume {run_dir}: no durable manifest was found. A "
            "continuation must use the exact retained run context."
        )
    if pending is None:
        raise ApprovalRequired(
            "the run has no active durable pause to approve and resume"
        )

    resolved_bundle = bundle_dir or manifest.bundle_dir
    resolved_bundle = Path(resolved_bundle)
    if params is not None and params != manifest.params:
        raise StateDiverged(
            "resume parameters differ from the exact retained run inputs"
        )
    if worklists is not None and worklists != manifest.worklists:
        raise StateDiverged(
            "resume worklists differ from the exact retained run inputs"
        )
    resolved_params = dict(manifest.params)
    resolved_worklists = {
        name: [dict(row) for row in rows] for name, rows in manifest.worklists.items()
    }
    resolved_healed = save_healed_to or manifest.save_healed_to

    live_bundle_version = bundle_version(resolved_bundle)

    # -- P0-5: enforce an authenticated approval before ANYTHING re-executes.
    # A pending escalation means a human was asked to authorize the resume; no
    # valid approval => refuse (never a silent proceed). A run_dir with no
    # pending escalation is not a paused run -- nothing to authorize.
    if pending.workflow_name != manifest.workflow_name or (
        not pending.program and pending.params != manifest.params
    ):
        raise StateDiverged(
            "the durable pause does not match the exact retained run manifest"
        )
    approved: ApprovalRecord = enforce_resume_authorization(
        pending,
        approval if approval is not None else store.read_approval(),
        bundle_version=live_bundle_version,
        now=now,
    )
    if pending.delivery_uncertainty is not None:
        last_linear = store.last_checkpoint()
        last_program = store.last_program_checkpoint()
        reconciled_without_retry = (
            not pending.program
            and last_linear is not None
            and last_linear.step_index == pending.step_index
            and last_linear.next_step_index > pending.step_index
            and last_linear.actuation in {"human_attended", "human_attended_skip"}
        ) or (
            pending.program
            and last_program is not None
            and last_program.verified_state_id == (pending.state_id or pending.step_id)
            and last_program.attended_transition is not None
        )
        if not reconciled_without_retry and not approved.authorize_uncertain_retry:
            raise ApprovalRequired(
                "the paused step may already have actuated; ordinary resume "
                "cannot repeat it. Reconcile and independently verify the "
                "outcome through the attended completion path, or create a "
                "fresh approval that explicitly authorizes one "
                "uncertain-delivery retry"
            )

    workflow = Workflow.load(resolved_bundle, key=key)
    if workflow.name != manifest.workflow_name:
        raise StateDiverged(
            "the durable manifest names a different workflow than the bundle"
        )
    if manifest is not None and manifest.governed_authorization is not None:
        existing = getattr(replayer, "governed_authorization", None)
        if existing is not None and existing != manifest.governed_authorization:
            raise BundleMismatch(
                "resume Replayer carries a different governed authorization "
                "than the durable run manifest"
            )
        replayer.governed_authorization = manifest.governed_authorization
        replayer.governed_continuation = True
    # Keep the resumed leg sealing new checkpoints with the same key.
    replayer.checkpoint_key = key
    program_checkpoint: Optional[ProgramCheckpoint] = store.last_program_checkpoint()

    if workflow.program is not None:
        if not pending.program:
            raise StateDiverged(
                "a program workflow cannot resume from a linear durable pause"
            )
        return _resume_program(
            store=store,
            replayer=replayer,
            workflow=workflow,
            checkpoint=program_checkpoint,
            bundle_dir=resolved_bundle,
            params=resolved_params,
            worklists=resolved_worklists,
            save_healed_to=resolved_healed,
            live_bundle_version=live_bundle_version,
            run_id=(manifest.run_id if manifest is not None else None),
            execution_target_kind=execution_target_kind,
            manifest=manifest,
            pending=pending,
        )

    if pending.program:
        raise StateDiverged(
            "a linear workflow cannot resume from a program durable pause"
        )
    # -- linear resume -------------------------------------------------------
    linear_checkpoints = store.checkpoints()
    last_linear = _linear_resume_checkpoint(
        checkpoints=linear_checkpoints,
        pending=pending,
        manifest=manifest,
        workflow=workflow,
    )
    start_index = last_linear.next_step_index if last_linear is not None else 0
    from openadapt_flow.runtime.replayer import _DURABLE_RESUME_AUTHORITY

    effective_run_id = replayer._admit_durable_resume(
        _DURABLE_RESUME_AUTHORITY,
        mode="linear",
        workflow=workflow,
        run_dir=run_dir,
        bundle_dir=resolved_bundle,
        run_id=(manifest.run_id if manifest is not None else None),
        params=resolved_params,
        worklists=resolved_worklists,
        resume_from=start_index,
        resume_program=None,
    )
    replayer.durable = True
    return replayer.run(
        workflow,
        params=resolved_params,
        worklists=resolved_worklists,
        bundle_dir=resolved_bundle,
        run_dir=run_dir,
        save_healed_to=(Path(resolved_healed) if resolved_healed else None),
        resume_from=start_index,
        run_id=effective_run_id,
        execution_target_kind=execution_target_kind,
        prior_screenshots_may_leave_box=(
            manifest.screenshots_may_leave_box if manifest is not None else False
        ),
        prior_model_calls=(manifest.model_calls if manifest is not None else 0),
        prior_external_network_calls=(
            manifest.external_network_calls if manifest is not None else "unknown"
        ),
    )


def _resume_program(
    *,
    store: CheckpointStore,
    replayer: Any,
    workflow: Workflow,
    checkpoint: Optional[ProgramCheckpoint],
    bundle_dir: Path,
    params: dict[str, str],
    worklists: dict[str, list[dict[str, str]]],
    save_healed_to: Optional[Path | str],
    live_bundle_version: str,
    run_id: Optional[str],
    execution_target_kind: Optional[ExecutionTargetKind],
    manifest: Any,
    pending: Any,
) -> RunReport:
    """Restore and continue a Phase-2 PROGRAM run from its interpreter checkpoint.

    Revalidates the bundle version and the live app state (and re-verifies the
    already-confirmed effects still hold) BEFORE re-driving, then hands the
    restored interpreter state to the replayer's program resume path. A program
    run that halted on its very FIRST state has no checkpoint (``checkpoint`` is
    None): there is nothing verified to restore, so it resumes from the top.
    """
    attended_checkpoint = (
        checkpoint is not None
        and checkpoint.attended_transition is not None
        and pending.status == "approved"
        and checkpoint.seq == pending.program_checkpoint_seq + 1
        and checkpoint.verified_state_id == (pending.state_id or pending.step_id)
    )
    if checkpoint is None:
        if (
            pending.program_checkpoint_seq != 0
            or pending.resume_from_step_id is not None
        ):
            raise StateDiverged(
                "the durable program pause lost its verified interpreter checkpoint"
            )
    else:
        if checkpoint.workflow_name != workflow.name or (
            not attended_checkpoint
            and (
                checkpoint.seq != pending.program_checkpoint_seq
                or pending.resume_from_step_id != checkpoint.verified_state_id
                or not _created_no_later(checkpoint.created_at, pending.created_at)
            )
        ):
            raise StateDiverged(
                "the program checkpoint cursor changed after the durable pause"
            )
        if checkpoint.attended_transition is not None:
            if manifest is None:
                raise BundleMismatch(
                    "the attended interpreter transition has no run manifest"
                )
            from openadapt_flow.runtime.durable.attended import (
                validate_attended_program_receipt,
            )

            validate_attended_program_receipt(
                store.run_dir,
                checkpoint=checkpoint,
                pending=pending,
                manifest=manifest,
                live_bundle_version=live_bundle_version,
            )
        if (
            checkpoint.bundle_version
            and checkpoint.bundle_version != live_bundle_version
        ):
            raise BundleMismatch(
                "the interpreter checkpoint was captured against bundle version "
                f"{checkpoint.bundle_version!r} but the bundle being resumed is "
                f"{live_bundle_version!r} — the program changed; re-run"
            )
        # Revalidate the live app is still in the checkpoint's expected state and
        # the already-confirmed effects still hold (raises StateDiverged).
        replayer.revalidate_program_checkpoint(checkpoint, store.completed_effects())

    from openadapt_flow.runtime.replayer import _DURABLE_RESUME_AUTHORITY

    effective_run_id = replayer._admit_durable_resume(
        _DURABLE_RESUME_AUTHORITY,
        mode="program",
        workflow=workflow,
        run_dir=store.run_dir,
        bundle_dir=bundle_dir,
        run_id=run_id,
        params=params,
        worklists=worklists,
        resume_from=None,
        resume_program=checkpoint,
    )
    replayer.durable = True
    return replayer.run(
        workflow,
        params=params,
        worklists=worklists,
        bundle_dir=bundle_dir,
        run_dir=store.run_dir,
        save_healed_to=(Path(save_healed_to) if save_healed_to else None),
        resume_program=checkpoint,
        run_id=effective_run_id,
        execution_target_kind=execution_target_kind,
        prior_screenshots_may_leave_box=(
            manifest.screenshots_may_leave_box if manifest is not None else False
        ),
        prior_model_calls=(manifest.model_calls if manifest is not None else 0),
        prior_external_network_calls=(
            manifest.external_network_calls if manifest is not None else "unknown"
        ),
    )

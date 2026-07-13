"""Resume a durably-paused run from its last verified checkpoint (RFC §5).

:func:`resume` is the Tier-3 resume entrypoint. Given a ``run_dir`` that holds
a :class:`~.checkpoint.PendingEscalation` (and the per-step checkpoints written
during the original run), it:

1. reads the :class:`~.checkpoint.RunManifest` to recover the bundle and the
   run's parameter bindings (so the caller need only supply a live
   ``Replayer`` -- a GUI automation cannot be resumed without a live backend);
2. determines the resume point = the last verified checkpoint's
   ``next_step_index`` (0 when nothing verified);
3. loads the workflow from the bundle and re-runs it **from that index
   onward** -- the paused step and everything after it, NEVER from step 0.

Steps before the resume point are not re-executed, so an already-confirmed
consequential write is never re-performed. Re-executing the PAUSED step itself
is safe by the effect layer's idempotency posture: a consequential write should
carry an ``idempotency_key`` (``runtime.effects.Effect``), so a re-issued write
that already landed collapses to one record rather than duplicating.

Explicit non-goal (RFC §5): resume is DETERMINISTIC. It hands the remaining
workflow to the SAME deterministic replayer, never to a free-form agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore


def resume_point(run_dir: Path | str) -> int:
    """The step index a resume would continue from for ``run_dir``.

    The last verified checkpoint's ``next_step_index``; 0 when nothing has
    verified yet (a run that halted on its very first step resumes from 0).
    """
    last = CheckpointStore(run_dir).last_checkpoint()
    return last.next_step_index if last is not None else 0


def resume(
    run_dir: Path | str,
    replayer: Any,
    *,
    bundle_dir: Optional[Path | str] = None,
    params: Optional[dict[str, str]] = None,
    save_healed_to: Optional[Path | str] = None,
) -> RunReport:
    """Resume a durably-paused run from its last verified checkpoint.

    Args:
        run_dir: The original run directory (holds the checkpoints, the
            manifest, and the pending escalation).
        replayer: A live :class:`~openadapt_flow.runtime.replayer.Replayer`
            (its backend/vision cannot be serialized, so the caller provides a
            fresh one bound to the recovered system). Durability is force-
            enabled on it so the resumed leg keeps checkpointing.
        bundle_dir: Override the bundle recorded in the manifest (rarely
            needed); defaults to the manifest's ``bundle_dir``.
        params: Override the parameter bindings recorded in the manifest;
            defaults to the manifest's ``params`` so the resume re-binds
            identically.
        save_healed_to: Override the manifest's healed-bundle path.

    Returns:
        The :class:`~openadapt_flow.ir.RunReport` for the resumed leg. Its
        ``results`` include synthetic entries for the already-verified steps
        (so ``success`` accounts for the whole workflow) followed by the
        freshly executed tail.

    Raises:
        FileNotFoundError: when ``run_dir`` has no manifest (it was not run
            durably) and no ``bundle_dir`` override is supplied.
    """
    run_dir = Path(run_dir)
    store = CheckpointStore(run_dir)
    manifest = store.read_manifest()

    resolved_bundle = bundle_dir or (manifest.bundle_dir if manifest else None)
    if resolved_bundle is None:
        raise FileNotFoundError(
            f"Cannot resume {run_dir}: no durable manifest was found and no "
            "bundle_dir was supplied. A resumable run must have been executed "
            "with durability enabled (Replayer(..., durable=True))."
        )
    resolved_params = (
        params if params is not None else (manifest.params if manifest else {})
    )
    resolved_healed = save_healed_to or (manifest.save_healed_to if manifest else None)

    workflow = Workflow.load(resolved_bundle)
    start_index = resume_point(run_dir)

    # Force durability on so the resumed leg keeps advancing the checkpoints
    # and can itself pause/resume again.
    replayer.durable = True
    return replayer.run(
        workflow,
        params=resolved_params,
        bundle_dir=Path(resolved_bundle),
        run_dir=run_dir,
        save_healed_to=(Path(resolved_healed) if resolved_healed else None),
        resume_from=start_index,
    )

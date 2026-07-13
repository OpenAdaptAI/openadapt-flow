"""Durable tiered runtime (RFC §5, Tier 3): checkpoint + pause/approve/resume.

The escalation tier of the Workflow-Program IR runtime
(``docs/design/WORKFLOW_PROGRAM_IR.md`` §5). Where the deterministic fast path
and the bounded local recovery cannot safely proceed, a production run must not
just die: it must **durably pause at the last verified checkpoint, persist why
it paused and the proposed operator options, and RESUME from that checkpoint**
after approval -- never from step 0, and never by handing the remaining
workflow to a free-form agent.

Public surface:

- Persistence models: :class:`RunCheckpoint`, :class:`PendingEscalation`,
  :class:`RunManifest`, and the :class:`CheckpointStore` that reads/writes them
  under a run directory.
- Runtime hook: :class:`DurableRun` (the replayer's per-run controller),
  :func:`classify_halt` (halt -> category + proposed operator options), and
  :func:`resumed_step_results` (reconstruct already-verified steps on resume).
- Resume: :func:`resume` and :func:`resume_point`.

Import-light by design (pydantic + json + pathlib): no vision, no backend, no
model call.
"""

from openadapt_flow.runtime.durable.checkpoint import (  # noqa: F401
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)
from openadapt_flow.runtime.durable.controller import (  # noqa: F401
    DurableRun,
    classify_halt,
    resumed_step_results,
)
from openadapt_flow.runtime.durable.resume import (  # noqa: F401
    resume,
    resume_point,
)

__all__ = [
    "CheckpointStore",
    "PendingEscalation",
    "RunCheckpoint",
    "RunManifest",
    "DurableRun",
    "classify_halt",
    "resumed_step_results",
    "resume",
    "resume_point",
]

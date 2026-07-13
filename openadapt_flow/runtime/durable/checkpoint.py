"""Durable checkpoint + pending-escalation store (RFC §5, Tier 3).

The Workflow-Program IR RFC (``docs/design/WORKFLOW_PROGRAM_IR.md`` §5) makes
the runtime three-tier: a deterministic fast path, a bounded local model
recovery, and -- when neither can safely proceed -- a **durable pause /
approve / resume from the last verified checkpoint**. Today's replayer only
had the fast path and a bare ``halt``: on failure the run just dies, and a
re-run starts from step 0. That is unsafe in production, because re-running a
workflow that already performed consequential writes re-performs them.

This module is the persistence substrate for Tier 3. It is deliberately
import-light (pydantic + json + pathlib only -- no vision, no backend, no
model), and it lives in its OWN package so the state-machine interpreter
(Phase 2, which rewrites ``replayer.py`` heavily) can adopt it with a minimal,
localized set of replayer touch-points (see the module docstring of
``openadapt_flow.runtime.durable.controller``).

Two durable artifacts, both written under the run directory:

- :class:`RunCheckpoint` -- one per VERIFIED step (identity passed, effects
  CONFIRMED, postconditions passed), written to ``run_dir/checkpoints/``. It
  records enough to RESUME: the step index/id (and, for the Phase-2 state
  machine, an optional ``state_id``), the run's parameter bindings, and the
  index to continue from. Deterministic, cheap, ``$0``.
- :class:`PendingEscalation` -- written to ``run_dir/pending_escalation.json``
  when the run HALTS for an operator (a non-CONFIRMED effect that escalates,
  an unmet consequential guard, an unconfirmed placeholder effect, an
  unresolved disambiguation, ...). It captures WHY the run paused and the
  proposed operator options, and points at the last verified checkpoint so a
  human can approve and RESUME from there -- never from step 0, and never by
  handing the remaining workflow to a free-form agent.

A small :class:`RunManifest` (``run_dir/checkpoints/_manifest.json``) records
the bundle directory and parameter bindings so :func:`~.resume.resume` can
reconstruct the run from ``run_dir`` alone.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

CHECKPOINTS_DIRNAME = "checkpoints"
MANIFEST_FILENAME = "_manifest.json"
PENDING_FILENAME = "pending_escalation.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunManifest(BaseModel):
    """Enough to reconstruct a run from its ``run_dir`` alone.

    Written once at run start (durability enabled). :func:`~.resume.resume`
    reads it to find the bundle and the parameter bindings without the caller
    re-supplying them.
    """

    schema_version: int = 1
    workflow_name: str
    #: The workflow bundle directory (absolute), source of ``workflow.json``
    #: and the template crops.
    bundle_dir: str
    #: The run's fully-resolved parameter bindings (defaults + caller
    #: overrides), so a resume re-binds identically.
    params: dict[str, str] = Field(default_factory=dict)
    #: Optional healed-bundle output path, mirrored from the original run.
    save_healed_to: Optional[str] = None
    created_at: str = Field(default_factory=_now)


class RunCheckpoint(BaseModel):
    """A durable marker that a step VERIFIED and the run may resume after it.

    Written after each step whose identity passed, whose declared effects were
    CONFIRMED (or a duplicate RECONCILED), and whose postconditions passed --
    i.e. every step that ``result.ok`` and did not halt. The last such
    checkpoint is the resume point: a run that pauses re-does only the paused
    step onward, so an already-confirmed consequential write is NEVER
    re-executed.
    """

    schema_version: int = 1
    workflow_name: str
    #: Index of the verified step in ``workflow.steps``.
    step_index: int
    step_id: str
    intent: str = ""
    #: Phase-2 state-machine state id, when the interpreter has one. None for
    #: the linear replayer -- the step index/id is the resume key. Present so
    #: the state-machine PR can populate it without a schema change.
    state_id: Optional[str] = None
    #: Where a resume continues: the NEXT step to execute (``step_index + 1``
    #: for the linear replayer). Stored explicitly so the state machine can
    #: point at an arbitrary successor state.
    next_step_index: int
    #: The run's parameter bindings at checkpoint time (resume re-binds these).
    params: dict[str, str] = Field(default_factory=dict)
    #: Verification evidence carried for the audit trail / operator.
    effect_verified: Optional[bool] = None
    postconditions_ok: Optional[bool] = None
    skipped: bool = False
    actuation: Optional[str] = None
    created_at: str = Field(default_factory=_now)


class PendingEscalation(BaseModel):
    """A durable pause: the run HALTED for an operator instead of dying.

    Captures the current state, WHY it paused, and the proposed operator
    options (derived from the halt reason / compensation escalation), plus the
    checkpoint to resume from. Persisted to ``run_dir/pending_escalation.json``;
    an operator (or a separate escalation path) reviews it, resolves the cause,
    and resumes -- deterministically, from :attr:`resume_from_index`, NOT from
    step 0 and NOT by delegating the tail of the workflow to a free-form agent
    (RFC §5 explicit non-goal).
    """

    schema_version: int = 1
    workflow_name: str
    #: The step that halted.
    step_index: int
    step_id: str
    intent: str = ""
    state_id: Optional[str] = None
    #: Coarse machine category (see :func:`classify_halt`): ``effect_refuted``,
    #: ``effect_indeterminate``, ``effect_escalated``, ``placeholder_effect``,
    #: ``effect_unverifiable``, ``unmet_guard``, ``disambiguation``,
    #: ``identity``, ``postcondition``, ``resolution``, or ``halt``.
    category: str
    #: The verbatim halt reason (``result.error``) -- WHY it paused.
    reason: str = ""
    #: Supporting audit lines (e.g. ``result.effect_results`` verdicts).
    detail: list[str] = Field(default_factory=list)
    #: Operator-facing next actions proposed for this pause (from the halt
    #: reason / compensation). Approval and resume are always among them.
    proposed_options: list[str] = Field(default_factory=list)
    #: The step index a resume continues from = the last verified checkpoint's
    #: ``next_step_index`` (0 when nothing verified yet). The paused step and
    #: everything after it re-run; verified steps before it do NOT.
    resume_from_index: int = 0
    resume_from_step_id: Optional[str] = None
    #: The run's parameter bindings, so an approved resume re-binds identically.
    params: dict[str, str] = Field(default_factory=dict)
    status: Literal["pending", "approved"] = "pending"
    created_at: str = Field(default_factory=_now)


class CheckpointStore:
    """Read/write the durable artifacts under a run directory.

    Layout::

        run_dir/
          checkpoints/
            _manifest.json            # RunManifest
            step_0000_<id>.json       # RunCheckpoint (one per verified step)
            step_0001_<id>.json
          pending_escalation.json     # PendingEscalation (present iff paused)
    """

    def __init__(self, run_dir: Path | str) -> None:
        self.run_dir = Path(run_dir)
        self.checkpoints_dir = self.run_dir / CHECKPOINTS_DIRNAME

    # -- manifest ------------------------------------------------------------

    def write_manifest(self, manifest: RunManifest) -> Path:
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        path = self.checkpoints_dir / MANIFEST_FILENAME
        path.write_text(manifest.model_dump_json(indent=2))
        return path

    def read_manifest(self) -> Optional[RunManifest]:
        path = self.checkpoints_dir / MANIFEST_FILENAME
        if not path.is_file():
            return None
        return RunManifest.model_validate(json.loads(path.read_text()))

    # -- checkpoints ---------------------------------------------------------

    @staticmethod
    def _safe(step_id: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in step_id)

    def _checkpoint_path(self, checkpoint: RunCheckpoint) -> Path:
        name = (
            f"step_{checkpoint.step_index:04d}_"
            f"{self._safe(checkpoint.step_id)}.json"
        )
        return self.checkpoints_dir / name

    def write_checkpoint(self, checkpoint: RunCheckpoint) -> Path:
        """Persist a verified-step checkpoint.

        Idempotent per step index: re-writing the same index overwrites the
        file, never appends a duplicate -- so a resume that re-verifies a step
        cannot produce two checkpoints for it.
        """
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        path = self._checkpoint_path(checkpoint)
        path.write_text(checkpoint.model_dump_json(indent=2))
        return path

    def checkpoints(self) -> list[RunCheckpoint]:
        """All checkpoints, ordered by step index."""
        if not self.checkpoints_dir.is_dir():
            return []
        out: list[RunCheckpoint] = []
        for path in self.checkpoints_dir.glob("step_*.json"):
            out.append(RunCheckpoint.model_validate(json.loads(path.read_text())))
        out.sort(key=lambda c: c.step_index)
        return out

    def last_checkpoint(self) -> Optional[RunCheckpoint]:
        """The highest-index verified checkpoint, or None if nothing verified."""
        checkpoints = self.checkpoints()
        return checkpoints[-1] if checkpoints else None

    # -- pending escalation --------------------------------------------------

    def _pending_path(self) -> Path:
        return self.run_dir / PENDING_FILENAME

    def write_pending(self, pending: PendingEscalation) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self._pending_path()
        path.write_text(pending.model_dump_json(indent=2))
        return path

    def read_pending(self) -> Optional[PendingEscalation]:
        path = self._pending_path()
        if not path.is_file():
            return None
        return PendingEscalation.model_validate(json.loads(path.read_text()))

    def clear_pending(self) -> None:
        """Remove a resolved pending escalation (called when a resume starts)."""
        path = self._pending_path()
        if path.is_file():
            path.unlink()

"""Passive routine discovery — Robotic Process Mining (RPM) for openadapt-flow.

Where the compiler (``openadapt_flow.compiler``) turns ONE deliberately-recorded
demonstration into ONE workflow, this package mines routines from a PASSIVELY
captured, UNSEGMENTED UI-event log: one continuous stream that interleaves many
task instances (across sessions / workers) with noise and unrelated actions,
where nobody pressed "record" to mark task boundaries.

The pipeline (``routine_discovery``) segments that stream into candidate task
instances, mines the repeated control-flow patterns across segments, and emits
CANDIDATE ROUTINES — each a set of position-aligned trace instances with a
support/confidence score. That output is the INPUT a downstream multi-trace
induction step consumes to synthesize a parameterized ``WorkflowProgram`` (the
control-flow graph of ``docs/design/WORKFLOW_PROGRAM_IR.md``). Induction itself
is intentionally NOT built here (a sibling effort); the hand-off is the thin
:class:`~openadapt_flow.mining.routine_discovery.SegmentedRoutine` protocol so
the two decouple.

Literature this implements (see ``routine_discovery`` docstring for the mapping):

* Leno et al., "Identifying Candidate Routines for RPA from Unsegmented UI
  Logs" (ICPM'20) — the frequent-pattern-over-an-unsegmented-log framing.
* Agostinelli et al., "Automated Segmentation of UI Logs" — recovering task
  instance boundaries (start/end markers, gaps).
* Bosco et al., "Discovering Automatable Routines from User Interaction Logs"
  (BPM'19) — mining deterministic, automatable routines.

Evaluation is on SYNTHETIC logs (``synth``) — the standard process-mining
approach where ground truth is known. Multi-source input adapters (``sources``)
prove the pipeline consumes non-demo sources (a computer-use agent trajectory, an
existing RPA / Playwright-codegen script), not only recorded demos.

No model calls anywhere in this package — discovery is purely structural.
"""

from __future__ import annotations

from openadapt_flow.mining.routine_discovery import (
    CandidateRoutine,
    DiscardedPattern,
    RoutineDiscoveryResult,
    RoutineInstance,
    SegmentedRoutine,
    action_signature,
    discover_routines,
)

__all__ = [
    "CandidateRoutine",
    "DiscardedPattern",
    "RoutineDiscoveryResult",
    "RoutineInstance",
    "SegmentedRoutine",
    "action_signature",
    "discover_routines",
]

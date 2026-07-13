"""Multi-trace induction: infer a parameterized PROGRAM from several demos.

One demonstration is *evidence*, not *specification* (RFC
``docs/design/WORKFLOW_PROGRAM_IR.md`` §1, §3): the same single trace is
consistent with many distinct programs. The whole programming-by-demonstration
lineage a demonstration compiler descends from -- Rousillon/Helena, WebRobot,
Skill-DisCo, PROLEX -- closes that gap with an **induction loop** that
generalizes from *multiple* demonstrations of the same task. This module
implements RFC §3 step [4] (multi-trace induction) + step [5] (held-out
validation / quarantine): it turns ``list[Workflow | recording-dir]`` into a
:class:`~openadapt_flow.ir.ProgramGraph` -- the Phase-2 state machine -- *or an
honest refusal*.

What it infers, and how (all deterministic + structural -- ZERO model calls):

* **Parameters.** A value that VARIES across traces at the same aligned
  position is a typed :class:`~openadapt_flow.ir.ParamSpec`; a value that is
  CONSTANT across traces stays a baked literal (WebRobot-style value
  speculation, made *determinate* by cross-trace evidence rather than guessed).
* **Loops.** A repeated aligned sub-sequence whose repetition count DIFFERS
  across traces (a worklist of length 2 vs 3) is a
  :class:`~openadapt_flow.ir.LoopSpec` over an inferred
  :class:`~openadapt_flow.ir.Relation`, its body the repeated subflow
  (Rousillon/Helena "for every row ...").
* **Branches.** A step present/divergent in some traces but not others UNDER A
  DETECTABLE CONDITION is a ``branch`` state with guarded transitions -- the
  guard is *proposed and flagged for confirmation* (Skill-DisCo: divergences
  localize the branch automatically).
* **Optional steps.** Present in some, absent in others, with NO derivable
  condition -> an optional/guarded step that SKIPS when its own target is
  absent.

**Refuse rather than guess** (RFC §3 [5]; mirrors ``runtime.identity`` and
``compiler.disambiguation``). When traces CONTRADICT or intent stays
underdetermined -- a divergent branch with no detectable condition, an
irreconcilable ordering -- induction does NOT fabricate a program. It marks the
point ``underdetermined`` with the specific ambiguity, routes it to the
disambiguation flow (:mod:`openadapt_flow.compiler.disambiguation`), leaves
``certified=False``, and does not emit a program. A wrong branch on an
irreversible node is the failure class the whole repo is organized to avoid.

**The compile-time model only PROPOSES.** An optional :class:`Proposer` (the
compile-time StepAnnotator lives behind this interface) may propose an
interpretation for an ambiguous point, but every proposal is recorded in
``proposed`` / attached to the uncertainty and is NEVER silently trusted: a
proposal cannot flip an ``underdetermined`` point to certified. Deterministic
structural inference is the core; the model is advisory and flagged.

Touch-points (kept minimal per the stacking constraint on PR #79):

* Reuses the Phase-2 IR (``ProgramGraph`` / ``State`` / ``Transition`` /
  ``LoopSpec`` / ``Relation``) and Phase-1 ``ParamSpec`` / ``Predicate`` /
  ``Guard`` VERBATIM -- adds NO new IR fields.
* Reuses ``compiler.disambiguation``'s question model to route uncertainties to
  the SAME ask-don't-guess flow (#74).
* ``compiler/compile.py`` is UNCHANGED -- induction runs as a pass OVER
  already-compiled workflows (``compile_recording`` is the single-trace
  bootstrap, RFC §3 [1]); a recording-dir trace is compiled through it.
* The emitted program replays through the EXISTING Phase-2 interpreter
  (``runtime.replayer``) with zero changes -- proven by the round-trip test.
"""

from __future__ import annotations

import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional, Protocol, Union, runtime_checkable

from pydantic import BaseModel, Field

from openadapt_flow.compiler.disambiguation import (
    AmbiguityKind,
    DisambiguationQuestion,
    OptionEffect,
    QuestionOption,
)
from openadapt_flow.ir import (
    ActionKind,
    Guard,
    LoopSpec,
    ParamKind,
    ParamSpec,
    Predicate,
    PredicateKind,
    ProgramGraph,
    Relation,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)

TraceInput = Union[Workflow, str, Path]

# A typed value shorter than this (stripped) is too weak to speculate on as a
# parameter -- mirrors disambiguation.MIN_PARAM_VALUE_CHARS.
MIN_PARAM_VALUE_CHARS = 1


# ===========================================================================
# Compile-time model, behind an interface: it PROPOSES, it never decides.
# ===========================================================================


class Proposal(BaseModel):
    """A compile-time-model suggestion for an under-specified point.

    ADVISORY ONLY. A proposal is surfaced in :attr:`InductionResult.proposed`
    (and attached to any uncertainty it concerns) so a reviewer sees it, but it
    NEVER silently changes the deterministic structural decision and NEVER flips
    an ``underdetermined`` point to certified (``trusted`` is always False --
    the field exists to make that contract explicit and auditable).
    """

    target: str = Field(description="Column / state id the proposal concerns")
    kind: str = Field(description="'guard' | 'param' | 'label'")
    content: str = Field(description="Human-readable proposed interpretation")
    source: str = "annotator"
    trusted: bool = Field(
        default=False,
        description="Always False: proposals are flagged, never auto-applied.",
    )


@runtime_checkable
class Proposer(Protocol):
    """Compile-time interpretation proposer (the #78 StepAnnotator fits here).

    Called at MOST once per ambiguous/branch point at COMPILE time. Any
    implementation that reaches a model does so here, once -- never at run time
    (the runtime's audited $0 / 0-call property is preserved). Tests pass a
    deterministic fake; a real annotator may consult a VLM. Either way the
    return value is advisory (see :class:`Proposal`).
    """

    def propose(self, target: str, kind: str, context: dict[str, Any]) -> Optional[str]:
        """Return a human-readable proposed interpretation, or None to abstain."""
        ...


# ===========================================================================
# Result model
# ===========================================================================


class Uncertainty(BaseModel):
    """A point where intent stays underdetermined -- the reason induction
    REFUSES to emit (RFC §3 [5] quarantine). Routed to the disambiguation flow
    via :attr:`question`."""

    kind: str = Field(
        description="'ambiguous_branch' | 'alignment_failure' | 'unobserved'"
    )
    location: str = Field(description="Column / position the ambiguity is at")
    detail: str
    consequential: bool = Field(
        default=True,
        description="True when a wrong resolution risks an irreversible action.",
    )
    question: Optional[DisambiguationQuestion] = Field(
        default=None,
        description="The grounded question routed to compiler.disambiguation.",
    )
    proposal: Optional[Proposal] = Field(
        default=None,
        description="A compile-time-model suggestion -- flagged, NOT trusted.",
    )


class ColumnDecision(BaseModel):
    """The induced interpretation of one aligned column, kept for the audit
    trail AND for held-out reproduction scoring (:func:`reproduction_score`)."""

    index: int
    kind: str  # literal | param | loop | branch | optional | divergent
    align_sig: str = Field(description="Stringified alignment signature")
    field: str = ""
    literal_value: Optional[str] = None  # kind == literal
    param_name: Optional[str] = None  # kind == param / loop
    present_in: list[int] = Field(default_factory=list)  # trace indices
    counts: list[int] = Field(
        default_factory=list, description="Per-trace loop iteration counts."
    )
    note: str = ""


class InductionResult(BaseModel):
    """The induced program (or an honest refusal) plus the full audit trail."""

    n_traces: int
    program: Optional[ProgramGraph] = None
    workflow: Optional[Workflow] = Field(
        default=None,
        description="A replayable Workflow carrying program/subflows/"
        "param_specs/data_sources (None when quarantined).",
    )
    param_specs: dict[str, ParamSpec] = Field(default_factory=dict)
    column_decisions: list[ColumnDecision] = Field(default_factory=list)
    inferred: list[str] = Field(
        default_factory=list,
        description="What was inferred DETERMINISTICALLY (params/loops/opt).",
    )
    proposed: list[Proposal] = Field(
        default_factory=list,
        description="Compile-time-model suggestions -- flagged, never trusted.",
    )
    uncertainties: list[Uncertainty] = Field(default_factory=list)

    @property
    def underdetermined(self) -> bool:
        return bool(self.uncertainties)

    @property
    def certified(self) -> bool:
        """False iff any point stays underdetermined (refuse rather than
        guess) OR no program was emitted. A flagged proposal does NOT certify."""
        return self.program is not None and not self.uncertainties

    def render(self) -> str:
        lines = [
            f"Induction over {self.n_traces} trace(s): "
            f"{len(self.inferred)} inferred, {len(self.proposed)} proposed "
            f"(flagged), {len(self.uncertainties)} underdetermined."
        ]
        for line in self.inferred:
            lines.append(f"  [inferred]  {line}")
        for p in self.proposed:
            lines.append(f"  [proposed]  {p.target}: {p.content}  (NOT trusted)")
        for u in self.uncertainties:
            lines.append(f"  [REFUSED]   {u.location}: {u.detail}")
        verdict = "CERTIFIED" if self.certified else "NOT CERTIFIED"
        lines.append(
            f"Certification: {verdict}"
            + ("" if self.certified else " -- resolve the point(s) above.")
        )
        return "\n".join(lines)


class HeldOutValidation(BaseModel):
    """Leave-one-out held-out validation (RFC §3 [5]): infer from N-1 traces,
    check the induced program reproduces the held-out trace."""

    per_trace: list[float] = Field(default_factory=list)
    mean: float = 0.0
    n_traces: int = 0

    def render(self) -> str:
        scores = ", ".join(f"{s:.2f}" for s in self.per_trace)
        return (
            f"Held-out validation ({self.n_traces} folds): "
            f"mean={self.mean:.2f} [{scores}]"
        )


# ===========================================================================
# Trace normalization + signatures
# ===========================================================================


def _as_workflow(trace: TraceInput) -> Workflow:
    """Normalize a trace to a linear ``Workflow``. A recording DIRECTORY is
    compiled through the single-trace bootstrap ``compile_recording`` (RFC §3
    [1]); a ``Workflow`` is used as-is."""
    if isinstance(trace, Workflow):
        return trace
    from openadapt_flow.compiler.compile import compile_recording

    recording = Path(trace)
    with tempfile.TemporaryDirectory(prefix="induce-boot-") as tmp:
        return compile_recording(recording, Path(tmp) / "bundle", name=recording.name)


_LEADING_VERBS = frozenset(
    {"type", "enter", "input", "click", "press", "select", "set", "fill"}
)


def _norm_intent(step: Step) -> str:
    """A value-free field label from a step's intent (lowercased, digits
    dropped so a per-run value in the intent does not fragment alignment, and a
    leading action verb dropped so 'type patient' keys as 'patient')."""
    text = "".join(c for c in step.intent.lower() if not c.isdigit())
    words = text.split()
    if words and words[0] in _LEADING_VERBS:
        words = words[1:]
    return " ".join(words) or " ".join(text.split())


def _field_key(step: Step) -> str:
    """A stable, VALUE-FREE identity for the field/target a step acts on -- the
    dimension alignment matches on (so the SAME field with a DIFFERENT value
    aligns, revealing a parameter rather than a structural change)."""
    if step.action is ActionKind.KEY:
        # Distinct keys are distinct control-flow (Enter vs Escape), so the key
        # IS part of the field identity here.
        return f"key:{step.key}"
    if step.param:
        return step.param
    if step.anchor is not None and step.anchor.ocr_text:
        return step.anchor.ocr_text
    return _norm_intent(step)


def _sig(step: Step) -> tuple[str, str]:
    return (step.action.value, _field_key(step))


def _value(step: Step) -> Optional[str]:
    """The per-run VALUE a step carries (the thing that varies for a param)."""
    if step.action is ActionKind.TYPE:
        return step.text if step.text is not None else step.param
    if step.action is ActionKind.KEY:
        return step.key
    if step.anchor is not None:
        return step.anchor.ocr_text
    return None


# Dialog detection is shared with disambiguation so induction and interactive
# disambiguation agree on what "an optional popup" looks like.
from openadapt_flow.compiler.disambiguation import (  # noqa: E402
    _dialog_text,
    _looks_like_dialog,
)

# ===========================================================================
# Reduce a trace to tokens (collapse consecutive repeats into loop candidates)
# ===========================================================================


class _SingleTok:
    kind = "single"

    def __init__(self, step: Step):
        self.step = step
        self.align_sig: tuple = _sig(step)

    @property
    def value(self) -> Optional[str]:
        return _value(self.step)


class _LoopTok:
    kind = "loop"

    def __init__(self, body_steps: list[Step], iterations: list[list[Step]]):
        self.body_steps = body_steps
        self.iterations = iterations  # per-iteration list of steps
        body_sig = tuple(_sig(s) for s in body_steps)
        self.align_sig = ("__loop__", body_sig)

    @property
    def count(self) -> int:
        return len(self.iterations)

    def rows(self, field: str) -> list[dict[str, str]]:
        """Per-iteration binding of ``field`` -> value (single-column body)."""
        out: list[dict[str, str]] = []
        for it in self.iterations:
            val = _value(it[0]) if it else None
            out.append({field: val if val is not None else ""})
        return out


def _reduce_trace(steps: list[Step]) -> list:
    """Collapse maximal CONSECUTIVE repeated sub-sequences into ``_LoopTok``s;
    everything else stays a ``_SingleTok``. Smallest repeating block wins (the
    tightest loop body). Model-free and purely structural."""
    sigs = [_sig(s) for s in steps]
    n = len(steps)
    toks: list = []
    i = 0
    while i < n:
        found: Optional[tuple[int, int]] = None
        max_len = (n - i) // 2
        for length in range(1, max_len + 1):
            block = sigs[i : i + length]
            reps = 1
            while sigs[i + reps * length : i + reps * length + length] == block:
                reps += 1
            if reps >= 2:
                found = (length, reps)
                break
        if found is not None:
            length, reps = found
            body_steps = steps[i : i + length]
            iterations = [
                steps[i + r * length : i + r * length + length] for r in range(reps)
            ]
            toks.append(_LoopTok(body_steps, iterations))
            i += length * reps
        else:
            toks.append(_SingleTok(steps[i]))
            i += 1
    return toks


# ===========================================================================
# Multiple-trace alignment (incremental LCS merge of reduced token sequences)
# ===========================================================================


class _Column:
    """One aligned position across traces. ``tokens[t]`` is trace ``t``'s token
    at this position, or absent when the trace does not have this column."""

    def __init__(self, align_sig, kind: str):
        self.align_sig = align_sig
        self.kind = kind  # "single" | "loop"
        self.tokens: dict[int, Any] = {}
        # For a "replace" divergence: the id of the mutually-exclusive column
        # this one is paired against (same position, different content).
        self.divergent_group: Optional[int] = None


def _align(reduced: list[list]) -> list[_Column]:
    """Align the reduced token sequences of every trace into ordered columns.

    Incremental merge: start from trace 0's columns, then fold each subsequent
    trace in with a ``difflib`` alignment over the token ALIGN-SIGNATURES.
    ``equal`` blocks attach to existing columns; ``insert`` blocks add columns
    present only where seen; ``delete`` blocks leave columns absent in the new
    trace; ``replace`` blocks are recorded as mutually-exclusive DIVERGENCES
    (branch-or-contradiction candidates).
    """
    if not reduced:
        return []
    columns: list[_Column] = []
    for tok in reduced[0]:
        col = _Column(tok.align_sig, tok.kind)
        col.tokens[0] = tok
        columns.append(col)

    div_group = 0
    for t in range(1, len(reduced)):
        seq = reduced[t]
        a = [c.align_sig for c in columns]
        b = [tok.align_sig for tok in seq]
        sm = SequenceMatcher(a=a, b=b, autojunk=False)
        merged: list[_Column] = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    col = columns[i1 + k]
                    col.tokens[t] = seq[j1 + k]
                    merged.append(col)
            elif tag == "delete":
                # In consensus, absent from this trace -> optional so far.
                merged.extend(columns[i1:i2])
            elif tag == "insert":
                # New in this trace only.
                for j in range(j1, j2):
                    tok = seq[j]
                    col = _Column(tok.align_sig, tok.kind)
                    col.tokens[t] = tok
                    merged.append(col)
            elif tag == "replace":
                # Mutually-exclusive divergence: keep BOTH sides, tagged as one
                # group, so interpretation can decide branch vs contradiction.
                for col in columns[i1:i2]:
                    col.divergent_group = div_group
                    merged.append(col)
                for j in range(j1, j2):
                    tok = seq[j]
                    col = _Column(tok.align_sig, tok.kind)
                    col.tokens[t] = tok
                    col.divergent_group = div_group
                    merged.append(col)
                div_group += 1
        columns = merged
    return columns


# ===========================================================================
# Interpretation: aligned columns -> ProgramGraph (or refusal)
# ===========================================================================


def _is_irreversible(col: _Column) -> bool:
    return any(
        tok.kind == "single" and tok.step.risk == "irreversible"
        for tok in col.tokens.values()
    )


def _param_name_for(field: str, taken: set[str], fallback: str) -> str:
    base = "".join(c if c.isalnum() else "_" for c in field.lower()).strip("_")
    base = "_".join(p for p in base.split("_") if p) or fallback
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


class _Node:
    """A contiguous chunk of emitted graph with one ``entry`` state and a set of
    ``exits`` (transitions whose target is patched to the following node)."""

    def __init__(self, entry: str):
        self.entry = entry
        self.states: dict[str, State] = {}
        self.exits: list[Transition] = []

    def set_next(self, next_id: str) -> None:
        for tr in self.exits:
            tr.target = next_id


def induce_program(
    traces: list[TraceInput], *, propose: Optional[Proposer] = None
) -> InductionResult:
    """Induce a parameterized :class:`ProgramGraph` from multiple demonstrations
    of the same task -- the RFC §3 induction loop (step [4] + [5]).

    Deterministic and model-free at its core (align -> params / loops /
    branches / optional). An optional :class:`Proposer` may PROPOSE an
    interpretation for an ambiguous point; every proposal is flagged and never
    silently trusted. When intent stays underdetermined the induced program is
    QUARANTINED (not emitted) and ``result.certified is False``.

    Args:
        traces: Two or more traces (compiled ``Workflow``s or recording dirs)
            of the SAME task. One trace is the degenerate bootstrap.
        propose: Optional compile-time interpretation proposer (advisory).

    Returns:
        An :class:`InductionResult` with the induced program (or a refusal) and
        the full audit trail.
    """
    workflows = [_as_workflow(t) for t in traces]
    n = len(workflows)
    result = InductionResult(n_traces=n)
    if n == 0:
        return result

    reduced = [_reduce_trace(wf.steps) for wf in workflows]
    columns = _align(reduced)

    # Detect an alignment failure: too little shared structure to trust that
    # these are traces of the SAME task (refuse rather than induce noise).
    shared = sum(1 for c in columns if len(c.tokens) == n)
    if n >= 2 and columns and shared == 0:
        result.uncertainties.append(
            Uncertainty(
                kind="alignment_failure",
                location="<whole trace>",
                detail=(
                    "traces share no aligned steps -- cannot infer a common "
                    "program (are these the same task?)."
                ),
            )
        )
        return result

    decisions: list[ColumnDecision] = []
    nodes: list[_Node] = []
    param_specs: dict[str, ParamSpec] = {}
    data_sources: dict[str, Relation] = {}
    taken: set[str] = set()
    seen_div_groups: set[int] = set()

    for idx, col in enumerate(columns):
        present = sorted(col.tokens)

        # --- divergence (replace region): branch or contradiction ----------
        if col.divergent_group is not None:
            grp = col.divergent_group
            if grp in seen_div_groups:
                continue  # the whole group is handled once, at its first column
            seen_div_groups.add(grp)
            group_cols = [c for c in columns if c.divergent_group == grp]
            _handle_divergence(idx, group_cols, workflows, result, propose, decisions)
            # Underdetermined divergence => refuse to emit (handled below).
            continue

        # --- loop ----------------------------------------------------------
        if col.kind == "loop":
            node, dec = _emit_loop(
                idx, col, present, n, taken, param_specs, data_sources
            )
            nodes.append(node)
            decisions.append(dec)
            result.inferred.append(dec.note)
            continue

        # --- single step present in ALL traces: literal vs param ----------
        if len(present) == n:
            node, dec = _emit_required_single(
                idx, col, present, taken, param_specs, result
            )
            nodes.append(node)
            decisions.append(dec)
            continue

        # --- single step present in SOME traces: branch or optional -------
        node, dec = _emit_optional_single(idx, col, present, n, result, propose)
        nodes.append(node)
        decisions.append(dec)

    result.column_decisions = decisions
    result.param_specs = param_specs

    # Refuse rather than guess: any underdetermined point quarantines the whole
    # program (RFC §3 [5]) -- do NOT emit a program that guesses a branch.
    if result.uncertainties:
        return result

    program, subflows = _wire(nodes)
    if program is None:
        return result
    workflow = Workflow(
        name="induced-program",
        program=program,
        subflows=subflows,
        param_specs=param_specs,
        params={k: (v.example or "") for k, v in param_specs.items()},
        data_sources=data_sources,
    )
    result.program = program
    result.workflow = workflow
    return result


def _emit_loop(idx, col, present, n, taken, param_specs, data_sources):
    loop_toks = [col.tokens[t] for t in present]
    body_steps = loop_toks[0].body_steps
    field = _field_key(body_steps[0]) if body_steps else f"row_{idx}"
    counts = [tok.count for tok in loop_toks]

    param_name = _param_name_for(field, taken, f"row_{idx}")
    taken.add(param_name)
    # Representative worklist: the LONGEST demonstrated queue, inlined so the
    # bundle is self-contained (a run may override via Replayer worklists=...).
    rep = max(loop_toks, key=lambda tk: tk.count)
    rel_name = f"worklist_{idx}"
    data_sources[rel_name] = Relation(
        name=rel_name,
        rows=rep.rows(param_name),
        description=f"inferred worklist for the '{field}' loop",
    )
    param_specs[param_name] = ParamSpec(
        name=param_name,
        type=ParamKind.STRING,
        example=(rep.rows(param_name)[0][param_name] if rep.count else None),
        required=True,
    )

    # Body subflow: the repeated step(s), the varying field bound per row.
    body_id = f"body_{idx}"
    body_states: dict[str, State] = {}
    prev_ids: list[str] = []
    for bi, bstep in enumerate(body_steps):
        sid = f"{body_id}_s{bi}"
        step = bstep.model_copy(deep=True)
        if step.action is ActionKind.TYPE:
            step.param = param_name
            step.text = None
        st = State(
            id=sid,
            kind=StateKind.ACTION,
            step=step,
            transitions=[Transition(target="__PATCH__")],
        )
        body_states[sid] = st
        prev_ids.append(sid)
    body_end = f"{body_id}_end"
    body_states[body_end] = State(
        id=body_end, kind=StateKind.TERMINAL, outcome="success"
    )
    ordered = list(body_states)
    for k, sid in enumerate(ordered):
        st = body_states[sid]
        if st.kind is StateKind.ACTION:
            st.transitions[0].target = (
                ordered[k + 1] if k + 1 < len(ordered) else body_end
            )
    body_graph = ProgramGraph(entry=ordered[0], states=body_states)

    loop_state_id = f"loop_{idx}"
    node = _Node(loop_state_id)
    exit_tr = Transition(target="__PATCH__")
    node.states[loop_state_id] = State(
        id=loop_state_id,
        kind=StateKind.LOOP,
        loop=LoopSpec(relation=rel_name, body=body_id, var=field),
        transitions=[exit_tr],
    )
    node.exits = [exit_tr]
    node._subflow = (body_id, body_graph)  # type: ignore[attr-defined]

    dec = ColumnDecision(
        index=idx,
        kind="loop",
        align_sig=str(col.align_sig),
        field=field,
        param_name=param_name,
        present_in=present,
        counts=counts,
        note=(
            f"loop over '{field}' -- body repeats {counts} across traces "
            f"(counts differ => a worklist, not unrolled steps)"
        ),
    )
    return node, dec


def _emit_required_single(idx, col, present, taken, param_specs, result):
    toks = [col.tokens[t] for t in present]
    step0 = toks[0].step
    field = _field_key(step0)
    values = [tok.value for tok in toks]
    sid = f"s{idx}"
    step = step0.model_copy(deep=True)
    exit_tr = Transition(target="__PATCH__")

    varies = len({v for v in values}) > 1
    if step0.action is ActionKind.TYPE and varies:
        # A value that VARIES across traces at the same field is a PARAMETER
        # (cross-trace evidence makes WebRobot value-speculation determinate).
        name = _param_name_for(field, taken, f"value_{idx}")
        taken.add(name)
        step.param = name
        step.text = None
        example = next((v for v in values if v), None)
        param_specs[name] = ParamSpec(
            name=name, type=ParamKind.STRING, example=example, required=True
        )
        result.inferred.append(
            f"param '{name}' -- '{field}' varies across traces {values}"
        )
        kind, param_name, literal = "param", name, None
    else:
        literal = values[0]
        if step0.action is ActionKind.TYPE:
            result.inferred.append(
                f"literal '{field}' = {literal!r} -- constant across traces"
            )
        kind, param_name = "literal", None

    node = _Node(sid)
    node.states[sid] = State(
        id=sid, kind=StateKind.ACTION, step=step, transitions=[exit_tr]
    )
    node.exits = [exit_tr]
    dec = ColumnDecision(
        index=idx,
        kind=kind,
        align_sig=str(col.align_sig),
        field=field,
        literal_value=literal if kind == "literal" else None,
        param_name=param_name,
        present_in=present,
    )
    return node, dec


def _emit_optional_single(idx, col, present, n, result, propose):
    tok = col.tokens[present[0]]
    step0 = tok.step
    field = _field_key(step0)
    is_dialog = _looks_like_dialog(step0)

    if is_dialog:
        # A DETECTABLE condition (the dialog's own presence) -> a guarded BRANCH
        # (RFC §2.2). The guard is PROPOSED and flagged for confirmation. The
        # dialog LABEL is extracted the same way disambiguation does (#74), so
        # both agree on what text signals the popup.
        dialog_text = _dialog_text(step0)
        branch_id = f"branch_{idx}"
        do_id = f"opt_{idx}"
        do_step = step0.model_copy(deep=True)
        fall_tr = Transition(target="__PATCH__")
        do_tr = Transition(target="__PATCH__")
        node = _Node(branch_id)
        node.states[branch_id] = State(
            id=branch_id,
            kind=StateKind.BRANCH,
            transitions=[
                Transition(
                    guard=Predicate(
                        kind=PredicateKind.TEXT_PRESENT,
                        text=dialog_text,
                        intent="optional dialog is present",
                    ),
                    target=do_id,
                    label="dialog present",
                ),
                fall_tr,
            ],
        )
        node.states[do_id] = State(
            id=do_id, kind=StateKind.ACTION, step=do_step, transitions=[do_tr]
        )
        node.exits = [fall_tr, do_tr]
        result.inferred.append(
            f"branch on optional dialog {dialog_text!r} at column {idx} "
            f"(present in {present}/{n} traces) -- guard TEXT_PRESENT"
        )
        proposed = _maybe_propose(
            propose, branch_id, "guard", {"dialog": dialog_text}, result
        )
        result.proposed.append(
            Proposal(
                target=branch_id,
                kind="guard",
                content=(proposed or f"confirm guard: TEXT_PRESENT({dialog_text!r})"),
            )
        )
        dec = ColumnDecision(
            index=idx,
            kind="branch",
            align_sig=str(col.align_sig),
            field=field,
            present_in=present,
            note=f"optional dialog branch ({dialog_text!r})",
        )
        return node, dec

    # No derivable condition -> an OPTIONAL guarded step that SKIPs when its own
    # target is absent (Guard on_unmet='skip', predicate ANCHOR_RESOLVES).
    sid = f"opt_{idx}"
    step = step0.model_copy(deep=True)
    exit_tr = Transition(target="__PATCH__")
    guard_pred = (
        Predicate(
            kind=PredicateKind.ANCHOR_RESOLVES,
            anchor=step.anchor,
            intent=f"optional step '{field}' target present",
        )
        if step.anchor is not None
        else Predicate(
            kind=PredicateKind.TEXT_PRESENT,
            text=field,
            intent=f"optional step '{field}' present",
        )
    )
    step.guard = Guard(predicate=guard_pred, on_unmet="skip")
    node = _Node(sid)
    node.states[sid] = State(
        id=sid, kind=StateKind.ACTION, step=step, transitions=[exit_tr]
    )
    node.exits = [exit_tr]
    result.inferred.append(
        f"optional step '{field}' at column {idx} "
        f"(present in {present}/{n} traces) -- skip when absent"
    )
    dec = ColumnDecision(
        index=idx,
        kind="optional",
        align_sig=str(col.align_sig),
        field=field,
        present_in=present,
        note=f"optional step '{field}' (guarded skip)",
    )
    return node, dec


def _handle_divergence(idx, group_cols, workflows, result, propose, decisions):
    """A ``replace`` divergence: mutually-exclusive content at the same aligned
    position. If a discriminating condition were DETECTABLE it would be a
    branch; here none is derivable structurally, so this is a contradiction the
    traces do not resolve -- REFUSE (RFC §3 [5]) and route to disambiguation.
    A :class:`Proposer` may suggest a guard, but it is flagged, NEVER trusted.
    """
    labels = []
    for col in group_cols:
        for tok in col.tokens.values():
            if tok.kind == "single":
                labels.append(_value(tok.step) or _field_key(tok.step))
    labels = list(dict.fromkeys(labels))
    consequential = any(_is_irreversible(c) for c in group_cols)

    proposal_content = _maybe_propose(
        propose,
        f"divergence_{idx}",
        "guard",
        {"arms": labels},
        result,
    )
    proposal = (
        Proposal(
            target=f"divergence_{idx}",
            kind="guard",
            content=proposal_content,
        )
        if proposal_content
        else None
    )
    if proposal is not None:
        result.proposed.append(proposal)

    question = DisambiguationQuestion(
        id=f"{AmbiguityKind.OPTIONAL_DIALOG.value}:divergence_{idx}",
        kind=AmbiguityKind.OPTIONAL_DIALOG,
        step_id=f"divergence_{idx}",
        prompt=(
            f"Traces diverge here between {labels}. Under what condition should "
            "the workflow take each branch? (No condition was detectable from "
            "the demonstrations.)"
        ),
        options=[
            QuestionOption(
                key="halt",
                label="Halt -- the condition is unknown (safe default)",
                effect=OptionEffect.NONE,
            ),
        ],
        default_key="halt",
        consequential=consequential,
        evidence=f"divergent branch between {labels} at column {idx}",
    )
    result.uncertainties.append(
        Uncertainty(
            kind="ambiguous_branch",
            location=f"column {idx}",
            detail=(
                f"traces diverge between {labels} with no detectable "
                "condition -- cannot decide the guard; refusing to guess a "
                "branch on a" + (" consequential" if consequential else "") + " node."
            ),
            consequential=consequential,
            question=question,
            proposal=proposal,
        )
    )
    decisions.append(
        ColumnDecision(
            index=idx,
            kind="divergent",
            align_sig="__divergent__",
            note=f"underdetermined divergence between {labels}",
        )
    )


def _maybe_propose(
    propose: Optional[Proposer], target, kind, context, result
) -> Optional[str]:
    if propose is None:
        return None
    try:
        return propose.propose(target, kind, context)
    except Exception:  # pragma: no cover - advisory path never breaks induction
        return None


def _wire(nodes: list[_Node]):
    """Chain nodes into a single program graph + collect loop-body subflows."""
    if not nodes:
        return None, {}
    states: dict[str, State] = {}
    subflows: dict[str, ProgramGraph] = {}
    end_id = "__end__"
    for i, node in enumerate(nodes):
        states.update(node.states)
        sub = getattr(node, "_subflow", None)
        if sub is not None:
            subflows[sub[0]] = sub[1]
        node.set_next(nodes[i + 1].entry if i + 1 < len(nodes) else end_id)
    states[end_id] = State(id=end_id, kind=StateKind.TERMINAL, outcome="success")
    return ProgramGraph(entry=nodes[0].entry, states=states), subflows


# ===========================================================================
# Held-out validation (RFC §3 [5]) + reproduction scoring
# ===========================================================================


def reproduction_score(result: InductionResult, trace: TraceInput) -> float:
    """Score how well the induced program would REPRODUCE ``trace`` in [0, 1].

    Deterministic and backend-free: it checks each induced column decision
    against the held-out trace's own tokens. A PARAM column reproduces any value
    (the run supplies it); a LITERAL column only reproduces the frozen value
    (so a param wrongly frozen as a constant scores LOW on a trace with a
    different value); LOOP / BRANCH / OPTIONAL columns reproduce whatever count
    / presence the trace shows. Extra trace tokens the program cannot account
    for are penalized. Returns 0.0 for a quarantined (unemitted) program.
    """
    if result.program is None or not result.column_decisions:
        return 0.0
    held = _reduce_trace(_as_workflow(trace).steps)

    col_sigs = [d.align_sig for d in result.column_decisions if d.kind != "divergent"]
    decs = [d for d in result.column_decisions if d.kind != "divergent"]
    held_sigs = [str(tok.align_sig) for tok in held]

    sm = SequenceMatcher(a=col_sigs, b=held_sigs, autojunk=False)
    matched: dict[int, int] = {}  # col index -> held index
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                matched[i1 + k] = j1 + k

    score = 0.0
    for ci, dec in enumerate(decs):
        if ci in matched:
            tok = held[matched[ci]]
            if dec.kind == "literal":
                score += 1.0 if tok.value == dec.literal_value else 0.0
            elif dec.kind == "param":
                score += 1.0  # a param reproduces any observed value
            elif dec.kind == "loop":
                score += 1.0 if tok.kind == "loop" else 0.5
            else:  # branch / optional
                score += 1.0
        else:
            # Column not present in the held trace.
            score += 1.0 if dec.kind in ("branch", "optional") else 0.0

    total = len(decs)
    extra = len(held) - len(matched)
    denom = max(total + max(extra, 0), 1)
    return score / denom


def validate_held_out(
    traces: list[TraceInput], *, propose: Optional[Proposer] = None
) -> HeldOutValidation:
    """Leave-one-out held-out validation (RFC §3 [5]): for each trace, induce a
    program from the OTHER N-1 traces and score whether it reproduces the held
    one. Reports the per-fold scores and their mean. Requires >= 2 traces."""
    n = len(traces)
    if n < 2:
        return HeldOutValidation(per_trace=[], mean=0.0, n_traces=n)
    scores: list[float] = []
    for i in range(n):
        train = traces[:i] + traces[i + 1 :]
        result = induce_program(train, propose=propose)
        scores.append(reproduction_score(result, traces[i]))
    mean = sum(scores) / len(scores) if scores else 0.0
    return HeldOutValidation(per_trace=scores, mean=mean, n_traces=n)

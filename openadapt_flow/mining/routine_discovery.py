"""Robotic Process Mining: discover routines from an unsegmented UI-event log.

Input is a **common event-log format**: a flat, ordered ``list[dict]`` where each
dict is one UI action in the SAME schema the recorder writes to ``events.jsonl``
(``openadapt_flow.recorder``) — ``{"kind": "click"|"double_click"|"type"|"key"|
"scroll"|"wait", ...fields}`` — optionally carrying a ``"t"`` timestamp (seconds)
and a ``"structural"`` locator (``{"selector"|"role"|"name"|"automation_id"}``).
Crucially the log is UNSEGMENTED: it is one continuous stream interleaving many
task instances (possibly across workers/sessions) with noise and unrelated
actions, and nothing marks where one task instance ends and the next begins.

The multi-source adapters in ``openadapt_flow.mining.sources`` produce logs in
exactly this format from non-demo sources (a computer-use agent trajectory, an
RPA / Playwright-codegen script), and ``openadapt_flow.mining.synth`` generates
labelled synthetic logs for evaluation.

Pipeline (grounded in the RPM literature)
-----------------------------------------
1. **Encode** each event as a control-flow *signature* (:func:`action_signature`)
   — a symbol that identifies the KIND of action while abstracting away
   instance-specific data (the typed *value*, the exact pixel coordinates). Two
   instances of the same routine therefore produce the SAME signature sequence
   even though their parameters differ. This is the "action = type + abstracted
   context" representation of Leno et al. (ICPM'20).

2. **Mine frequent control-flow patterns.** Find contiguous signature
   subsequences that recur at least ``min_support`` times (counting
   NON-OVERLAPPING occurrences), via Apriori-style right-extension, then keep the
   MAXIMAL ones (a pattern subsumed by a longer equally-supported pattern is
   dropped). Each maximal frequent pattern is the invariant control-flow skeleton
   of a repeated routine. This is the frequent-pattern core of Leno et al. and
   the "recurring routine" notion of Bosco et al. (BPM'19).

3. **Segment.** Recover task-instance boundaries by claiming each pattern's
   non-overlapping occurrence spans left-to-right, strongest pattern first, so
   every event belongs to at most one routine instance (Agostinelli et al.,
   "Automated Segmentation of UI Logs" — start/end markers recovered from the
   recurring control flow rather than assumed). Events claimed by no routine are
   NOISE.

4. **Emit candidate routines, honestly.** Each discovered routine is a set of
   position-ALIGNED instances (position *k* is the same action signature in every
   instance — the aligned input a downstream inducer wants) plus a ``support``
   count and a heuristic ``confidence``. Patterns BELOW the support/length
   thresholds are NOT promoted to routines; they are reported in
   ``RoutineDiscoveryResult.discarded`` so the pipeline never silently
   over-claims.

Thresholds are parameters with documented defaults. They are the knobs that
**need real-data calibration** — the defaults below are reasonable for the
synthetic harness, NOT validated on production logs:

* ``min_support`` — how many instances make a "routine". Too low → noise
  coincidences promoted; too high → real but rare routines missed.
* ``min_pattern_length`` — shortest control-flow skeleton worth reporting. Too
  low → ubiquitous single actions (a lone click) reported as routines; too high →
  short-but-real routines missed.
* ``spatial_bucket_px`` — pixel grid for abstracting click coordinates when a
  click has NO structural locator. Substrate/DPI dependent.
* ``gap_threshold_s`` — inter-event idle gap treated as a task boundary, used
  only for the temporal-cohesion component of confidence. Highly workflow- and
  operator-dependent.

Model calls: NONE. Discovery is purely structural.

Decoupling from induction
-------------------------
This module's job ENDS at segmented candidate routines. Multi-trace induction
(synthesizing the parameterized program) is built separately and consumes this
output through the thin :class:`SegmentedRoutine` / :class:`RoutineInstanceLike`
protocols only — it must not import the concrete classes' internals.
Reconciliation of the two is done at merge time.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

# A single UI action in the common event-log format (the events.jsonl schema).
Event = Mapping[str, Any]
Signature = str

# Documented default thresholds (see module docstring — these are the
# "needs real-data calibration" knobs).
DEFAULT_MIN_SUPPORT = 2
DEFAULT_MIN_PATTERN_LENGTH = 3
DEFAULT_MAX_PATTERN_LENGTH = 60
DEFAULT_SPATIAL_BUCKET_PX = 64
DEFAULT_GAP_THRESHOLD_S = 30.0


# -- control-flow encoding ---------------------------------------------------


def action_signature(
    event: Event, *, spatial_bucket_px: int = DEFAULT_SPATIAL_BUCKET_PX
) -> Signature:
    """Return the control-flow signature of one event.

    The signature identifies the action's KIND and its target's STABLE identity
    while abstracting instance-specific data, so two instances of the same
    routine share a signature sequence:

    * click / double_click — prefer the structural identity (DOM ``selector`` /
      UIA ``automation_id`` / ``role:name``); fall back to a coarse spatial
      bucket (``spatial_bucket_px`` grid) on pixel-only substrates that recorded
      no structural locator;
    * type — the typed VALUE is abstracted away (it is per-instance data / a
      parameter); the parameter NAME is kept when present (a typed field's
      identity), else a bare ``type``;
    * key — the key name (control flow depends on Enter vs Tab);
    * scroll — the direction only (magnitude is incidental);
    * anything else — the bare kind.
    """
    kind = event.get("kind")
    if kind in ("click", "double_click"):
        struct = event.get("structural") or {}
        ident = struct.get("selector") or struct.get("automation_id")
        if not ident:
            role, name = struct.get("role"), struct.get("name")
            if role or name:
                ident = f"{role or ''}/{name or ''}"
        if ident:
            return f"{kind}@{ident}"
        bucket = max(1, int(spatial_bucket_px))
        bx = int(event.get("x", 0)) // bucket
        by = int(event.get("y", 0)) // bucket
        return f"{kind}#({bx},{by})"
    if kind == "type":
        param = event.get("param")
        return f"type:param={param}" if param else "type"
    if kind == "key":
        return f"key:{event.get('key')}"
    if kind == "scroll":
        dy = int(event.get("dy", 0) or 0)
        direction = "down" if dy > 0 else ("up" if dy < 0 else "none")
        return f"scroll:{direction}"
    return str(kind)


# -- hand-off protocols (the thin contract induction consumes) ---------------


@runtime_checkable
class RoutineInstanceLike(Protocol):
    """One segmented task instance: an ordered slice of the source event log.

    ``events`` are the original event dicts (unmodified) for this instance, in
    order; position *k* corresponds to signature *k* of the routine.
    """

    start_index: int
    end_index: int

    @property
    def events(self) -> Sequence[Event]: ...


@runtime_checkable
class SegmentedRoutine(Protocol):
    """A discovered routine: aligned instances + support + confidence.

    This is the ENTIRE surface a downstream multi-trace inducer needs; it lets
    induction consume discovery output without importing the concrete
    :class:`CandidateRoutine`. ``signature`` is the shared control-flow skeleton
    (one symbol per aligned position); ``instances`` are the aligned trace
    instances; ``support`` is ``len(instances)``.
    """

    signature: tuple[Signature, ...]
    support: int
    confidence: float

    @property
    def instances(self) -> Sequence[RoutineInstanceLike]: ...


# -- concrete result types ---------------------------------------------------


@dataclass(frozen=True)
class RoutineInstance:
    """A single segmented instance of a routine (satisfies RoutineInstanceLike).

    ``events`` is the contiguous slice ``log[start_index:end_index]`` of the
    source log — the aligned control-flow skeleton of this instance. Its length
    equals the routine's signature length, so ``events[k]`` is the concrete event
    that realized ``signature[k]`` in this instance (the alignment a downstream
    inducer relies on).
    """

    start_index: int
    end_index: int  # exclusive
    events: tuple[Event, ...]

    @property
    def signatures(self) -> tuple[Signature, ...]:
        """Per-position signatures of this instance (for auditing alignment)."""
        return tuple(action_signature(e) for e in self.events)

    @property
    def timespan_s(self) -> float | None:
        """Wall-clock duration if the events carry ``t`` timestamps, else None."""
        ts = [e["t"] for e in self.events if "t" in e]
        return (max(ts) - min(ts)) if len(ts) >= 2 else None


@dataclass(frozen=True)
class CandidateRoutine:
    """A discovered routine (satisfies SegmentedRoutine).

    Attributes:
        routine_id: Stable identifier within one discovery run (``routine_0``…).
        signature: The shared control-flow skeleton — one signature per aligned
            position, identical across all instances by construction.
        instances: The aligned, segmented task instances.
        support: Number of instances (``len(instances)``).
        confidence: Heuristic ``[0, 1]`` score blending support, skeleton length,
            and temporal cohesion. Diagnostic ONLY — it is NOT a calibrated
            probability and needs real-data validation (see module docstring).
    """

    routine_id: str
    signature: tuple[Signature, ...]
    instances: tuple[RoutineInstance, ...]
    support: int
    confidence: float

    def __post_init__(self) -> None:
        # Invariant the inducer relies on: every instance is aligned to the
        # signature (same length, same per-position signatures).
        for inst in self.instances:
            if len(inst.events) != len(self.signature):
                raise ValueError(
                    f"{self.routine_id}: instance length {len(inst.events)} != "
                    f"signature length {len(self.signature)} (misaligned)"
                )


@dataclass(frozen=True)
class DiscardedPattern:
    """A recurring pattern that was NOT promoted to a routine, with the reason.

    Reported so discovery never silently over- or under-claims: a reviewer can
    see what was rejected and re-tune thresholds. ``reason`` is one of
    ``"below_support"`` (recurred, but fewer than ``min_support`` times),
    ``"below_min_length"`` (frequent enough, but shorter than
    ``min_pattern_length`` — e.g. a single ubiquitous click), or
    ``"subsumed_by_longer"`` (a sub-pattern of a discovered routine).
    """

    signature: tuple[Signature, ...]
    support: int
    reason: str


@dataclass(frozen=True)
class RoutineDiscoveryResult:
    """The outcome of one discovery run over one unsegmented log."""

    routines: tuple[CandidateRoutine, ...]
    discarded: tuple[DiscardedPattern, ...]
    noise_indices: tuple[int, ...]
    log_length: int
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        """Fraction of log events claimed by a discovered routine instance."""
        if not self.log_length:
            return 0.0
        claimed = self.log_length - len(self.noise_indices)
        return claimed / self.log_length


# -- frequent-pattern mining -------------------------------------------------


def _nonoverlap_starts(starts: Sequence[int], length: int) -> list[int]:
    """Greedy left-to-right non-overlapping occurrence start positions."""
    chosen: list[int] = []
    last_end = -1
    for st in sorted(starts):
        if st > last_end:
            chosen.append(st)
            last_end = st + length - 1
    return chosen


def _frequent_grams(
    sigs: Sequence[Signature], min_support: int, max_length: int
) -> dict[tuple[Signature, ...], list[int]]:
    """All contiguous signature n-grams with raw occurrence count >= min_support.

    Apriori right-extension: a frequent (k+1)-gram's k-gram prefix is frequent,
    so we only extend frequent grams. Raw count is an upper bound on
    non-overlapping support, so pruning on it never discards a gram that could
    clear the final non-overlapping threshold. Returns each gram mapped to ALL
    its raw start positions.
    """
    n = len(sigs)
    level: dict[tuple[Signature, ...], list[int]] = defaultdict(list)
    for i, s in enumerate(sigs):
        level[(s,)].append(i)
    level = {g: p for g, p in level.items() if len(p) >= min_support}
    frequent: dict[tuple[Signature, ...], list[int]] = dict(level)
    while level and (len(next(iter(level))) < max_length):
        nxt: dict[tuple[Signature, ...], list[int]] = defaultdict(list)
        for gram, starts in level.items():
            glen = len(gram)
            for st in starts:
                end = st + glen
                if end < n:
                    nxt[gram + (sigs[end],)].append(st)
        level = {g: p for g, p in nxt.items() if len(p) >= min_support}
        frequent.update(level)
    return frequent


def _is_contiguous_subsequence(
    small: tuple[Signature, ...], big: tuple[Signature, ...]
) -> bool:
    """True if ``small`` occurs as a contiguous run inside ``big``."""
    if len(small) >= len(big):
        return False
    for i in range(len(big) - len(small) + 1):
        if big[i : i + len(small)] == small:
            return True
    return False


# -- public entry point ------------------------------------------------------


def discover_routines(
    log: Sequence[Event],
    *,
    min_support: int = DEFAULT_MIN_SUPPORT,
    min_pattern_length: int = DEFAULT_MIN_PATTERN_LENGTH,
    max_pattern_length: int = DEFAULT_MAX_PATTERN_LENGTH,
    spatial_bucket_px: int = DEFAULT_SPATIAL_BUCKET_PX,
    gap_threshold_s: float = DEFAULT_GAP_THRESHOLD_S,
) -> RoutineDiscoveryResult:
    """Discover candidate routines from one unsegmented UI-event log.

    See the module docstring for the pipeline and the meaning/calibration of each
    threshold. No model calls; purely structural.

    Args:
        log: The unsegmented event log (common event-log format; see module
            docstring). Order is significant; each event is a mapping.
        min_support: Minimum NON-OVERLAPPING occurrences for a pattern to be a
            routine.
        min_pattern_length: Minimum control-flow skeleton length (in actions).
        max_pattern_length: Upper bound on mined skeleton length (bounds work).
        spatial_bucket_px: Pixel grid for abstracting structural-less clicks.
        gap_threshold_s: Inter-event idle gap counted as a boundary, for the
            temporal-cohesion component of confidence only.

    Returns:
        A :class:`RoutineDiscoveryResult`: the discovered routines (each a set of
        aligned instances + support + confidence), the discarded sub-threshold
        patterns (with reasons), and the indices of noise events claimed by no
        routine.
    """
    if min_support < 1:
        raise ValueError("min_support must be >= 1")
    if min_pattern_length < 1:
        raise ValueError("min_pattern_length must be >= 1")

    n = len(log)
    sigs = [
        action_signature(e, spatial_bucket_px=spatial_bucket_px) for e in log
    ]
    frequent = _frequent_grams(sigs, min_support, max_pattern_length)

    # Non-overlapping support per gram; keep those clearing min_support.
    supported: dict[tuple[Signature, ...], int] = {}
    for gram, starts in frequent.items():
        support = len(_nonoverlap_starts(starts, len(gram)))
        if support >= min_support:
            supported[gram] = support

    # Maximality: drop a pattern subsumed by a longer pattern of >= support
    # (its sub-patterns add no information — the longer routine covers them).
    grams = sorted(supported, key=lambda g: (-len(g), g))
    maximal: list[tuple[Signature, ...]] = []
    subsumed: list[tuple[Signature, ...]] = []
    for gram in grams:
        dominated = any(
            _is_contiguous_subsequence(gram, other)
            and supported[other] >= supported[gram]
            for other in supported
            if other != gram
        )
        (subsumed if dominated else maximal).append(gram)

    # Length gate: maximal patterns long enough are routine candidates; the rest
    # are recorded as discarded (honest reporting, no silent drop).
    discarded: list[DiscardedPattern] = []
    routine_candidates: list[tuple[Signature, ...]] = []
    for gram in maximal:
        if len(gram) >= min_pattern_length:
            routine_candidates.append(gram)
        else:
            discarded.append(
                DiscardedPattern(gram, supported[gram], "below_min_length")
            )
    for gram in subsumed:
        discarded.append(
            DiscardedPattern(gram, supported[gram], "subsumed_by_longer")
        )

    # Global segmentation: claim each candidate's non-overlapping spans, strongest
    # first, so no event is double-counted. Strength = longer skeleton, then more
    # support (a longer routine is a more specific explanation of the stream).
    routine_candidates.sort(key=lambda g: (-len(g), -supported[g], g))
    claimed = [False] * n
    routines: list[CandidateRoutine] = []
    next_id = 0
    for gram in routine_candidates:
        glen = len(gram)
        instances: list[RoutineInstance] = []
        for st in sorted(frequent[gram]):
            span = range(st, st + glen)
            if all(not claimed[j] for j in span):
                for j in span:
                    claimed[j] = True
                instances.append(
                    RoutineInstance(
                        start_index=st,
                        end_index=st + glen,
                        events=tuple(log[st : st + glen]),
                    )
                )
        if len(instances) >= min_support:
            routines.append(
                CandidateRoutine(
                    routine_id=f"routine_{next_id}",
                    signature=gram,
                    instances=tuple(instances),
                    support=len(instances),
                    confidence=_confidence(gram, instances, gap_threshold_s),
                )
            )
            next_id += 1
        else:
            # Segmentation stole overlapping events; no longer frequent alone.
            discarded.append(
                DiscardedPattern(
                    gram, len(instances), "below_support_after_segmentation"
                )
            )

    # Below-support recurring grams (raw count clears the Apriori floor but
    # non-overlapping support does not) — reported so a reviewer can lower the
    # threshold knowingly.
    for gram, starts in frequent.items():
        if gram not in supported and len(gram) >= min_pattern_length:
            support = len(_nonoverlap_starts(starts, len(gram)))
            discarded.append(DiscardedPattern(gram, support, "below_support"))

    noise_indices = tuple(i for i in range(n) if not claimed[i])
    routines.sort(key=lambda r: (-r.support, -len(r.signature), r.routine_id))

    return RoutineDiscoveryResult(
        routines=tuple(routines),
        discarded=tuple(discarded),
        noise_indices=noise_indices,
        log_length=n,
        params={
            "min_support": min_support,
            "min_pattern_length": min_pattern_length,
            "max_pattern_length": max_pattern_length,
            "spatial_bucket_px": spatial_bucket_px,
            "gap_threshold_s": gap_threshold_s,
        },
    )


def _confidence(
    signature: tuple[Signature, ...],
    instances: Sequence[RoutineInstance],
    gap_threshold_s: float,
) -> float:
    """Heuristic confidence in ``[0, 1]`` — diagnostic, NOT calibrated.

    Blends three signals, each in ``[0, 1]``:

    * support — more instances → more confidence (``1 - 1/(1+support)``);
    * length — a longer control-flow skeleton is a more specific, less
      coincidental routine (``1 - 1/(1+length)``);
    * temporal cohesion — the fraction of instances whose internal inter-event
      gaps all stay below ``gap_threshold_s`` (a real task instance runs as a
      burst, not spread across idle gaps). ``1.0`` when events carry no ``t``.

    These weights and the whole score need real-data calibration; today it only
    RANKS candidates within a run.
    """
    support = len(instances)
    support_c = 1.0 - 1.0 / (1.0 + support)
    length_c = 1.0 - 1.0 / (1.0 + len(signature))

    cohesive = 0
    timed = 0
    for inst in instances:
        ts = [e["t"] for e in inst.events if "t" in e]
        if len(ts) < 2:
            continue
        timed += 1
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        if all(g < gap_threshold_s for g in gaps):
            cohesive += 1
    cohesion_c = 1.0 if timed == 0 else cohesive / timed

    return round((support_c + length_c + cohesion_c) / 3.0, 3)

"""Synthetic unsegmented-log generator — the RPM evaluation harness.

Robotic Process Mining is evaluated on SYNTHETIC logs first (the standard
process-mining method): only when the ground-truth segmentation is KNOWN can you
measure whether discovery recovered the right routines and rejected the noise.

This module interleaves instances of a few MockMed-style tasks — with the
variation real logs exhibit (per-instance PARAMETER values, variable WORKLIST
lengths, an OPTIONAL dialog) — and injects NOISE (unrelated actions) between
instances, producing one continuous, unsegmented ``list[dict]`` in the common
event-log format plus the ground-truth boundaries.

The two tasks are deliberately shaped so their INVARIANT control-flow skeleton is
a solid contiguous block (the part every instance shares), while the variable
parts (a changing note value, a survey modal present only sometimes, a
variable-length worklist of per-row-distinct actions) sit at the edges. That is
exactly what real routines look like — a fixed core with variable trimmings — and
it is what lets discovery recover the core across all instances while correctly
leaving the optional/variable actions unclustered. Recovering the loop OVER the
worklist (generalizing per-row actions) is the downstream INDUCTION step, not
discovery's job.

No model calls; deterministic given a seed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

Event = dict[str, Any]


@dataclass(frozen=True)
class GroundTruthInstance:
    """A known task instance in a synthetic log (for evaluating discovery).

    ``core_start``/``core_end`` bound the INVARIANT control-flow skeleton — the
    contiguous block every instance of ``task`` shares and that discovery is
    expected to recover. ``start``/``end`` bound the full instance including its
    variable/optional trimmings.
    """

    task: str
    start: int  # inclusive, into the full log
    end: int  # exclusive
    core_start: int  # inclusive
    core_end: int  # exclusive


@dataclass(frozen=True)
class SyntheticLog:
    """A generated unsegmented log plus its ground truth."""

    events: list[Event]
    instances: list[GroundTruthInstance]
    noise_indices: list[int] = field(default_factory=list)

    def core_signatures(self, task: str) -> tuple[str, ...]:
        """The invariant core signatures of the first instance of ``task``.

        Imported lazily so this module has no import-time dependency on the
        discovery module (keeps the harness standalone).
        """
        from openadapt_flow.mining.routine_discovery import action_signature

        for inst in self.instances:
            if inst.task == task:
                core = self.events[inst.core_start : inst.core_end]
                return tuple(action_signature(e) for e in core)
        raise KeyError(f"no instance of task {task!r}")


def _click(selector: str) -> Event:
    return {"kind": "click", "structural": {"selector": selector}}


def _type(text: str, param: str | None = None) -> Event:
    event: Event = {"kind": "type", "text": text}
    if param:
        event["param"] = param
    return event


def _key(key: str) -> Event:
    return {"kind": "key", "key": key}


def task_a_add_note(
    *, note: str, patient_user: str, include_survey: bool
) -> tuple[list[Event], int]:
    """Task A "add-encounter-note": login -> open -> encounter -> note -> save.

    The invariant CORE is the whole login->save skeleton (identical control flow
    every time; only the typed values differ, and those are abstracted by the
    signature). An OPTIONAL survey modal dismissal is appended in some instances —
    a trailing optional step that is genuinely NOT part of the invariant routine.

    Returns:
        ``(events, core_len)`` — ``events[:core_len]`` is the invariant core.
    """
    core: list[Event] = [
        _click("#username"),
        _type(patient_user, param="operator"),
        _click("#password"),
        _type("mockmed-demo-pass"),
        _click("#signin"),
        _click(".open-btn"),
        _click("#new-encounter"),
        _click("#type-triage"),
        _click("#note"),
        _type(note, param="note"),
        _click("#save-encounter"),
    ]
    events = list(core)
    if include_survey:  # optional post-save dialog (present only sometimes)
        events.append(_click("#dismiss-survey"))
    return events, len(core)


def task_b_review_worklist(
    *, query: str, worklist_len: int
) -> tuple[list[Event], int]:
    """Task B "review-worklist": open worklist -> filter -> iterate N rows.

    The invariant CORE is open-worklist -> filter -> type query -> Enter (fixed
    control flow every time). The worklist ITERATION that follows has a variable
    length AND uses a distinct selector per row (``.task-row-0`` /
    ``#mark-done-0`` / ``.task-row-1`` / …). Per-row-distinct actions do not
    recur, so discovery correctly leaves them out of the invariant routine;
    generalizing them into a loop over the worklist is the downstream induction
    step, not discovery's job.

    Returns:
        ``(events, core_len)`` — ``events[:core_len]`` is the invariant core.
    """
    core: list[Event] = [
        _click("#worklist-tab"),
        _click("#filter-open"),
        _type(query, param="filter"),
        _key("Enter"),
    ]
    events = list(core)
    for row in range(worklist_len):
        events.append(_click(f".task-row-{row}"))
        events.append(_click(f"#mark-done-{row}"))
    return events, len(core)


def _noise(rng: random.Random, k: int) -> list[Event]:
    """`k` unrelated actions with non-recurring signatures (background noise).

    Each uses a distinct selector / random coordinate bucket so no two noise
    actions share a signature — noise must not accidentally form a routine.
    """
    events: list[Event] = []
    for _ in range(k):
        roll = rng.random()
        if roll < 0.5:
            events.append(_click(f"#noise-{rng.randrange(10_000)}"))
        elif roll < 0.75:
            events.append(
                {
                    "kind": "click",
                    "x": rng.randrange(2000),
                    "y": rng.randrange(2000),
                }
            )
        elif roll < 0.9:
            events.append(
                {"kind": "scroll", "dx": 0, "dy": rng.choice((-300, 300))}
            )
        else:
            events.append(_type(f"stray-{rng.randrange(10_000)}"))
    return events


def generate_log(
    *,
    n_a: int = 3,
    n_b: int = 2,
    noise_between: int = 2,
    seed: int = 0,
    a_survey_every: int = 3,
    b_worklist_lens: tuple[int, ...] = (1, 1),
    gap_between_s: float = 60.0,
    step_s: float = 0.7,
) -> SyntheticLog:
    """Generate one unsegmented log interleaving task A and task B with noise.

    Args:
        n_a: Instances of task A (add-note).
        n_b: Instances of task B (review-worklist).
        noise_between: Unrelated actions injected between consecutive instances.
        seed: RNG seed (deterministic output).
        a_survey_every: Every k-th A instance appends the optional survey modal
            dismissal (set huge to disable; ``1`` for always).
        b_worklist_lens: Worklist length for the i-th B instance (cycled).
        gap_between_s: Idle-time gap (seconds) inserted between instances — the
            temporal signal a boundary-aware segmenter can use.
        step_s: Wall-clock spacing between consecutive events within an instance.

    Returns:
        A :class:`SyntheticLog` with the flat event log, per-instance ground
        truth, and the set of noise indices.
    """
    rng = random.Random(seed)

    # Build the instances (payload + core length), then interleave A/B round-robin
    # so the stream genuinely mixes tasks rather than grouping them.
    pending: list[tuple[str, list[Event], int]] = []
    a_left, b_left = n_a, n_b
    a_i = b_i = 0
    take_a = True
    while a_left or b_left:
        if take_a and a_left:
            events, core_len = task_a_add_note(
                note=f"Follow-up in {a_i + 1} weeks",
                patient_user=f"nurse.{a_i}",
                include_survey=((a_i + 1) % a_survey_every == 0),
            )
            pending.append(("A", events, core_len))
            a_left -= 1
            a_i += 1
        elif b_left:
            wl = b_worklist_lens[b_i % len(b_worklist_lens)] if b_worklist_lens else 0
            events, core_len = task_b_review_worklist(
                query=f"open-{b_i}", worklist_len=wl
            )
            pending.append(("B", events, core_len))
            b_left -= 1
            b_i += 1
        take_a = not take_a

    log: list[Event] = []
    instances: list[GroundTruthInstance] = []
    noise_indices: list[int] = []
    clock = 0.0

    def emit(event: Event, *, is_noise: bool) -> None:
        nonlocal clock
        event = dict(event)
        event["t"] = round(clock, 3)
        clock += step_s
        if is_noise:
            noise_indices.append(len(log))
        log.append(event)

    for pos, (task, events, core_len) in enumerate(pending):
        if pos > 0:
            clock += gap_between_s  # idle gap between task instances
            for noise_event in _noise(rng, noise_between):
                emit(noise_event, is_noise=True)
            clock += gap_between_s
        start = len(log)
        for event in events:
            emit(event, is_noise=False)
        instances.append(
            GroundTruthInstance(
                task=task,
                start=start,
                end=len(log),
                core_start=start,
                core_end=start + core_len,
            )
        )

    return SyntheticLog(
        events=log, instances=instances, noise_indices=noise_indices
    )

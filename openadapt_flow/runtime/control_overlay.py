"""Exact, presentation-only runtime events for out-of-process overlays.

The target application's DOM/accessibility tree is not an overlay surface.
This module emits the canonical, PHI-safe ``openadapt-types`` contract for a
native Desktop overlay, a sibling hosted-stream layer, or deterministic media
composition.  It carries no selectors, target text, URLs, screenshots, report
bodies, or execution authority.

``openadapt-types`` remains an optional interoperability dependency.  Import
this module through the ``interop`` extra when an overlay consumer is present;
ordinary Flow replay does not require it.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from types import MappingProxyType
from typing import Literal, TypeAlias

from openadapt_types import (
    ControlOverlayDataClassification,
    ControlOverlayFrameV1,
    ControlOverlayMode,
    ControlOverlayPhase,
    ControlOverlayProfile,
    ControlOverlayTimelineEventV1,
    ControlOverlayTimelineV1,
    build_control_overlay_timeline,
)

ExecutionOutcome: TypeAlias = Literal[
    "VERIFIED",
    "COMPLETED_UNVERIFIED",
    "HALTED",
    "FAILED",
    "ROLLED_BACK",
]
OverlayFrameSink: TypeAlias = Callable[[ControlOverlayFrameV1], None]
MillisecondsClock: TypeAlias = Callable[[], int | float]

_TERMINAL_PHASE_BY_OUTCOME = MappingProxyType(
    {
        "VERIFIED": ControlOverlayPhase.VERIFIED,
        "COMPLETED_UNVERIFIED": ControlOverlayPhase.COMPLETED_UNVERIFIED,
        "HALTED": ControlOverlayPhase.HALTED,
        "FAILED": ControlOverlayPhase.FAILED,
        "ROLLED_BACK": ControlOverlayPhase.ROLLED_BACK,
    }
)


def _unix_ms() -> int:
    return time.time_ns() // 1_000_000


def _monotonic_ms() -> float:
    return time.monotonic_ns() / 1_000_000.0


class RuntimeControlOverlayEmitter:
    """Emit one exact canonical event stream without controlling execution."""

    def __init__(
        self,
        sink: OverlayFrameSink,
        *,
        mode: ControlOverlayMode = ControlOverlayMode.REPLAY,
        unix_ms_clock: MillisecondsClock = _unix_ms,
        monotonic_ms_clock: MillisecondsClock = _monotonic_ms,
    ) -> None:
        self._sink = sink
        self._mode = ControlOverlayMode(mode)
        self._unix_ms_clock = unix_ms_clock
        self._monotonic_ms_clock = monotonic_ms_clock
        self._profile: ControlOverlayProfile | None = None
        self._next_sequence = 0
        self._last_monotonic_ms: float | None = None
        self._terminal = False

    @property
    def mode(self) -> ControlOverlayMode:
        return self._mode

    def begin(self, *, profile: ControlOverlayProfile | str) -> None:
        """Begin a fresh run and bind its exact named execution profile."""

        self._profile = ControlOverlayProfile(profile)
        self._next_sequence = 0
        self._last_monotonic_ms = None
        self._terminal = False

    def emit_phase(
        self,
        phase: ControlOverlayPhase | str,
        *,
        current_step: int | None = None,
        total_steps: int | None = None,
    ) -> ControlOverlayFrameV1:
        """Emit an exact runtime phase; all presentation strings are canonical."""

        if self._profile is None:
            raise RuntimeError("control-overlay run has not begun")
        if self._terminal:
            raise RuntimeError("control-overlay run already emitted a terminal outcome")
        canonical_phase = ControlOverlayPhase(phase)
        if canonical_phase in {
            ControlOverlayPhase.VERIFIED,
            ControlOverlayPhase.COMPLETED_UNVERIFIED,
            ControlOverlayPhase.HALTED,
            ControlOverlayPhase.FAILED,
            ControlOverlayPhase.ROLLED_BACK,
        }:
            raise ValueError("terminal phases must be emitted from execution_outcome")

        return self._emit(
            canonical_phase,
            current_step=current_step,
            total_steps=total_steps,
            terminal=False,
        )

    def emit_terminal(self, outcome: ExecutionOutcome | str) -> ControlOverlayFrameV1:
        """Emit only an exact evidence-qualified ``RunReport.execution_outcome``.

        Generic success booleans and legacy strings such as ``SUCCESS`` are
        deliberately not accepted.  The producer never upgrades an outcome.
        """

        try:
            phase = _TERMINAL_PHASE_BY_OUTCOME[outcome]
        except KeyError as exc:
            raise ValueError(
                "terminal overlay requires an exact execution_outcome"
            ) from exc
        return self._emit(
            phase,
            current_step=None,
            total_steps=None,
            terminal=True,
        )

    def _emit(
        self,
        phase: ControlOverlayPhase,
        *,
        current_step: int | None,
        total_steps: int | None,
        terminal: bool,
    ) -> ControlOverlayFrameV1:
        if self._profile is None:
            raise RuntimeError("control-overlay run has not begun")
        if self._terminal:
            raise RuntimeError("control-overlay run already emitted a terminal outcome")
        observed_at_unix_ms = int(self._unix_ms_clock())
        observed_at_monotonic_ms = float(self._monotonic_ms_clock())
        if not math.isfinite(observed_at_monotonic_ms):
            raise ValueError("control-overlay monotonic clock must be finite")
        if (
            self._last_monotonic_ms is not None
            and observed_at_monotonic_ms < self._last_monotonic_ms
        ):
            raise ValueError("control-overlay monotonic clock moved backwards")

        frame = ControlOverlayFrameV1.build(
            event_sequence=self._next_sequence,
            observed_at_unix_ms=observed_at_unix_ms,
            observed_at_monotonic_ms=observed_at_monotonic_ms,
            visible=True,
            phase=phase,
            mode=self._mode,
            profile=self._profile,
            current_step=current_step,
            total_steps=total_steps,
            pause=False,
            resume=False,
            stop=False,
        )
        # Advance before delivery: a sink that receives the frame and then
        # raises cannot cause a sequence number or terminal event to be reused.
        self._next_sequence += 1
        self._last_monotonic_ms = observed_at_monotonic_ms
        if terminal:
            self._terminal = True
        self._sink(frame)
        return frame


def build_runtime_control_overlay_timeline_v1(
    frames: Sequence[ControlOverlayFrameV1],
    *,
    data_classification: ControlOverlayDataClassification,
    evidence_pack_id: str,
    media_sha256: str,
    duration_ms: int,
    media_started_monotonic_ms: float,
) -> ControlOverlayTimelineV1:
    """Bind retained runtime frames to exact immutable-media timing.

    The caller must retain the media start on the same monotonic clock as the
    frames.  Offsets are the exact monotonic deltas rounded half-up to the
    nearest millisecond.  The first frame must coincide with media start;
    duplicate rounded offsets, reconstruction from elapsed duration, and
    out-of-clip events are refused rather than adjusted.
    """

    if not frames:
        raise ValueError("control-overlay timeline requires at least one frame")
    if not math.isfinite(media_started_monotonic_ms) or (
        media_started_monotonic_ms < 0
    ):
        raise ValueError("media_started_monotonic_ms must be finite and non-negative")
    if frames[0].observed_at_monotonic_ms != media_started_monotonic_ms:
        raise ValueError("first overlay frame must exactly match media start")

    events: list[ControlOverlayTimelineEventV1] = []
    previous_offset = -1
    for frame in frames:
        delta = frame.observed_at_monotonic_ms - media_started_monotonic_ms
        if delta < 0 or not math.isfinite(delta):
            raise ValueError("overlay frame precedes media start")
        offset = int(math.floor(delta + 0.5))
        if offset <= previous_offset:
            raise ValueError("overlay media offsets must be strictly increasing")
        if offset > duration_ms:
            raise ValueError("overlay frame falls outside media duration")
        events.append(ControlOverlayTimelineEventV1(at_ms=offset, frame=frame))
        previous_offset = offset

    return build_control_overlay_timeline(
        data_classification=data_classification,
        evidence_pack_id=evidence_pack_id,
        media_sha256=media_sha256,
        duration_ms=duration_ms,
        events=events,
    )

"""Privacy-safe local metrics for attended resolution and Teach outcomes.

Each event is a new, immutable JSON file under the run directory.  The schema
contains only closed enums, booleans, bounded durations, digests, and an RFC
3339 timestamp.  It cannot represent a workflow name, operator identity,
filesystem path, screenshot, OCR text, parameter, or free-form reason.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.console import data
from openadapt_flow.repair.teach import TeachRepairResult
from openadapt_flow.runtime.durable.attended import AttendedDecision

_EVENTS_DIR = "resolution-metrics"
_CATEGORY = Literal[
    "effect_refuted",
    "effect_indeterminate",
    "effect_escalated",
    "placeholder_effect",
    "effect_unverifiable",
    "unmet_guard",
    "disambiguation",
    "identity",
    "postcondition",
    "resolution",
    "human_required",
    "halt",
    "operator_review",
]


class ResolutionMetricEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.resolution-metric/v1"] = (
        "openadapt.resolution-metric/v1"
    )
    event_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_kind: Literal["decision", "teach"]
    category: _CATEGORY
    action: Literal["continue", "skip", "reject", "teach", "escalate"]
    terminal_resolution: bool = False
    resolution_latency_s: Optional[float] = Field(default=None, ge=0, le=604_800)
    teach_outcome: Literal["none", "candidate", "banked", "refused"] = "none"
    candidate_state: Optional[Literal["candidate", "rejected"]] = None
    candidate_record_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    policy_passed: Optional[bool] = None
    qualification_passed: Optional[bool] = None
    consequential: bool = False
    emitted_at: datetime


class ResolutionMetricSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.resolution-metric-summary/v1"] = (
        "openadapt.resolution-metric-summary/v1"
    )
    resolved_count: int = Field(ge=0)
    median_time_to_resolve_s: Optional[float] = Field(default=None, ge=0)
    teach_attempt_count: int = Field(ge=0)
    teach_candidate_count: int = Field(ge=0)
    teach_acceptance_rate: Optional[float] = Field(default=None, ge=0, le=1)


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _latency(started_at: Optional[str], ended_at: str) -> Optional[float]:
    start = _parse_time(started_at)
    end = _parse_time(ended_at)
    if start is None or end is None or end < start:
        return None
    return round(min((end - start).total_seconds(), 604_800.0), 3)


def _write_event(run_dir: Path, event: ResolutionMetricEvent) -> None:
    directory = run_dir / _EVENTS_DIR
    if directory.is_symlink():
        raise ValueError("the resolution metric directory cannot be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{event.event_id}.json"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(event.model_dump_json(indent=2))
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except FileExistsError:
        existing = ResolutionMetricEvent.model_validate_json(path.read_text())
        if existing != event:
            raise ValueError("the resolution metric id already has different bytes")


def emit_decision_metric(
    run_dir: Path,
    *,
    category: str,
    pause_created_at: Optional[str],
    decision: AttendedDecision,
) -> ResolutionMetricEvent:
    terminal = decision.status in {"completed", "halted", "rejected"}
    emitted_at = _parse_time(decision.created_at)
    if emitted_at is None:
        raise ValueError("the attended decision has no valid event time")
    event_id = hashlib.sha256(
        f"decision:{decision.request_digest}".encode("ascii")
    ).hexdigest()
    event = ResolutionMetricEvent(
        event_id=event_id,
        event_kind="decision",
        category=category,
        action=decision.action,
        terminal_resolution=terminal,
        resolution_latency_s=(
            _latency(pause_created_at, decision.created_at) if terminal else None
        ),
        emitted_at=emitted_at,
    )
    _write_event(run_dir, event)
    return event


def emit_teach_metric(
    run_dir: Path,
    *,
    category: str,
    result: TeachRepairResult,
) -> ResolutionMetricEvent:
    now = datetime.now(timezone.utc)
    event_id = hashlib.sha256(
        f"teach:{result.attempt_digest}".encode("ascii")
    ).hexdigest()
    event = ResolutionMetricEvent(
        event_id=event_id,
        event_kind="teach",
        category=category,
        action="teach",
        teach_outcome=result.outcome,
        candidate_state=result.candidate_state,
        candidate_record_sha256=result.candidate_record_sha256,
        policy_passed=result.policy_passed,
        qualification_passed=result.qualification_passed,
        consequential=result.consequential,
        emitted_at=now,
    )
    _write_event(run_dir, event)
    return event


def resolution_metric_summary(runs_root: Path) -> ResolutionMetricSummary:
    events: list[ResolutionMetricEvent] = []
    for run_dir in data._scan(runs_root, data._is_run_dir):
        directory = run_dir / _EVENTS_DIR
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink():
                continue
            try:
                events.append(
                    ResolutionMetricEvent.model_validate_json(path.read_text())
                )
            except (OSError, ValueError):
                continue
    latencies = [
        event.resolution_latency_s
        for event in events
        if event.terminal_resolution and event.resolution_latency_s is not None
    ]
    teach = [event for event in events if event.teach_outcome != "none"]
    accepted = [event for event in teach if event.teach_outcome == "candidate"]
    return ResolutionMetricSummary(
        resolved_count=sum(event.terminal_resolution for event in events),
        median_time_to_resolve_s=(
            round(float(median(latencies)), 3) if latencies else None
        ),
        teach_attempt_count=len(teach),
        teach_candidate_count=len(accepted),
        teach_acceptance_rate=(round(len(accepted) / len(teach), 4) if teach else None),
    )


__all__ = [
    "ResolutionMetricEvent",
    "ResolutionMetricSummary",
    "emit_decision_metric",
    "emit_teach_metric",
    "resolution_metric_summary",
]

"""Compensation hook -- reconcile-or-escalate for irreversible effects.

The RFC (``docs/design/WORKFLOW_PROGRAM_IR.md`` section 2.4) gives each
consequential state an explicit ``compensation`` (the undo/saga action) and
routes unrecoverable failures to a durable human checkpoint rather than
proceeding. This module is the runtime counterpart for the effect layer: when
an :class:`EffectVerdict` REFUTES a consequential (irreversible) write, we do
NOT silently proceed -- we either *reconcile* (compensate, then re-verify the
effect is now correct) or *escalate* (durably halt for a human).

The canonical case is a detected duplicate: a non-idempotent double-submit
lands two records. A :class:`Compensator` removes the extras and we re-verify;
only if the system of record now shows exactly the intended record do we
report the write reconciled. Faults with no safe automatic undo (a missing /
phantom write, a partial save, a collateral loss, or an INDETERMINATE
unreadable system of record) always ESCALATE -- reconciliation must never
invent state.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    EffectVerifier,
    Verdict,
)


class CompensationOutcome(str, Enum):
    RECONCILED = "reconciled"  # compensated and re-verified CONFIRMED -> proceed
    ESCALATED = "escalated"  # cannot auto-fix -> durable halt for a human
    NOOP = "noop"  # nothing to do (the effect was already CONFIRMED)


class CompensationResult(BaseModel):
    """Outcome of :func:`reconcile_or_escalate`."""

    outcome: CompensationOutcome
    reason: str = ""
    #: Whether the run may proceed. True ONLY when the effect was reconciled
    #: to a CONFIRMED re-verification -- never on escalation.
    proceed: bool = False
    #: Number of compensating actions taken (e.g. extra records deleted).
    actions_taken: int = 0
    #: The verdict AFTER compensation (the original verdict on escalation).
    final_verdict: Optional[EffectVerdict] = None
    #: Human-facing escalation summary for the durable checkpoint.
    escalation: Optional[str] = None


class CompensationAction(BaseModel):
    """What a :class:`Compensator` did, for audit."""

    deleted_records: list[dict[str, Any]] = Field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None


@runtime_checkable
class Compensator(Protocol):
    """Undo the side effect of a consequential write.

    Implementations must be safe to call on the exact records the verdict
    matched and must report failure (``ok=False``) rather than raise, so
    :func:`reconcile_or_escalate` can escalate cleanly on a failed undo.
    """

    def undo(
        self, effect: Effect, verdict: EffectVerdict, context: Any = None
    ) -> CompensationAction:
        """Compensate the effect (e.g. delete duplicate records)."""
        ...


def _is_duplicate(verdict: EffectVerdict) -> bool:
    """A REFUTED record_written whose only fault is too-many matches."""
    return (
        verdict.verdict is Verdict.REFUTED
        and verdict.observed_count is not None
        and verdict.expected_count is not None
        and verdict.observed_count > verdict.expected_count
    )


def reconcile_or_escalate(
    effect: Effect,
    verdict: EffectVerdict,
    *,
    verifier: EffectVerifier,
    before: EffectState,
    compensator: Optional[Compensator] = None,
    context: Any = None,
) -> CompensationResult:
    """Reconcile a REFUTED consequential effect, or escalate for a human.

    Args:
        effect: The effect that was verified.
        verdict: Its (non-CONFIRMED) verdict.
        verifier: The verifier, used to RE-verify after compensation.
        before: The original pre-action snapshot (re-verification baseline).
        compensator: Optional undo action; without one, everything escalates.
        context: Passed through to the verifier / compensator.

    Returns:
        A :class:`CompensationResult`. ``proceed`` is True only when the
        effect was compensated AND re-verified CONFIRMED.
    """
    if verdict.confirmed:
        return CompensationResult(
            outcome=CompensationOutcome.NOOP,
            reason="effect already confirmed; no compensation needed",
            proceed=True,
            final_verdict=verdict,
        )

    # INDETERMINATE (unreadable SoR) or a reversible effect: never auto-fix.
    if verdict.verdict is Verdict.INDETERMINATE:
        return _escalate(
            verdict,
            "system of record is unreadable -- cannot compensate blindly; "
            "durably halt and escalate to a human",
        )
    if effect.risk != "irreversible":
        return _escalate(
            verdict,
            "reversible effect refuted -- halt for review (no compensation "
            "configured for reversible writes)",
        )
    if compensator is None:
        return _escalate(
            verdict,
            "no compensator available for an irreversible refuted effect -- "
            "durably halt and escalate",
        )

    # Only a duplicate has a safe, well-defined automatic undo (remove the
    # extras). Missing / partial / collateral-loss faults cannot be fixed by
    # deleting rows -- inventing or overwriting state would be another wrong
    # action -- so they escalate.
    if not _is_duplicate(verdict):
        return _escalate(
            verdict,
            "refuted effect is not a duplicate (missing / partial / "
            "collateral loss) -- no safe automatic compensation; escalate",
        )

    action = compensator.undo(effect, verdict, context=context)
    if not action.ok:
        return _escalate(
            verdict,
            f"compensation failed ({action.error}) -- escalate",
            actions_taken=len(action.deleted_records),
        )

    reverified = verifier.verify(effect, before, context=context)
    if reverified.confirmed:
        return CompensationResult(
            outcome=CompensationOutcome.RECONCILED,
            reason=(
                f"deleted {len(action.deleted_records)} duplicate record(s); "
                "re-verification against the system of record now confirms "
                "exactly the intended record"
            ),
            proceed=True,
            actions_taken=len(action.deleted_records),
            final_verdict=reverified,
        )
    return _escalate(
        reverified,
        "compensation did not restore the intended state -- escalate",
        actions_taken=len(action.deleted_records),
    )


def _escalate(
    verdict: EffectVerdict, reason: str, *, actions_taken: int = 0
) -> CompensationResult:
    return CompensationResult(
        outcome=CompensationOutcome.ESCALATED,
        reason=reason,
        proceed=False,
        actions_taken=actions_taken,
        final_verdict=verdict,
        escalation=(
            f"[{verdict.substrate}] {verdict.kind.value}: {verdict.reason} -- {reason}"
        ),
    )


class RestCompensator:
    """Compensate a duplicate write on a REST system of record by DELETE-ing
    the extra records, keeping the earliest-id match.

    Args:
        base_url: Base URL of the system of record.
        delete_path_template: Path template with ``{id}`` for the per-record
            DELETE (e.g. ``/api/encounter/{id}``).
        session: Optional ``requests``-style session.
        timeout_s: Per-request timeout.
    """

    def __init__(
        self,
        base_url: str,
        *,
        delete_path_template: str = "/api/encounter/{id}",
        session: Any = None,
        timeout_s: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.delete_path_template = delete_path_template
        self.timeout_s = timeout_s
        self._session = session

    def _get_session(self) -> Any:
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def undo(
        self, effect: Effect, verdict: EffectVerdict, context: Any = None
    ) -> CompensationAction:
        # Keep the earliest record (lowest id); delete the rest.
        records = sorted(verdict.matched_records, key=lambda r: r.get("id", 0))
        extras = records[1:]
        deleted: list[dict[str, Any]] = []
        for rec in extras:
            url = f"{self.base_url}{self.delete_path_template.format(id=rec['id'])}"
            try:
                resp = self._get_session().delete(url, timeout=self.timeout_s)
            except Exception as exc:  # noqa: BLE001
                return CompensationAction(
                    deleted_records=deleted, ok=False, error=str(exc)
                )
            if resp.status_code // 100 != 2:
                return CompensationAction(
                    deleted_records=deleted,
                    ok=False,
                    error=f"DELETE {url} -> {resp.status_code}",
                )
            deleted.append(rec)
        return CompensationAction(deleted_records=deleted, ok=True)

"""Post-run checks R1 and R2, the item decision, and the problem-rate rule.

R1 ``verified``: the transaction outcome is present, is ``VERIFIED``, and
agrees with the execution outcome and the outcome envelope. Every effect was
confirmed on the first read, with no reconciliation, by a system-of-record
read at ``INDEPENDENT_SYSTEM`` from a verifier identity the switch record
names. A ``VERIFIED`` execution outcome with no transaction outcome fails.

R2 ``whole_run_clean``: over the whole run, no model call, no repair, no
drift rescue or drift-oracle call, no ``grounder`` rung, no rung below the
step's floor from passing cases, and no dialog dismissal those cases didn't
use.

The engine's outcome words never change here. An item that fails R2 after
R1 passed stays ``VERIFIED`` ("Done and checked"), still goes to a person, and
turns the switch off with ``screen_changed``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from openadapt_flow.approval_mode.model import (
    CheckOutcome,
    ItemDecisionKind,
    OffReason,
    SwitchThresholds,
)
from openadapt_flow.approval_mode.switch import RUNG_ORDER, SwitchLogEvent

if TYPE_CHECKING:
    from openadapt_flow.approval_mode.switch import SwitchRecord
    from openadapt_flow.ir import RunReport, StepResult

#: Outcomes that may be shown as "nothing was written" (design section 8).
NOTHING_WRITTEN = frozenset(
    {"HALTED_BEFORE_EFFECT", "REJECTED_POLICY", "CANCELED", "FAILED_PLATFORM"}
)
_EXECUTION_FOR_NOTHING_WRITTEN = frozenset({"HALTED", "FAILED"})


def outcome_is_uncertain(report: "RunReport") -> bool:
    """True when the item ended "Check the record", "Finished, not checked",
    with an unknown or inconsistent outcome, or rolled back.

    These switch the mode off with ``uncertain_result``.
    """

    transaction = report.transaction_outcome
    execution = report.execution_outcome
    if transaction is None or execution is None:
        return True
    if transaction == "VERIFIED":
        return not _verified_outcome_consistent(report)
    if transaction in NOTHING_WRITTEN:
        return execution not in _EXECUTION_FOR_NOTHING_WRITTEN
    # RECONCILIATION_REQUIRED, COMPLETED_UNVERIFIED, ROLLED_BACK.
    return True


def _verified_outcome_consistent(report: "RunReport") -> bool:
    envelope = report.outcome_envelope
    return (
        report.transaction_outcome == "VERIFIED"
        and report.execution_outcome == "VERIFIED"
        and envelope is not None
        and envelope.outcome == "VERIFIED"
        and report.success is True
        and report.production_eligible is True
    )


def _consequential(result: "StepResult") -> bool:
    return (
        result.risk == "irreversible"
        or result.risk_review_required
        or bool(result.effect_contract_hashes)
        or bool(result.effect_evidence)
        or result.effect_verified is not None
        or result.effect_approved_unverified
        or result.delivery_uncertainty is not None
    )


def check_verified(
    report: "RunReport",
    record: "SwitchRecord",
    *,
    saves_without_person: int,
) -> CheckOutcome:
    """R1: a first-read system-of-record confirmation of every save."""

    def fail(detail: str) -> CheckOutcome:
        return CheckOutcome(code="verified", passed=False, detail=detail)

    if report.transaction_outcome is None:
        return fail("transaction_outcome_missing")
    if report.transaction_outcome != "VERIFIED":
        return fail("not_verified")
    if not _verified_outcome_consistent(report):
        return fail("outcome_inconsistent")
    if report.execution_profile != record.profile:
        return fail("profile_mismatch")
    if report.bundle_content_digest != record.bundle_content_digest:
        return fail("bundle_mismatch")
    if saves_without_person < 1:
        return fail("no_save_checked")
    identities = set(record.verifier_identities)
    evidence_count = 0
    for result in report.results:
        if result.effect_approved_unverified:
            return fail("approved_unverified_write")
        if not _consequential(result) or result.skipped:
            continue
        if result.delivery_uncertainty is not None:
            return fail("delivery_uncertain")
        if not result.effect_evidence:
            return fail("save_without_read_back")
        declared = Counter(result.effect_contract_hashes)
        observed = Counter(item.effect_contract_hash for item in result.effect_evidence)
        if not declared or declared != observed:
            return fail("read_back_does_not_cover_save")
        for item in result.effect_evidence:
            evidence_count += 1
            if (
                item.initial_verdict != "confirmed"
                or item.final_verdict != "confirmed"
                or item.observed_effect != "present"
            ):
                return fail("read_back_not_confirmed_first_time")
            if item.reconciliation_completed or item.reconciliation_actions:
                return fail("read_back_reconciled")
            if item.verification_tier != 1:
                return fail("read_back_not_system_of_record")
            if item.verifier_identity is None:
                return fail("verifier_identity_missing")
            if item.verifier_identity not in identities:
                return fail("verifier_identity_not_bound")
    if evidence_count == 0:
        return fail("save_without_read_back")
    return CheckOutcome(code="verified", passed=True)


def step_screen_problem(
    result: "StepResult",
    record: "SwitchRecord",
) -> Optional[str]:
    """The first reason this step's screen handling differs from passing cases.

    Shared by the pre-save check P4 (for steps so far) and R2 (whole run).
    """

    if result.heal is not None:
        return "repair_applied"
    if result.postcondition_drift_rescues or result.drift_oracle_calls:
        return "drift_rescue"
    for resolution in (result.resolution, result.drag_end_resolution):
        if resolution is None:
            continue
        if resolution.rung == "grounder":
            return "grounder_rung"
        floor = record.rung_floor.get(result.step_id)
        if floor is None:
            return "rung_not_seen_in_passing_cases"
        if RUNG_ORDER.index(resolution.rung) > RUNG_ORDER.index(floor):
            return "rung_below_floor"
    allowed = set(record.allowed_interstitials.get(result.step_id, ()))
    for action in result.interstitial_actions:
        if action.interstitial not in allowed:
            return "new_dialog_dismissal"
    return None


def check_whole_run_clean(report: "RunReport", record: "SwitchRecord") -> CheckOutcome:
    """R2: the whole run used no repair, model call or rescue."""

    def fail(detail: str, step_id: Optional[str] = None) -> CheckOutcome:
        return CheckOutcome(
            code="whole_run_clean", passed=False, detail=detail, step_id=step_id
        )

    if report.model_calls:
        return fail("model_call")
    if report.heal_count:
        return fail("repair_applied")
    if report.rung_counts.get("grounder"):
        return fail("grounder_rung")
    if report.screenshots_may_leave_box:
        return fail("model_egress_possible")
    for result in report.results:
        problem = step_screen_problem(result, record)
        if problem is not None:
            return fail(problem, result.step_id)
    return CheckOutcome(code="whole_run_clean", passed=True)


@dataclass(frozen=True)
class ItemDecision:
    """The decision for one finished item, before it is logged."""

    kind: ItemDecisionKind
    reason: Optional[str]
    needs_person: bool
    counts_toward_problems: bool
    switch_off: Optional[OffReason]


def decide_problems_only(
    report: "RunReport",
    *,
    r1: CheckOutcome,
    r2: CheckOutcome,
    gate_decision: Optional[ItemDecision],
) -> ItemDecision:
    """Combine the gate's pre-save decision with R1, R2 and the outcome."""

    uncertain = outcome_is_uncertain(report)
    if gate_decision is not None:
        # The gate sent the item to a person before a save. An earlier save in
        # the same item may already have landed; Flow's outcome rules decide
        # what the customer reads, and an uncertain result still switches off.
        off = gate_decision.switch_off
        if off is None and uncertain:
            off = OffReason.UNCERTAIN_RESULT
        return ItemDecision(
            kind=gate_decision.kind,
            reason=gate_decision.reason,
            needs_person=True,
            counts_toward_problems=gate_decision.counts_toward_problems
            or off is OffReason.UNCERTAIN_RESULT,
            switch_off=off,
        )
    if r1.passed and r2.passed:
        return ItemDecision(
            kind=ItemDecisionKind.NO_PERSON,
            reason=None,
            needs_person=False,
            counts_toward_problems=False,
            switch_off=None,
        )
    if r1.passed:
        # "Saved and checked, but OpenAdapt had to work around a changed
        # screen." The outcome word stays VERIFIED.
        return ItemDecision(
            kind=ItemDecisionKind.PROBLEM,
            reason=OffReason.SCREEN_CHANGED.value,
            needs_person=True,
            counts_toward_problems=True,
            switch_off=OffReason.SCREEN_CHANGED,
        )
    reason = (report.transaction_outcome or "unknown_outcome").lower()
    return ItemDecision(
        kind=ItemDecisionKind.PROBLEM,
        reason=reason,
        needs_person=True,
        counts_toward_problems=True,
        switch_off=OffReason.UNCERTAIN_RESULT if uncertain else None,
    )


def too_many_problems(
    finished: list[SwitchLogEvent],
    thresholds: SwitchThresholds,
) -> bool:
    """3 counted problems in a row, or 5 within the last 20 items.

    ``finished`` holds the latest ``item_finished`` event per item, in order.
    Items sent to approval by the item checks or by a stale status are
    recorded with ``counts_toward_problems=False``; they neither count nor
    break a run of problems.
    """

    counted = [
        event
        for event in finished
        if event.counts_toward_problems or event.decision is ItemDecisionKind.NO_PERSON
    ]
    streak = 0
    for event in reversed(counted):
        if event.decision is ItemDecisionKind.NO_PERSON:
            break
        streak += 1
    if streak >= thresholds.problems_in_a_row:
        return True
    window = counted[-thresholds.problems_window :]
    problems = sum(
        1 for event in window if event.decision is not ItemDecisionKind.NO_PERSON
    )
    return problems >= thresholds.problems_in_window

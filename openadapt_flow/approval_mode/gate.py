"""The pre-save gate (P1 to P5) and the per-item finish (R1, R2, switch-off).

:class:`ApprovalGate` is the runtime object a caller hands to the
:class:`~openadapt_flow.runtime.replayer.Replayer`. The replayer asks it
before every consequential save, at the last point before input delivery and
after the target and the right-record check have resolved, and again when the
run's outcome is stamped. One gate instance serves one item (one run).

Modes:

- ``not_gated``: every save proceeds, and the run report says so.
- ``each_item``: every save stops before delivery and the item goes to a
  person for approval. The approve-and-save path is phase 0 work; this gate
  only guarantees that nothing is saved without one.
- ``problems_only``: a save proceeds without a person only when the item
  checks (design 7.2) and P1 to P5 (design 7.3) all pass. Any failure stops
  before the save and sends the item to a person: per-item approval for an
  item check, a switch problem or a changed screen, or "stop before saving"
  for the signed-in user, the right record or the record check. After the
  run, R1 and R2 decide whether the item counts as done without a person; any
  other result goes to a person, and the switch-off rules in design 7.6 are
  written to the runner's switch log.

Every failure, including an exception inside the gate itself, fails toward a
person. Nothing here retries a save.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional, Protocol

from openadapt_flow.approval_mode.model import (
    ApprovalMode,
    ApprovalModeItemEvidence,
    CheckOutcome,
    ItemContext,
    ItemDecisionKind,
    OffReason,
    resolve_approval_mode,
)
from openadapt_flow.approval_mode.post import (
    ItemDecision,
    check_verified,
    check_whole_run_clean,
    decide_problems_only,
    step_screen_problem,
    too_many_problems,
)
from openadapt_flow.approval_mode.switch import (
    StatusRejected,
    StatusRequest,
    SwitchLog,
    SwitchLogError,
    SwitchLogState,
    SwitchRecord,
    SwitchStatus,
    verify_status,
)

if TYPE_CHECKING:
    from openadapt_flow.ir import RunReport, Step, StepResult, Workflow

SignedInVerdict = Literal["match", "mismatch", "unreadable"]


class SignedInUserCheck(Protocol):
    """Reads the signed-in user from the application's own screen (P2)."""

    #: ``login_id`` where the application shows one; otherwise a display
    #: name that runner setup attested belongs only to the OpenAdapt user.
    kind: Literal["login_id", "display_name_unique_by_attestation"]

    def check(self, frame_png: bytes) -> SignedInVerdict: ...

    def reference(self, key: bytes) -> str:
        """``hmac-sha256:<hex>`` of the expected identifier under ``key``."""
        ...


def dedicated_user_reference(identifier: str, key: bytes) -> str:
    """The HMAC a switch record stores for the dedicated user's identifier."""

    digest = hmac.new(key, identifier.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


class ScreenTextSignedInUser:
    """P2 by OCR: the dedicated user's identifier appears as a whole token.

    ``expected`` comes from customer-controlled configuration (for example an
    environment variable), never from a recording. The value is held in memory
    only; evidence keeps the closed verdict.
    """

    def __init__(
        self,
        vision: Any,
        expected: str,
        *,
        kind: Literal["login_id", "display_name_unique_by_attestation"] = "login_id",
        region: Optional[tuple[int, int, int, int]] = None,
    ) -> None:
        if not expected or not expected.strip():
            raise ValueError("the dedicated user's identifier is required")
        self._vision = vision
        self._expected = expected.strip()
        self.kind = kind
        self._region = region

    def reference(self, key: bytes) -> str:
        return dedicated_user_reference(self._expected, key)

    def check(self, frame_png: bytes) -> SignedInVerdict:
        try:
            lines = self._vision.ocr(frame_png, region=self._region)
        except Exception:  # noqa: BLE001 - an unreadable screen is not a match
            return "unreadable"
        texts = [str(getattr(line, "text", "") or "") for line in lines or []]
        if not any(text.strip() for text in texts):
            return "unreadable"
        expected = self._expected.casefold()
        for text in texts:
            for token in text.split():
                if token.strip(".,;:()[]{}<>\"'").casefold() == expected:
                    return "match"
        return "mismatch"


#: ``fetch_status(request)`` returns Cloud's signed status (model, dict, JSON).
StatusFetcher = Callable[[StatusRequest], Any]


@dataclass(frozen=True)
class SaveContext:
    """What the replayer knows at the last point before one save."""

    step: "Step"
    workflow: "Workflow"
    #: The save step's own result (not yet appended to the report).
    result: "StepResult"
    #: Results of the steps before this one in the same run.
    prior_results: Sequence["StepResult"]
    effect_verifier: Any
    governed_authorization: Any
    #: Model calls made so far in this run.
    model_calls: int
    #: The frame the save's target and identity were resolved on, if any.
    frame: Callable[[], Optional[bytes]]


@dataclass(frozen=True)
class GateVerdict:
    """The gate's answer for one save."""

    proceed: bool
    decision: Optional[ItemDecision]
    checks: tuple[CheckOutcome, ...]

    def refusal_message(self) -> Optional[str]:
        """Plain-words reason for the halt. Codes only; never a value."""

        if self.proceed or self.decision is None:
            return None
        failed = next((check for check in self.checks if not check.passed), None)
        detail = f" ({failed.code}: {failed.detail})" if failed is not None else ""
        if self.decision.kind is ItemDecisionKind.STOP_BEFORE_SAVING:
            what = "stopped before saving"
        else:
            what = "is waiting for a person to approve it before saving"
        return f"Approval mode: this item {what}{detail}. Nothing was retried."


class ApprovalGate:
    """Pre-save gate and post-run finish for one item under an approval mode."""

    def __init__(
        self,
        mode: ApprovalMode | str | None,
        *,
        record: Optional[SwitchRecord] = None,
        log: SwitchLog | Path | str | None = None,
        pinned_status_keys: Optional[Mapping[str, str]] = None,
        fetch_status: Optional[StatusFetcher] = None,
        environment_digest: Optional[Callable[[], Optional[str]]] = None,
        runner_id: Optional[str] = None,
        signed_in_user: Optional[SignedInUserCheck] = None,
        dedicated_user_key: Optional[bytes] = None,
        item: Optional[ItemContext] = None,
        monotonic: Callable[[], float] = time.monotonic,
        nonce_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        self.mode = resolve_approval_mode(mode)
        self.record = record
        if log is None or isinstance(log, SwitchLog):
            self.log = log
        else:
            self.log = SwitchLog(log)
        self._pinned_keys = dict(pinned_status_keys or {})
        self._fetch_status = fetch_status
        self._environment_digest = environment_digest
        self._runner_id = runner_id
        self._signed_in_user = signed_in_user
        self._dedicated_user_key = dedicated_user_key
        self._item = item if item is not None else ItemContext()
        self._monotonic = monotonic
        self._nonce_factory = nonce_factory
        self._reset_item_state()

    # -- per-item state --------------------------------------------------------
    def _reset_item_state(self) -> None:
        self._run_ref: Optional[str] = None
        self._bundle_digest: Optional[str] = None
        self._item_checks: list[CheckOutcome] = []
        self._item_fallback: Optional[ItemDecision] = None
        self._pre_checks: list[CheckOutcome] = []
        self._decision: Optional[ItemDecision] = None
        self._gated_step_id: Optional[str] = None
        self._saves_without_person = 0
        self._passed_results: set[int] = set()
        self._item_started_logged = False
        self._off_logged: Optional[OffReason] = None
        self._status: Optional[SwitchStatus] = None
        self._status_fetched_at: Optional[float] = None
        self._finished: Optional[ApprovalModeItemEvidence] = None
        self._began = False

    def begin_item(
        self,
        *,
        run_id: str,
        bundle_content_digest: Optional[str],
        profile: Optional[str],
        inputs_from_recording: Sequence[str] = (),
        durable_resume: bool = False,
        model_egress_possible: bool = False,
    ) -> Optional[str]:
        """Start one item. Returns a refusal message, or ``None``.

        In a gated mode, a run in which an input came from the recording is
        refused before anything happens: values must arrive already approved.
        """

        self._reset_item_state()
        self._began = True
        self._run_ref = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        self._bundle_digest = bundle_content_digest
        if self.mode is ApprovalMode.NOT_GATED:
            return None
        refusal: Optional[str] = None
        recorded = sorted(set(inputs_from_recording))
        self._item_checks.append(
            CheckOutcome(
                code="inputs_supplied",
                passed=not recorded,
                detail="ok" if not recorded else "input_from_recording",
            )
        )
        if recorded:
            refusal = (
                "Approval mode: every declared input must be supplied for this "
                f"item; {len(recorded)} input(s) would come from the recording. "
                "Nothing was typed or saved."
            )
            self._item_fallback = ItemDecision(
                kind=ItemDecisionKind.APPROVAL_REQUIRED,
                reason="inputs_supplied",
                needs_person=True,
                counts_toward_problems=False,
                switch_off=None,
            )
        if self.mode is ApprovalMode.PROBLEMS_ONLY:
            self._evaluate_item_checks(
                profile=profile,
                durable_resume=durable_resume,
                model_egress_possible=model_egress_possible,
            )
        return refusal

    def _evaluate_item_checks(
        self,
        *,
        profile: Optional[str],
        durable_resume: bool,
        model_egress_possible: bool,
    ) -> None:
        item = self._item
        record = self.record
        expected_channel = record.channel if record is not None else None
        checks = [
            CheckOutcome(
                code="channel",
                passed=item.channel is not None and item.channel == expected_channel,
                detail=(
                    "ok"
                    if item.channel is not None and item.channel == expected_channel
                    else "channel_not_covered"
                ),
            ),
            CheckOutcome(
                code="inputs_strict",
                passed=item.header_maps_every_input
                and item.rows_well_formed
                and item.values_pass_types,
                detail=(
                    "ok"
                    if item.header_maps_every_input
                    and item.rows_well_formed
                    and item.values_pass_types
                    else "inputs_not_strictly_parsed"
                ),
            ),
            CheckOutcome(
                code="not_a_retry",
                passed=not item.is_retry and not item.allow_repeat,
                detail=(
                    "ok"
                    if not item.is_retry and not item.allow_repeat
                    else "retry_or_repeat"
                ),
            ),
            CheckOutcome(
                code="record_key_not_recent",
                passed=item.record_key_seen_recently is False,
                detail=(
                    "ok"
                    if item.record_key_seen_recently is False
                    else (
                        "record_key_unknown"
                        if item.record_key_seen_recently is None
                        else "record_key_entered_recently"
                    )
                ),
            ),
            CheckOutcome(
                code="deployment_config",
                passed=not item.caller_supplied_configuration
                and record is not None
                and profile == record.profile
                and not model_egress_possible,
                detail=(
                    "caller_supplied_configuration"
                    if item.caller_supplied_configuration
                    else "profile_not_covered"
                    if record is None or profile != record.profile
                    else "model_egress_possible"
                    if model_egress_possible
                    else "ok"
                ),
            ),
            CheckOutcome(
                code="switch_lease",
                passed=item.holds_switch_lease,
                detail="ok" if item.holds_switch_lease else "lease_not_held",
            ),
            CheckOutcome(
                code="not_resumed",
                passed=not durable_resume,
                detail="ok" if not durable_resume else "resumed_run",
            ),
        ]
        self._item_checks.extend(checks)
        failed = next((check for check in checks if not check.passed), None)
        if failed is not None and self._item_fallback is None:
            self._item_fallback = ItemDecision(
                kind=ItemDecisionKind.APPROVAL_REQUIRED,
                reason=failed.code,
                needs_person=True,
                counts_toward_problems=False,
                switch_off=None,
            )

    # -- before each save ------------------------------------------------------
    def before_save(self, ctx: SaveContext) -> GateVerdict:
        """Decide whether this save may proceed without a person."""

        try:
            return self._before_save(ctx)
        except Exception:  # noqa: BLE001 - every gate failure goes to a person
            decision = ItemDecision(
                kind=ItemDecisionKind.APPROVAL_REQUIRED,
                reason="gate_error",
                needs_person=True,
                counts_toward_problems=False,
                switch_off=None,
            )
            check = CheckOutcome(
                code="gate_error",
                passed=False,
                detail="gate_error",
                step_id=ctx.step.id,
            )
            self._halt(ctx, decision, [check])
            return GateVerdict(proceed=False, decision=decision, checks=(check,))

    def _halt(
        self, ctx: SaveContext, decision: ItemDecision, checks: list[CheckOutcome]
    ) -> None:
        self._pre_checks.extend(checks)
        if self._decision is None:
            self._decision = decision
            self._gated_step_id = ctx.step.id
        if decision.switch_off is not None:
            self._log_switch_off(decision.switch_off)

    def _before_save(self, ctx: SaveContext) -> GateVerdict:
        if not self._began:
            raise RuntimeError("begin_item was not called for this run")
        if self.mode is ApprovalMode.NOT_GATED:
            return GateVerdict(proceed=True, decision=None, checks=())
        if self._decision is not None:
            # A gate that already sent the item to a person never reopens.
            return GateVerdict(proceed=False, decision=self._decision, checks=())
        if self._item_fallback is not None:
            failed = next(c for c in self._item_checks if not c.passed)
            check = failed.model_copy(update={"step_id": ctx.step.id})
            self._halt(ctx, self._item_fallback, [])
            return GateVerdict(
                proceed=False, decision=self._item_fallback, checks=(check,)
            )
        if self.mode is ApprovalMode.EACH_ITEM:
            decision = ItemDecision(
                kind=ItemDecisionKind.APPROVAL_REQUIRED,
                reason="each_item",
                needs_person=True,
                counts_toward_problems=False,
                switch_off=None,
            )
            check = CheckOutcome(
                code="each_item",
                passed=False,
                detail="approval_required",
                step_id=ctx.step.id,
            )
            self._halt(ctx, decision, [check])
            return GateVerdict(proceed=False, decision=decision, checks=(check,))

        if id(ctx.result) in self._passed_results:
            # Another input edge of a save that already passed (for example a
            # focus click before typing). Re-read the switch log, which must
            # hold before every input edge, and re-check the screens.
            repeat: list[CheckOutcome] = []
            for check, failure in (
                self._check_switch_on(ctx, refresh_status=False),
                self._check_same_screens(ctx),
            ):
                repeat.append(check)
                if failure is not None:
                    self._halt(ctx, failure, repeat)
                    return GateVerdict(
                        proceed=False, decision=failure, checks=tuple(repeat)
                    )
            return GateVerdict(proceed=True, decision=None, checks=tuple(repeat))

        checks: list[CheckOutcome] = []
        for evaluate in (
            self._check_switch_on,
            self._check_signed_in_user,
            self._check_right_record,
            self._check_same_screens,
            self._check_record_check_ready,
        ):
            check, failure = evaluate(ctx)
            checks.append(check)
            if failure is not None:
                self._halt(ctx, failure, checks)
                return GateVerdict(
                    proceed=False, decision=failure, checks=tuple(checks)
                )
        # Every pre-check passed. Record that a save without a person is about
        # to start, so an interrupted item is found by the next one.
        if not self._item_started_logged:
            try:
                assert self.log is not None and self._run_ref is not None
                self.log.append("item_started", run_ref=self._run_ref)
            except (SwitchLogError, OSError, AssertionError):
                check = CheckOutcome(
                    code="switch_log",
                    passed=False,
                    detail="switch_log_unwritable",
                    step_id=ctx.step.id,
                )
                decision = ItemDecision(
                    kind=ItemDecisionKind.APPROVAL_REQUIRED,
                    reason="switch_log",
                    needs_person=True,
                    counts_toward_problems=False,
                    switch_off=None,
                )
                checks.append(check)
                self._halt(ctx, decision, checks)
                return GateVerdict(
                    proceed=False, decision=decision, checks=tuple(checks)
                )
            self._item_started_logged = True
        self._pre_checks.extend(checks)
        self._passed_results.add(id(ctx.result))
        self._saves_without_person += 1
        return GateVerdict(proceed=True, decision=None, checks=tuple(checks))

    # -- P1 --------------------------------------------------------------------
    def _check_switch_on(
        self, ctx: SaveContext, refresh_status: bool = True
    ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
        def fail(
            detail: str, off: Optional[OffReason] = None
        ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
            return (
                CheckOutcome(
                    code="switch_on", passed=False, detail=detail, step_id=ctx.step.id
                ),
                ItemDecision(
                    kind=ItemDecisionKind.APPROVAL_REQUIRED,
                    reason="switch_on",
                    needs_person=True,
                    counts_toward_problems=False,
                    switch_off=off,
                ),
            )

        record = self.record
        if record is None or self.log is None:
            return fail("no_switch_record")
        try:
            state = self.log.state()
        except (SwitchLogError, OSError):
            return fail("switch_log_unreadable")
        if (
            state.switch_id != record.switch_id
            or state.switch_record_sha256 != record.digest()
        ):
            return fail("switch_log_for_other_record")
        if state.is_off:
            return fail("switched_off")
        if any(ref != self._run_ref for ref in state.open_items):
            return fail("interrupted_item", OffReason.UNCERTAIN_RESULT)
        if self._runner_id != record.runner_id:
            return fail("runner_changed", OffReason.ENVIRONMENT_CHANGED)
        if self._bundle_digest != record.bundle_content_digest:
            return fail("version_changed", OffReason.VERSION_CHANGED)
        environment: Optional[str]
        try:
            environment = (
                self._environment_digest() if self._environment_digest else None
            )
        except Exception:  # noqa: BLE001 - an unreadable environment is not proof
            environment = None
        if environment is None:
            return fail("environment_unreadable")
        if environment != record.environment_digest:
            return fail("environment_changed", OffReason.ENVIRONMENT_CHANGED)
        if not refresh_status:
            if not self._status_is_fresh():
                return fail("status_unconfirmed")
            return CheckOutcome(
                code="switch_on", passed=True, step_id=ctx.step.id
            ), None
        status_detail, cloud_off = self._ensure_status(state)
        if cloud_off is not None:
            return fail("cloud_switched_off", cloud_off)
        if status_detail != "ok":
            self._log_status_unconfirmed(state)
            return fail(status_detail)
        if state.status_unconfirmed:
            try:
                self.log.append("status_restored")
            except (SwitchLogError, OSError):
                return fail("switch_log_unwritable")
        return CheckOutcome(code="switch_on", passed=True, step_id=ctx.step.id), None

    def _status_is_fresh(self) -> bool:
        if self._status is None or self._status_fetched_at is None:
            return False
        assert self.record is not None
        age = self._monotonic() - self._status_fetched_at
        return 0 <= age <= self.record.thresholds.status_max_age_s

    def _ensure_status(self, state: SwitchLogState) -> tuple[str, Optional[OffReason]]:
        """Return (detail, cloud_off_reason). ``detail == 'ok'`` means fresh."""

        if self._status_is_fresh():
            return "ok", None
        record = self.record
        assert record is not None
        if self._fetch_status is None:
            return "no_status_source", None
        try:
            request = StatusRequest(
                switch_id=record.switch_id,
                switch_record_sha256=record.digest(),
                runner_id=record.runner_id,
                nonce=self._nonce_factory(),
                runner_event_seq=state.last_seq,
                runner_event_sha256=state.last_sha256,
            )
        except ValueError:
            return "status_request_invalid", None
        # Freshness counts from before the request left, on the monotonic
        # clock, so a slow answer can only shorten the window.
        requested_at = self._monotonic()
        try:
            raw = self._fetch_status(request)
        except Exception:  # noqa: BLE001 - an outage is "unconfirmed", never "on"
            return "status_fetch_failed", None
        try:
            status = verify_status(raw, request=request, pinned_keys=self._pinned_keys)
        except StatusRejected:
            return "status_rejected", None
        if status.state == "off":
            return "ok", status.off_reason or OffReason.TURNED_OFF
        self._status = status
        self._status_fetched_at = requested_at
        return "ok", None

    def _log_status_unconfirmed(self, state: SwitchLogState) -> None:
        if state.status_unconfirmed or self.log is None:
            return
        try:
            self.log.append("status_unconfirmed")
        except (SwitchLogError, OSError):
            pass

    def _log_switch_off(self, reason: OffReason) -> None:
        if self.mode is not ApprovalMode.PROBLEMS_ONLY or self.log is None:
            return
        if self._off_logged is not None:
            return
        try:
            state = self.log.state()
            if not state.is_off:
                self.log.append("switched_off", reason=reason.value)
            self._off_logged = reason
        except (SwitchLogError, OSError):
            # An unwritable log reads as off for the next item anyway.
            pass

    # -- P2 --------------------------------------------------------------------
    def _check_signed_in_user(
        self, ctx: SaveContext
    ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
        def fail(
            detail: str, *, counts: bool, off: Optional[OffReason] = None
        ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
            return (
                CheckOutcome(
                    code="signed_in_user",
                    passed=False,
                    detail=detail,
                    step_id=ctx.step.id,
                ),
                ItemDecision(
                    kind=ItemDecisionKind.STOP_BEFORE_SAVING,
                    reason="signed_in_user",
                    needs_person=True,
                    counts_toward_problems=counts,
                    switch_off=off,
                ),
            )

        check = self._signed_in_user
        record = self.record
        assert record is not None
        if check is None:
            return fail("not_configured", counts=False)
        if self._dedicated_user_key is None:
            return fail("dedicated_user_unbound", counts=False)
        try:
            reference = check.reference(self._dedicated_user_key)
        except Exception:  # noqa: BLE001
            return fail("dedicated_user_unbound", counts=False)
        if not hmac.compare_digest(reference, record.dedicated_user_ref):
            return fail(
                "dedicated_user_changed",
                counts=False,
                off=OffReason.ENVIRONMENT_CHANGED,
            )
        try:
            frame = ctx.frame()
        except Exception:  # noqa: BLE001
            frame = None
        if not frame:
            return fail("frame_unavailable", counts=True)
        try:
            verdict = check.check(frame)
        except Exception:  # noqa: BLE001
            verdict = "unreadable"
        if verdict == "match":
            detail = "ok" if check.kind == "login_id" else "display_name_by_attestation"
            return (
                CheckOutcome(
                    code="signed_in_user",
                    passed=True,
                    detail=detail,
                    step_id=ctx.step.id,
                ),
                None,
            )
        if verdict == "mismatch":
            return fail("other_user_signed_in", counts=True)
        return fail("signed_in_user_unreadable", counts=True)

    # -- P3 --------------------------------------------------------------------
    def _check_right_record(
        self, ctx: SaveContext
    ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
        def fail(detail: str) -> tuple[CheckOutcome, Optional[ItemDecision]]:
            return (
                CheckOutcome(
                    code="right_record",
                    passed=False,
                    detail=detail,
                    step_id=ctx.step.id,
                ),
                ItemDecision(
                    kind=ItemDecisionKind.STOP_BEFORE_SAVING,
                    reason="right_record",
                    needs_person=True,
                    counts_toward_problems=True,
                    switch_off=None,
                ),
            )

        checks = [r.identity for r in ctx.prior_results if r.identity is not None]
        current = ctx.result.identity
        if current is not None:
            checks.append(current)
        if any(check.status != "verified" for check in checks):
            return fail("identity_not_affirmative")
        anchored = ctx.step.anchor is not None or bool(ctx.step.identity_armed)
        if anchored and (current is None or current.status != "verified"):
            return fail("save_target_not_verified")
        if not checks:
            return fail("no_right_record_check")
        return CheckOutcome(code="right_record", passed=True, step_id=ctx.step.id), None

    # -- P4 --------------------------------------------------------------------
    def _check_same_screens(
        self, ctx: SaveContext
    ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
        record = self.record
        assert record is not None
        problem: Optional[str] = None
        problem_step: Optional[str] = ctx.step.id
        if ctx.model_calls:
            problem = "model_call"
        else:
            for result in [*ctx.prior_results, ctx.result]:
                problem = step_screen_problem(result, record)
                if problem is not None:
                    problem_step = result.step_id
                    break
        if problem is None:
            return CheckOutcome(
                code="same_screens", passed=True, step_id=ctx.step.id
            ), None
        return (
            CheckOutcome(
                code="same_screens", passed=False, detail=problem, step_id=problem_step
            ),
            ItemDecision(
                kind=ItemDecisionKind.APPROVAL_REQUIRED,
                reason="same_screens",
                needs_person=True,
                counts_toward_problems=True,
                switch_off=OffReason.SCREEN_CHANGED,
            ),
        )

    # -- P5 --------------------------------------------------------------------
    def _check_record_check_ready(
        self, ctx: SaveContext
    ) -> tuple[CheckOutcome, Optional[ItemDecision]]:
        from openadapt_flow.approval_mode.coverage import save_coverage
        from openadapt_flow.policy import iter_effect_paths
        from openadapt_flow.runtime.effects.adapter import (
            _probe_adapter_connection,
            verifier_identity,
        )
        from openadapt_flow.verification import VerificationTier, verifier_effect_tier

        def fail(detail: str) -> tuple[CheckOutcome, Optional[ItemDecision]]:
            return (
                CheckOutcome(
                    code="record_check_ready",
                    passed=False,
                    detail=detail,
                    step_id=ctx.step.id,
                ),
                ItemDecision(
                    kind=ItemDecisionKind.STOP_BEFORE_SAVING,
                    reason="record_check_ready",
                    needs_person=True,
                    counts_toward_problems=True,
                    switch_off=OffReason.RECORD_CHECK_LOST,
                ),
            )

        record = self.record
        assert record is not None
        verifier = ctx.effect_verifier
        if verifier is None:
            return fail("no_record_check")
        authorization = ctx.governed_authorization
        if authorization is not None and authorization.approves_unverified_write(
            ctx.step
        ):
            return fail("approved_unverified_write")
        coverage = save_coverage(ctx.workflow, ctx.step)
        if not coverage.covered:
            return fail(coverage.problems[0])
        for _path, effects in iter_effect_paths(ctx.step):
            for effect in effects:
                tier = verifier_effect_tier(verifier, effect)
                if tier is not VerificationTier.INDEPENDENT_SYSTEM:
                    return fail("read_back_not_system_of_record")
        candidates = getattr(verifier, "_candidates", None) or (verifier,)
        allowed = set(record.verifier_identities)
        for candidate in candidates:
            if verifier_identity(candidate) not in allowed:
                return fail("verifier_identity_changed")
        probe = _probe_adapter_connection(verifier)
        if not probe.ok:
            return fail("record_check_unreachable")
        return (
            CheckOutcome(code="record_check_ready", passed=True, step_id=ctx.step.id),
            None,
        )

    # -- after the run -----------------------------------------------------------
    def finish_item(self, report: "RunReport") -> ApprovalModeItemEvidence:
        """Decide the item, write the switch log, and return the evidence block.

        Safe to call again for the same run (for example after a late
        correction of the report): a changed decision is logged again, an
        unchanged one is not.
        """

        mode = self.mode
        channel = self._item.channel
        record = self.record
        switch_fields: dict[str, Any] = {}
        if record is not None and mode is ApprovalMode.PROBLEMS_ONLY:
            switch_fields = {
                "switch_id": record.switch_id,
                "switch_record_sha256": record.digest(),
            }
        if mode is ApprovalMode.NOT_GATED:
            evidence = ApprovalModeItemEvidence(
                mode=mode,
                channel=channel,
                decision=ItemDecisionKind.NOT_GATED,
                decision_reason="no_per_item_pause",
                needs_person=report.transaction_outcome != "VERIFIED",
            )
            self._finished = evidence
            return evidence
        if mode is ApprovalMode.EACH_ITEM:
            decision = self._decision
            if decision is not None:
                evidence = ApprovalModeItemEvidence(
                    mode=mode,
                    channel=channel,
                    item_checks=list(self._item_checks),
                    pre_checks=list(self._pre_checks),
                    gated_step_id=self._gated_step_id,
                    decision=decision.kind,
                    decision_reason=decision.reason,
                    needs_person=True,
                )
            else:
                evidence = ApprovalModeItemEvidence(
                    mode=mode,
                    channel=channel,
                    item_checks=list(self._item_checks),
                    decision=ItemDecisionKind.PROBLEM,
                    decision_reason=(
                        report.transaction_outcome or "unknown_outcome"
                    ).lower(),
                    needs_person=True,
                )
            self._finished = evidence
            return evidence

        # problems_only
        if record is None:
            r1 = CheckOutcome(code="verified", passed=False, detail="no_switch_record")
            r2 = CheckOutcome(
                code="whole_run_clean", passed=False, detail="no_switch_record"
            )
        else:
            r1 = check_verified(
                report, record, saves_without_person=self._saves_without_person
            )
            r2 = check_whole_run_clean(report, record)
        gate_decision = self._decision
        if gate_decision is None and self._item_fallback is not None:
            gate_decision = self._item_fallback
        decision = decide_problems_only(
            report, r1=r1, r2=r2, gate_decision=gate_decision
        )
        previous = self._finished
        if (
            previous is not None
            and previous.decision is decision.kind
            and previous.decision_reason == decision.reason
        ):
            recorded = previous.switch_log_recorded
            off = previous.switch_off_reason
        else:
            recorded, off = self._log_finish(decision)
        kind = decision.kind
        reason = decision.reason
        needs_person = decision.needs_person
        counts = decision.counts_toward_problems
        if kind is ItemDecisionKind.NO_PERSON and not recorded:
            # A saved-and-checked item whose result could not be logged still
            # goes to a person. Its open ``item_started`` event also makes the
            # next item find it and switch off as interrupted.
            kind = ItemDecisionKind.PROBLEM
            reason = "switch_log"
            needs_person = True
            counts = False
        evidence = ApprovalModeItemEvidence(
            mode=mode,
            channel=channel,
            **switch_fields,
            item_checks=list(self._item_checks),
            pre_checks=list(self._pre_checks),
            post_checks=[r1, r2],
            saves_without_person=self._saves_without_person,
            gated_step_id=self._gated_step_id,
            decision=kind,
            decision_reason=reason,
            needs_person=needs_person,
            counts_toward_problems=counts,
            switch_off_reason=off,
            switch_log_recorded=recorded,
        )
        self._finished = evidence
        return evidence

    def _log_finish(self, decision: ItemDecision) -> tuple[bool, Optional[OffReason]]:
        """Append ``item_finished`` and any switch-off. Returns (recorded, off)."""

        off = decision.switch_off
        if self.log is None or self.record is None or self._run_ref is None:
            return False, off
        try:
            self.log.append(
                "item_finished",
                run_ref=self._run_ref,
                decision=decision.kind,
                reason=decision.reason,
                counts_toward_problems=decision.counts_toward_problems,
            )
        except (SwitchLogError, OSError):
            return False, off
        if off is not None:
            self._log_switch_off(off)
        if not decision.counts_toward_problems:
            # Only a counted problem can tip the problem rate.
            return True, off
        try:
            state = self.log.state()
            if not state.is_off and too_many_problems(
                state.finished, self.record.thresholds
            ):
                self.log.append(
                    "switched_off", reason=OffReason.TOO_MANY_PROBLEMS.value
                )
                self._off_logged = OffReason.TOO_MANY_PROBLEMS
                off = off or OffReason.TOO_MANY_PROBLEMS
        except (SwitchLogError, OSError):
            return True, off
        return True, off

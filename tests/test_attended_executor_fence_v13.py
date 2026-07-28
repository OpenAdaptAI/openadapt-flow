"""Attended executor fences for durable continuation v13.

The executor result is a transport receipt.  The retained run state and the
external monotonic authority decide whether the result is admissible.
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedExecutionResult,
    execute_attended_action,
)
from openadapt_flow.runtime.durable.attended_service import AttendedActionService
from openadapt_flow.runtime.durable.authority import (
    DurableAuthority,
    DurableAuthorityBusy,
    DurableAuthorityRecord,
)
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
from openadapt_flow.runtime.durable.continuation import (
    ContinuationBusy,
    ContinuationCoordinator,
    ContinuationToken,
    current_continuation_token,
)
from tests.test_attended_actions import _paused, _request


def _authority_record(run_dir: Path) -> DurableAuthorityRecord:
    store = CheckpointStore(run_dir)
    authority = DurableAuthority(run_dir, store)
    with authority._transaction() as connection:  # noqa: SLF001
        record = authority._read(connection)  # noqa: SLF001
    assert record is not None
    return record


def _assert_direct_retry_is_fenced(run_dir: Path) -> None:
    record = _authority_record(run_dir)
    assert record.phase == "reconciliation_required"
    assert record.attempt_phase == "reconciliation_required"
    with pytest.raises((ContinuationBusy, DurableAuthorityBusy)):
        with ContinuationCoordinator(run_dir).lease(operation="resume"):
            pass


def test_owner_thread_receives_the_exact_admitted_continuation_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thread-affine executor must reuse, not replace, the active lease."""

    _workflow, _bundle, run_dir, _store, capability = _paused(tmp_path)
    admitted: list[ContinuationToken] = []
    observed: list[ContinuationToken | None] = []
    original_bind = ContinuationCoordinator.bind_approval

    def bind_and_retain(
        coordinator: ContinuationCoordinator,
        token: ContinuationToken,
        approval: Any,
    ) -> None:
        admitted.append(token)
        original_bind(coordinator, token, approval)

    class TokenCheckingExecutor:
        def continue_run(self, run_dir, capability, approval):
            token = current_continuation_token()
            observed.append(token)
            assert admitted and token == admitted[0]
            assert token is not None
            with ContinuationCoordinator(run_dir).lease(operation="resume") as reused:
                assert reused == token
            return AttendedExecutionResult(
                status="refused",
                message="the observation-only check refused before delivery",
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )

        def skip_run(self, run_dir, capability, approval):
            return self.continue_run(run_dir, capability, approval)

    monkeypatch.setattr(ContinuationCoordinator, "bind_approval", bind_and_retain)
    monkeypatch.setattr(
        "openadapt_flow.runtime.durable.attended_service._deployment_executor",
        lambda _deployment, *, key: nullcontext(TokenCheckingExecutor()),
    )

    with AttendedActionService(DeploymentConfig()) as service:
        decision = service.execute(
            run_dir,
            _request(capability, key="request-owner-token-v13"),
            operator="staff",
        )

    assert decision.status == "refused"
    assert len(admitted) == 1
    assert observed == admitted


@pytest.mark.parametrize("mode", ["exception", "malformed"])
def test_non_terminal_executor_response_requires_reconciliation_before_retry(
    tmp_path: Path,
    mode: str,
) -> None:
    """An absent valid receipt must never restore direct-resume eligibility."""

    _workflow, _bundle, run_dir, _store, capability = _paused(tmp_path)

    class InvalidExecutor:
        def continue_run(self, run_dir, capability, approval):
            if mode == "exception":
                raise RuntimeError("executor stopped without a terminal receipt")
            return {
                "status": "completed",
                "message": "self-asserted dictionary",
                "report_success": True,
                "unexpected": "not part of the closed result schema",
            }

        def skip_run(self, run_dir, capability, approval):
            return self.continue_run(run_dir, capability, approval)

    with pytest.raises(
        (RuntimeError, AttendedActionRefused, AttributeError, TypeError)
    ):
        execute_attended_action(
            run_dir,
            _request(capability, key=f"request-invalid-{mode}-v13"),
            operator="staff",
            executor=InvalidExecutor(),
        )

    _assert_direct_retry_is_fenced(run_dir)


@pytest.mark.parametrize("status", ["completed", "halted"])
def test_self_asserted_terminal_outcome_needs_durable_progress_proof(
    tmp_path: Path,
    status: str,
) -> None:
    """A fake terminal receipt cannot replace checkpoints or a final report."""

    _workflow, _bundle, run_dir, _store, capability = _paused(tmp_path)

    class SelfAssertedExecutor:
        def continue_run(self, run_dir, capability, approval):
            return AttendedExecutionResult(
                status=status,
                message=f"executor asserted {status} without durable progress",
                report_success=status == "completed",
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )

        def skip_run(self, run_dir, capability, approval):
            return self.continue_run(run_dir, capability, approval)

    with pytest.raises(AttendedActionRefused, match="durable|authority|proof|receipt"):
        execute_attended_action(
            run_dir,
            _request(capability, key=f"request-self-asserted-{status}-v13"),
            operator="staff",
            executor=SelfAssertedExecutor(),
        )

    _assert_direct_retry_is_fenced(run_dir)


def test_refused_outcome_requires_the_original_pause_to_remain_unchanged(
    tmp_path: Path,
) -> None:
    """A refused receipt is valid only for unchanged pre-delivery state."""

    _workflow, _bundle, run_dir, store, capability = _paused(tmp_path)

    class StateChangingRefusal:
        def continue_run(self, run_dir, capability, approval):
            store.clear_pending()
            return AttendedExecutionResult(
                status="refused",
                message="executor asserted refusal after changing durable state",
                report_success=False,
                resumed_from=capability.step_id,
                next_transition=capability.expected_next_transition,
            )

        def skip_run(self, run_dir, capability, approval):
            return self.continue_run(run_dir, capability, approval)

    with pytest.raises(AttendedActionRefused, match="durable|authority|proof|receipt"):
        execute_attended_action(
            run_dir,
            _request(capability, key="request-state-changing-refusal-v13"),
            operator="staff",
            executor=StateChangingRefusal(),
        )

    _assert_direct_retry_is_fenced(run_dir)

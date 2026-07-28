"""One signed relay loop for every attended engine action.

The component suites already prove the phone portal, the Cloud projection, and
the runner relay separately.  This test pins the last cross-component
invariant inside Flow: every runner action in a verified signed relay reaches
the normal engine journal, and an acknowledgement lost after that journal
write never repeats the action in a new process.
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.console.attention import attention_item
from openadapt_flow.console.decision_relay import DecisionRelay, RelayUncertain
from openadapt_flow.console.decision_supervisor import DecisionSupervisor
from openadapt_flow.ir import (
    ActionKind,
    Guard,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRequest,
    AttendedActionStore,
    AttendedExecutionResult,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_decision_relay import TOKEN, _deployment
from tests.test_decision_supervisor import (
    _halted_run,
    _relayed_for,
    _SequencedTransport,
)
from tests.test_replayer import FakeBackend, FakeVision, Match


class _FileCountingExecutor:
    """A deployment executor whose only external effect is a durable count."""

    def __init__(self, counter: Path) -> None:
        self.counter = counter

    def _completed(self, capability: Any) -> AttendedExecutionResult:
        prior = int(self.counter.read_text()) if self.counter.is_file() else 0
        self.counter.write_text(str(prior + 1))
        return AttendedExecutionResult(
            status="completed",
            message="verified",
            report_success=True,
            next_transition=capability.expected_next_transition,
        )

    def continue_run(
        self, run_dir: Path, capability: Any, approval: Any
    ) -> AttendedExecutionResult:
        return self._completed(capability)

    def skip_run(
        self, run_dir: Path, capability: Any, approval: Any
    ) -> AttendedExecutionResult:
        return self._completed(capability)


def _serve_in_child(
    runs: str,
    relay_body: dict[str, Any],
    counter: str,
    lose_ack: bool,
    result_queue: Any,
) -> None:
    """Execute one supervisor cycle in a fresh spawned interpreter."""
    deployment = _deployment()
    acknowledgement: Any = (
        RelayUncertain("the acknowledgement response was lost")
        if lose_ack
        else (200, {"accepted": True})
    )
    transport = _SequencedTransport(
        polls=[(200, {"decision": relay_body})],
        acknowledgements=[acknowledgement],
    )
    report = DecisionSupervisor(
        Path(runs),
        relay=DecisionRelay(transport, token=TOKEN, deployment=deployment),
        deployment=deployment,
        executor=_FileCountingExecutor(Path(counter)),
    ).serve_once(wait_s=0.0)
    result_queue.put(
        {
            "acknowledged": report.acknowledged,
            "reacknowledged": report.reacknowledged,
            "outcome": report.outcome.model_dump(mode="json")
            if report.outcome is not None
            else None,
        }
    )


def _new_process_cycle(
    runs: Path,
    relay_body: dict[str, Any],
    counter: Path,
    *,
    lose_ack: bool,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_serve_in_child,
        args=(str(runs), relay_body, str(counter), lose_ack, result_queue),
    )
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("the decision supervisor child process did not exit")
    assert process.exitcode == 0
    result = result_queue.get(timeout=5)
    result_queue.close()
    result_queue.join_thread()
    return result


def _skippable_halt(runs: Path, bundles: Path) -> tuple[Path, object]:
    """Produce a real halt whose signed capability permits every action."""
    workflow = Workflow(
        name="phone-skip-contract",
        steps=[
            Step(
                id="human",
                intent="complete the optional attended step",
                action=ActionKind.KEY,
                key="A",
                guard=Guard(
                    predicate=Predicate(
                        kind=PredicateKind.TEXT_PRESENT,
                        text="READY",
                    ),
                    on_unmet="skip",
                ),
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="DONE",
                        timeout_s=0.01,
                    )
                ],
            ),
            Step(id="next", intent="continue", action=ActionKind.KEY, key="B"),
        ],
    )
    bundle = bundles / "skip"
    run = runs / "skip"
    workflow.save(bundle)
    vision = FakeVision()
    vision.text_results = {
        "READY": Match(point=(10, 10), region=(0, 0, 20, 20), confidence=1.0)
    }
    report = Replayer(
        FakeBackend(), vision=vision, durable=True, poll_interval_s=0.0
    ).run(workflow, bundle_dir=bundle, run_dir=run)
    assert report.success is False
    item = attention_item(runs, run)
    assert item is not None and item.durably_paused
    capability = AttendedActionStore(run).read()
    assert set(capability.allowed_actions) == {
        "continue",
        "skip",
        "reject",
        "teach",
        "escalate",
    }
    return run, item


@pytest.mark.parametrize(
    ("action", "portable_action", "expected_status", "executor_calls"),
    [
        ("continue", "verify_and_resume", "completed", 1),
        ("skip", "skip", "completed", 1),
        ("reject", "reject", "rejected", 0),
        ("teach", "teach", "needs_demonstration", 0),
        ("escalate", "escalate", "escalated", 0),
    ],
)
def test_every_signed_relay_action_survives_lost_ack_and_process_restart(
    tmp_path: Path,
    action: str,
    portable_action: str,
    expected_status: str,
    executor_calls: int,
) -> None:
    """Run the exact signed relay in two fresh spawned Python processes."""
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    if action == "skip":
        run, item = _skippable_halt(runs, bundles)
    else:
        run, item = _halted_run(runs, bundles, action)

    deployment = _deployment()
    relay_body = _relayed_for(
        run,
        item,
        deployment,
        action=action,
        decision_action=portable_action,
        idempotency_key=f"phone-{action}-idempotency-0001",
    )
    counter = tmp_path / f"{action}.count"

    first = _new_process_cycle(runs, relay_body, counter, lose_ack=True)
    first_log = json.loads(AttendedActionStore(run).decisions_path.read_text())
    decisions_after_first = first_log["decisions"]
    report_after_first = (run / "report.json").read_bytes()
    pending_after_first = (run / "pending_escalation.json").read_bytes()

    second = _new_process_cycle(runs, relay_body, counter, lose_ack=False)
    final_log = json.loads(AttendedActionStore(run).decisions_path.read_text())

    assert first["outcome"]["status"] == expected_status
    assert first["outcome"]["action"] == action
    assert first["acknowledged"] == "accepted"
    assert first["reacknowledged"] is False
    assert second["outcome"] == first["outcome"]
    assert second["acknowledged"] == "accepted"
    assert second["reacknowledged"] is True
    observed_calls = int(counter.read_text()) if counter.is_file() else 0
    assert observed_calls == executor_calls

    # The second process can confirm the retained ACK. It cannot append a new
    # engine decision or change the run that the first process already ended,
    # resumed, parked, or marked for teaching.
    assert final_log["decisions"] == decisions_after_first
    assert (run / "report.json").read_bytes() == report_after_first
    assert (run / "pending_escalation.json").read_bytes() == pending_after_first
    acknowledgements = final_log["relay_acknowledgements"]
    assert len(acknowledgements) == 1
    assert acknowledgements[0]["action"] == action
    assert acknowledgements[0]["retained_status"] == expected_status
    assert acknowledgements[0]["confirmed"] is True

    engine_vocabulary = set(
        AttendedActionRequest.model_fields["action"].annotation.__args__
    )
    assert engine_vocabulary == {
        "continue",
        "skip",
        "reject",
        "teach",
        "escalate",
    }

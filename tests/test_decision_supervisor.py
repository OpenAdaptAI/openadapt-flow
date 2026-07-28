"""The loop that makes the hosted lane real, and what it refuses to do.

``decision_relay`` proved the wire. This file proves the two things a wire
cannot: that an open pause becomes answerable **without anyone calling
anything**, and that an answer is applied to the pause it was minted from
rather than to whichever run happened to be in hand.

The second is the one worth the most scrutiny. ``DecisionRelay.serve_once``
takes its run and item as arguments, which is safe only when exactly one pause
is open. A practice with two halted runs is the ordinary case, not the exotic
one, so ``test_a_decision_is_executed_against_the_pause_it_was_minted_from``
opens two and answers the second.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow.console.attention import attention_item
from openadapt_flow.console.decision_relay import (
    DecisionRelay,
    RelayedDecision,
    RelayRefused,
    RelayUncertain,
)
from openadapt_flow.console.decision_supervisor import (
    DecisionSupervisor,
    DecisionSupervisorThread,
)
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedActionStore,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_attended_actions import _ResultExecutor
from tests.test_decision_relay import (
    TOKEN,
    FakeTransport,
    _bound_relay_payload,
    _deployment,
    _sign,
)
from tests.test_replayer import FakeBackend, FakeVision

PROTECTED_VALUE = "Wilhelmina Featherstonehaugh"

LONG_PAST = "2000-01-01T00:00:00Z"


# --------------------------------------------------------------- fixtures


def _halted_run(runs_root: Path, bundles_root: Path, name: str):
    """One durably paused run under ``runs_root``, plus its attention item."""
    workflow = Workflow(
        name=f"supervisor-{name}",
        params={"patient": PROTECTED_VALUE},
        steps=[
            Step(
                id="human",
                intent=f"confirm coverage for {PROTECTED_VALUE}",
                action=ActionKind.KEY,
                key="A",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="COVERAGE ACTIVE",
                        timeout_s=0.01,
                    )
                ],
            ),
            Step(id="next", intent="record it", action=ActionKind.KEY, key="B"),
        ],
    )
    bundle = bundles_root / name
    run = runs_root / name
    workflow.save(bundle)
    report = Replayer(
        FakeBackend(), vision=FakeVision(), durable=True, poll_interval_s=0.0
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run,
        params={"patient": PROTECTED_VALUE},
    )
    assert report.success is False
    item = attention_item(runs_root, run)
    assert item is not None
    assert item.durably_paused is True
    return run, item


def _supervisor(
    runs_root: Path,
    *,
    deployment: Any = None,
    executor: Any = None,
    **responses: Any,
):
    deployment = deployment or _deployment()
    transport = FakeTransport(responses)
    relay = DecisionRelay(transport, token=TOKEN, deployment=deployment)
    supervisor = DecisionSupervisor(
        runs_root, relay=relay, deployment=deployment, executor=executor
    )
    return supervisor, transport


def _relayed_for(run: Path, item: Any, deployment: Any, **overrides: Any):
    """Mint the relay the control plane would mint for one real open pause.

    Built from the same helper the transport tests use, so a change to the
    projection cannot make this file agree with itself while disagreeing with
    the wire.
    """
    unsigned = _bound_relay_payload(run, item, deployment)
    unsigned.update(overrides)
    return _sign(unsigned)


# ------------------------------------------------------- publishing a queue


def test_an_open_pause_is_published_without_anyone_calling_anything(tmp_path):
    """The whole point: a halt becomes answerable from a phone by itself."""
    runs = tmp_path / "runs"
    _halted_run(runs, tmp_path / "bundles", "one")
    supervisor, transport = _supervisor(
        runs, tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"})
    )

    report = supervisor.publish_open_pauses()

    assert report.certain_count == 1
    assert len(report.published) == 1
    assert report.unknown == ()
    assert [path for path, _ in transport.calls] == ["/api/human-decisions/tasks"]


def test_every_open_pause_is_published_not_only_the_first(tmp_path):
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    _halted_run(runs, bundles, "one")
    _halted_run(runs, bundles, "two")
    supervisor, transport = _supervisor(
        runs, tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"})
    )

    report = supervisor.publish_open_pauses()

    assert len(report.published) == 2
    assert len(transport.calls) == 2


def test_nothing_protected_reaches_the_wire_from_the_whole_queue(tmp_path):
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    _halted_run(runs, bundles, "one")
    _halted_run(runs, bundles, "two")
    supervisor, transport = _supervisor(
        runs, tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"})
    )
    supervisor.publish_open_pauses()

    import json

    body = json.dumps([payload for _, payload in transport.calls])
    assert PROTECTED_VALUE not in body
    assert "supervisor-one" not in body
    assert "COVERAGE ACTIVE" not in body


def test_an_uncertain_publish_is_reported_as_unknown_never_as_reachable(tmp_path):
    runs = tmp_path / "runs"
    _halted_run(runs, tmp_path / "bundles", "one")
    supervisor, _ = _supervisor(runs, tasks=RelayUncertain("connection reset"))

    report = supervisor.publish_open_pauses()

    assert len(report.unknown) == 1
    assert report.published == ()
    assert report.already_published == ()
    assert report.certain_count == 0


def test_a_run_that_cannot_be_projected_is_recorded_not_raised(tmp_path):
    """A pause with no remote issuance is still served by the local console."""
    runs = tmp_path / "runs"
    run, _item = _halted_run(runs, tmp_path / "bundles", "one")
    # Break the capability so projection refuses, exactly as a closed pause
    # would.
    AttendedActionStore(run).capability_path.write_text("{}")
    supervisor, transport = _supervisor(
        runs, tasks=(200, {"accepted": True, "created": True})
    )

    report = supervisor.publish_open_pauses()

    assert report.published == ()
    assert report.already_published == ()
    assert transport.calls == []


def test_a_run_that_is_not_durably_paused_is_never_published(tmp_path):
    """A run that already resumed has no pause for a decision to bind to."""
    runs = tmp_path / "runs"
    run, _item = _halted_run(runs, tmp_path / "bundles", "one")
    supervisor, _ = _supervisor(runs)

    assert [pause.run_dir for pause in supervisor.open_pauses()] == [run]

    # Resuming clears the durable pause; the signed capability file survives it,
    # which is exactly why `durably_paused` and not the capability is the gate.
    (run / "pending_escalation.json").unlink()
    assert AttendedActionStore(run).capability_path.exists()
    assert supervisor.open_pauses() == []


class _ScriptedTransport(FakeTransport):
    """A transport whose /tasks response changes per call."""

    def __init__(self, task_responses: list, **responses) -> None:
        super().__init__(responses)
        self.task_responses = list(task_responses)

    def post(self, path, payload, *, timeout_s):
        self.calls.append((path, payload))
        if path.endswith("/tasks"):
            response = self.task_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return super().post(path, payload, timeout_s=timeout_s)


def test_one_refused_pause_does_not_silence_the_others(tmp_path):
    """The failure that would leave a whole practice's halts unreachable."""
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    _halted_run(runs, bundles, "one")
    _halted_run(runs, bundles, "two")
    deployment = _deployment()
    transport = _ScriptedTransport(
        [
            (400, {"error": "the projection is invalid"}),
            (200, {"accepted": True, "created": True, "task_id": "task_x"}),
        ]
    )
    relay = DecisionRelay(transport, token=TOKEN, deployment=deployment)
    supervisor = DecisionSupervisor(runs, relay=relay, deployment=deployment)

    report = supervisor.publish_open_pauses()

    assert len(report.refused) == 1
    assert len(report.published) == 1
    # Both were attempted; the refusal did not stop the loop.
    assert len([p for p, _ in transport.calls if p.endswith("/tasks")]) == 2


def test_an_accepted_pause_is_not_republished_every_cycle(tmp_path):
    """An open pause can last hours; re-POSTing identical bytes is noise."""
    runs = tmp_path / "runs"
    _halted_run(runs, tmp_path / "bundles", "one")
    supervisor, transport = _supervisor(
        runs, tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"})
    )

    first = supervisor.publish_open_pauses()
    second = supervisor.publish_open_pauses()

    assert len(first.published) == 1
    assert len(second.already_published) == 1
    assert len([p for p, _ in transport.calls if p.endswith("/tasks")]) == 1


def test_an_uncertain_pause_is_republished_because_the_post_is_idempotent(tmp_path):
    """Uncertainty is never memoized as success."""
    runs = tmp_path / "runs"
    _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    transport = _ScriptedTransport(
        [
            RelayUncertain("connection reset"),
            (200, {"accepted": True, "created": True, "task_id": "task_x"}),
        ]
    )
    relay = DecisionRelay(transport, token=TOKEN, deployment=deployment)
    supervisor = DecisionSupervisor(runs, relay=relay, deployment=deployment)

    assert len(supervisor.publish_open_pauses().unknown) == 1
    assert len(supervisor.publish_open_pauses().published) == 1


# ---------------------------------------------------- resolving an answer


def test_a_decision_is_executed_against_the_pause_it_was_minted_from(tmp_path):
    """Two runs are halted; the answer belongs to exactly one of them."""
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    _halted_run(runs, bundles, "one")
    run_two, item_two = _halted_run(runs, bundles, "two")
    deployment = _deployment()

    relay_body = _relayed_for(run_two, item_two, deployment)
    supervisor, transport = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    resolved = supervisor.resolve(
        RelayedDecision(decision_id=str(relay_body["decision_id"]), relay=relay_body)
    )
    assert resolved is not None
    assert resolved.run_dir == run_two


def test_one_cycle_publishes_executes_and_acknowledges_without_a_caller(tmp_path):
    """The whole lane, end to end, driven by nothing but the loop itself.

    This is the test that distinguishes a wired lane from a library that looks
    wired: no caller resolves the run, no caller supplies the item, and the
    engine -- not the relay -- performs the continuation.
    """
    runs = tmp_path / "runs"
    bundles = tmp_path / "bundles"
    _halted_run(runs, bundles, "one")
    run_two, item_two = _halted_run(runs, bundles, "two")
    deployment = _deployment()
    executor = _ResultExecutor()
    relay_body = _relayed_for(run_two, item_two, deployment)
    supervisor, transport = _supervisor(
        runs,
        deployment=deployment,
        executor=executor,
        tasks=(200, {"accepted": True, "created": True, "task_id": "task_x"}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    report = supervisor.serve_once(wait_s=0.0)

    # Both pauses were made answerable.
    assert len(report.publishes.published) == 2
    # Exactly one was answered, and the engine ran it.
    assert report.acknowledged == "accepted"
    assert report.outcome is not None
    assert report.outcome.status == "completed"
    assert executor.calls == 1
    ack_path, ack_body = transport.calls[-1]
    assert ack_path.endswith("/ack")
    assert ack_body["result"] == "accepted"


def test_a_governed_refusal_is_acknowledged_and_re_raised(tmp_path):
    """A refused answer must reach the operator, not vanish into the loop."""
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    # A binding the engine will not admit: the relay resolves to the right
    # pause, and revalidation still refuses it.
    relay_body = _relayed_for(
        run, item, deployment, binding_digest="sha256:" + "9" * 64
    )
    supervisor, transport = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    with pytest.raises(AttendedActionRefused):
        supervisor.serve_once(wait_s=0.0)

    assert transport.calls[-1][1]["result"] == "refused"


def test_a_decision_matching_no_open_pause_is_acknowledged_stale_not_executed(
    tmp_path,
):
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    relay_body = _relayed_for(
        run, item, deployment, capability_digest="sha256:" + "f" * 64
    )
    supervisor, transport = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    report = supervisor.serve_once(wait_s=0.0)

    assert report.acknowledged == "stale"
    assert report.outcome is None
    ack = [payload for path, payload in transport.calls if path.endswith("/ack")]
    assert ack and ack[0]["result"] == "stale"


def test_an_expired_decision_is_acknowledged_expired_not_executed(tmp_path):
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    relay_body = _relayed_for(run, item, deployment, expires_at=LONG_PAST)
    supervisor, transport = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    report = supervisor.serve_once(wait_s=0.0)

    assert report.acknowledged == "expired"
    assert report.outcome is None


def test_an_unreadable_deadline_is_treated_as_expired_not_as_open_ended(tmp_path):
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    relay_body = _relayed_for(run, item, deployment, expires_at="whenever")
    supervisor, _ = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True}),
        poll=(200, {"decision": relay_body}),
        ack=(200, {"accepted": True}),
    )

    assert supervisor.serve_once(wait_s=0.0).acknowledged == "expired"


def test_no_waiting_decision_is_a_quiet_cycle_not_an_error(tmp_path):
    runs = tmp_path / "runs"
    _halted_run(runs, tmp_path / "bundles", "one")
    supervisor, _ = _supervisor(
        runs,
        tasks=(200, {"accepted": True, "created": True}),
        poll=(204, {}),
    )

    report = supervisor.serve_once(wait_s=0.0)

    assert report.decision_id is None
    assert report.acknowledged is None
    assert len(report.publishes.published) == 1


def test_an_unverifiable_decision_is_refused_before_any_resolution(tmp_path):
    """A relay this runner's credential cannot have produced is never run."""
    runs = tmp_path / "runs"
    run, item = _halted_run(runs, tmp_path / "bundles", "one")
    deployment = _deployment()
    forged = _relayed_for(run, item, deployment)
    forged = {**forged, "relay_signature": "hmac-sha256:" + "0" * 64}
    supervisor, _ = _supervisor(
        runs,
        deployment=deployment,
        executor=_ResultExecutor(),
        tasks=(200, {"accepted": True, "created": True}),
        poll=(200, {"decision": forged}),
    )

    with pytest.raises(RelayRefused):
        supervisor.serve_once(wait_s=0.0)


# ------------------------------------------------------------- the thread


class _StubSupervisor:
    """A supervisor whose cycles are scripted, so the loop is testable."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    def serve_once(self, *, wait_s: float = 0.0, publish: bool = True) -> Any:
        self.calls += 1
        if not self.script:
            raise RelayUncertain("no more script")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _cycle(acknowledged: Optional[str] = None, unknown: tuple[str, ...] = ()):
    from openadapt_flow.console.decision_supervisor import CycleReport, PublishReport

    return CycleReport(
        publishes=PublishReport(unknown=unknown), acknowledged=acknowledged
    )


def test_the_loop_counts_outcomes_and_stops_when_asked():
    stub = _StubSupervisor([_cycle("accepted"), _cycle("stale"), _cycle("expired")])
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=lambda _s: None)

    stop_after = {"n": 0}

    def on_cycle(_report: Any) -> None:
        stop_after["n"] += 1
        if stop_after["n"] >= 3:
            thread.stop(timeout_s=0.0)

    thread._on_cycle = on_cycle
    thread.run()

    assert thread.stats.cycles == 3
    assert thread.stats.decisions_executed == 1
    assert thread.stats.decisions_stale == 1
    assert thread.stats.decisions_expired == 1


def test_a_relay_refusal_backs_the_loop_off_instead_of_spinning():
    slept: list[float] = []
    stub = _StubSupervisor(
        [RelayRefused("control plane refused"), RelayRefused("again"), _cycle()]
    )
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=slept.append)

    def on_cycle(_report: Any) -> None:
        thread.stop(timeout_s=0.0)

    thread._on_cycle = on_cycle
    thread.run()

    assert slept == [2.0, 4.0]
    # A successful cycle clears the backoff, so an outage does not permanently
    # slow the lane.
    assert thread.stats.consecutive_failures == 0


def test_a_governed_refusal_is_an_answer_not_an_outage():
    """A refused decision must not back the transport off; nothing is wrong."""
    slept: list[float] = []
    stub = _StubSupervisor([AttendedActionRefused("revalidation failed"), _cycle()])
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=slept.append)

    def on_cycle(_report: Any) -> None:
        thread.stop(timeout_s=0.0)

    thread._on_cycle = on_cycle
    thread.run()

    assert slept == []
    assert thread.stats.decisions_refused == 1


def test_the_loop_never_dies_on_an_unexpected_error():
    slept: list[float] = []
    stub = _StubSupervisor([ValueError("something unforeseen"), _cycle()])
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=slept.append)

    def on_cycle(_report: Any) -> None:
        thread.stop(timeout_s=0.0)

    thread._on_cycle = on_cycle
    thread.run()

    assert thread.stats.cycles == 1
    assert slept == [2.0]


def test_starting_twice_is_refused():
    stub = _StubSupervisor([])
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=lambda _s: None)
    thread._stop.set()  # so the thread exits immediately
    thread.start()
    with pytest.raises(RuntimeError):
        thread.start()
    thread.stop()


def test_the_thread_is_a_daemon_so_it_cannot_hold_the_console_open():
    stub = _StubSupervisor([])
    thread = DecisionSupervisorThread(stub, wait_s=0.0, sleep=lambda _s: None)
    thread._stop.set()
    thread.start()
    assert isinstance(thread._thread, threading.Thread)
    assert thread._thread.daemon is True
    thread.stop()
    assert thread.running is False


# ------------------------------------------------------------------- the CLI
#
# `--remote-decisions` must fail loudly, never quietly. An operator who asked
# for it and silently got a loopback-only console would believe a phone can
# answer a halt when nothing is listening for one.


def _console_args(*extra: str, config: Optional[Path] = None):
    from openadapt_flow.__main__ import build_parser

    argv = ["console", *extra]
    if config is not None:
        argv += ["--config", str(config)]
    return build_parser().parse_args(argv)


def _remote_config(tmp_path: Path, *, enabled: bool = True) -> Path:
    import yaml

    path = tmp_path / "deployment.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "human_decisions": {
                    "remote": {
                        "enabled": enabled,
                        "tenant_id": "tenant_exact_01",
                        "runner_id": "runner_exact_01",
                    }
                }
            }
        )
    )
    return path


def test_remote_decisions_is_off_unless_asked_for():
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    args = _console_args("--attend")
    assert _decision_supervisor_from_args(args, None) is None


def test_remote_decisions_refuses_a_read_only_console(tmp_path):
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    args = _console_args(
        "--attend", "--remote-decisions", config=_remote_config(tmp_path)
    )
    with pytest.raises(SystemExit, match="--attend --allow-actions"):
        _decision_supervisor_from_args(args, None)


def test_remote_decisions_refuses_a_deployment_that_did_not_enable_it(tmp_path):
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    args = _console_args(
        "--attend",
        "--allow-actions",
        "--remote-decisions",
        config=_remote_config(tmp_path, enabled=False),
    )
    with pytest.raises(SystemExit, match="human_decisions.remote.enabled"):
        _decision_supervisor_from_args(args, object())


def test_remote_decisions_refuses_without_a_runner_credential(tmp_path, monkeypatch):
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    monkeypatch.delenv("OPENADAPT_RUNNER_TOKEN", raising=False)
    args = _console_args(
        "--attend",
        "--allow-actions",
        "--remote-decisions",
        config=_remote_config(tmp_path),
    )
    with pytest.raises(SystemExit, match="OPENADAPT_RUNNER_TOKEN"):
        _decision_supervisor_from_args(args, object())


def test_remote_decisions_refuses_a_plaintext_control_plane(tmp_path, monkeypatch):
    """A runner credential is never sent over plaintext, even to localhost."""
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    monkeypatch.setenv("OPENADAPT_RUNNER_TOKEN", TOKEN)
    args = _console_args(
        "--attend",
        "--allow-actions",
        "--remote-decisions",
        "--remote-decision-host",
        "http://localhost:3000",
        config=_remote_config(tmp_path),
    )
    with pytest.raises(SystemExit, match="https"):
        _decision_supervisor_from_args(args, object())


def test_remote_decisions_builds_a_supervisor_when_fully_configured(
    tmp_path, monkeypatch
):
    from openadapt_flow.__main__ import _decision_supervisor_from_args

    monkeypatch.setenv("OPENADAPT_RUNNER_TOKEN", TOKEN)
    args = _console_args(
        "--attend",
        "--allow-actions",
        "--remote-decisions",
        "--remote-decision-host",
        "https://app.openadapt.test",
        config=_remote_config(tmp_path),
    )
    supervisor = _decision_supervisor_from_args(args, object())
    assert isinstance(supervisor, DecisionSupervisorThread)
    assert supervisor.running is False


def test_the_console_starts_and_stops_the_lane_with_the_server(monkeypatch):
    """The lane's lifetime is the server's; a stopped console leaves none."""
    import openadapt_flow.console.server as server_mod

    started: list[str] = []
    monkeypatch.setattr(
        server_mod, "create_app", lambda *a, **k: object(), raising=False
    )

    class _FakeUvicorn:
        @staticmethod
        def run(*_args: Any, **_kwargs: Any) -> None:
            started.append("served")

    class _Lane:
        def __init__(self) -> None:
            self.events: list[str] = []

        def start(self) -> None:
            self.events.append("start")

        def stop(self) -> None:
            self.events.append("stop")

    lane = _Lane()
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", _FakeUvicorn)
    monkeypatch.setattr(
        "openadapt_flow.console.app.create_app", lambda *a, **k: object()
    )
    server_mod.serve("bundles", "runs", None, decision_supervisor=lane, port=0)

    assert started == ["served"]
    assert lane.events == ["start", "stop"]

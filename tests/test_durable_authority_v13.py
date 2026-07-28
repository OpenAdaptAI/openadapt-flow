"""External monotonic authority contracts for durable continuation v13.

These tests keep the SQLite authority outside the copyable run directory.  A
restore of local evidence must therefore never restore permission to continue.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openadapt_flow.ir import ActionKind, Step, StepResult, Workflow
from openadapt_flow.runtime.durable.approval import (
    StateDiverged,
    approval_pause_digest,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionStore,
    AttendedDecision,
    AttendedDecisionLog,
    issue_attended_capability,
)
from openadapt_flow.runtime.durable.authority import (
    AUTHORITY_DB_ENV,
    DurableAuthority,
    DurableAuthorityBusy,
)
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)


@pytest.fixture(autouse=True)
def _isolated_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test its own external authority database."""

    monkeypatch.setenv(
        AUTHORITY_DB_ENV,
        str(tmp_path / "external-authority" / "authority.sqlite3"),
    )


def _fresh_run(
    tmp_path: Path,
) -> tuple[
    Path,
    CheckpointStore,
    RunManifest,
    DurableAuthority,
]:
    run_dir = tmp_path / "run"
    bundle_dir = tmp_path / "bundle"
    Workflow(
        name="authority-contract",
        steps=[
            Step(
                id="write-case",
                intent="write the case",
                action=ActionKind.KEY,
                key="ENTER",
            )
        ],
    ).save(bundle_dir)
    store = CheckpointStore(run_dir)
    manifest = RunManifest(
        run_id="run-v13-authority",
        namespace_id="namespace-v13-authority",
        canonical_run_dir=str(run_dir.resolve()),
        workflow_name="authority-contract",
        bundle_dir=str(bundle_dir.resolve()),
        params={"case": "A-100"},
    )
    store.write_fresh_manifest(manifest)
    authority = DurableAuthority(run_dir, store)
    assert authority.validate(manifest).phase == "active"
    return run_dir, store, manifest, authority


def _pause(
    store: CheckpointStore,
    manifest: RunManifest,
    authority: DurableAuthority,
) -> PendingEscalation:
    prior = authority.validate(manifest)
    pending = PendingEscalation(
        run_id=manifest.run_id,
        workflow_name=manifest.workflow_name,
        step_index=0,
        step_id="write-case",
        intent="write the case",
        category="human_required",
        reason="operator review is required",
        proposed_options=["continue", "reject"],
        resume_from_index=0,
        params=dict(manifest.params),
    )
    store.write_pending(pending)
    authority.advance(
        manifest,
        expected_progress_digest=prior.progress_digest,
        phase="paused",
        pause_binding_sha256=approval_pause_digest(pending),
    )
    assert authority.validate(manifest).phase == "paused"
    return pending


def _acquire(
    authority: DurableAuthority,
    manifest: RunManifest,
    pending: PendingEscalation,
    *,
    attempt: str,
    now: datetime,
    ttl_s: float = 60.0,
) -> str:
    owner = f"sha256:owner-{attempt}"
    authority.acquire(
        manifest,
        pause_binding_sha256=approval_pause_digest(pending),
        attempt_id=attempt,
        operation="continue",
        owner_nonce_sha256=owner,
        acquired_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
        now=now,
    )
    return owner


def _snapshot(run_dir: Path, target: Path) -> None:
    shutil.copytree(run_dir, target)


def _restore(run_dir: Path, snapshot: Path) -> None:
    shutil.rmtree(run_dir)
    shutil.copytree(snapshot, run_dir)


def _attended_context(
    tmp_path: Path,
) -> tuple[
    AttendedActionStore,
    DurableAuthority,
    RunManifest,
]:
    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    workflow = Workflow.load(manifest.bundle_dir)
    issue_attended_capability(
        run_dir,
        store=store,
        pending=pending,
        workflow=workflow,
        result=StepResult(
            step_id=pending.step_id,
            intent=pending.intent,
            ok=False,
            error=pending.reason,
        ),
    )
    return AttendedActionStore(run_dir), authority, manifest


def _decision(index: int) -> AttendedDecision:
    return AttendedDecision(
        decision_id=f"decision-{index:04d}",
        pause_id="pause-v13-authority",
        capability_digest="sha256:" + (f"{index:064x}"[-64:]),
        request_digest="sha256:" + (f"{index + 100:064x}"[-64:]),
        idempotency_key=f"authority-request-{index:04d}",
        action="continue",
        operator="operator@example.com",
        status="completed",
        message=f"decision {index} completed",
        report_success=True,
    )


def test_same_path_restore_before_pause_cannot_erase_pause(tmp_path: Path) -> None:
    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    snapshot = tmp_path / "active-snapshot"
    _snapshot(run_dir, snapshot)

    _pause(store, manifest, authority)
    _restore(run_dir, snapshot)

    with pytest.raises(
        DurableAuthorityBusy,
        match="does not match the external monotonic authority",
    ):
        CheckpointStore(run_dir).validate_namespace(manifest)


def test_same_path_restore_cannot_erase_acknowledged_progress(tmp_path: Path) -> None:
    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    snapshot = tmp_path / "paused-snapshot"
    _snapshot(run_dir, snapshot)

    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="progress-attempt",
        now=now,
    )
    authority.before_delivery(
        manifest,
        attempt_id="progress-attempt",
        owner_nonce_sha256=owner,
    )
    store.write_checkpoint(
        RunCheckpoint(
            run_id=manifest.run_id,
            workflow_name=manifest.workflow_name,
            bundle_version="sha256:bundle-v1",
            step_index=0,
            step_id="write-case",
            intent="write the case",
            next_step_index=1,
            params=dict(manifest.params),
            effect_verified=True,
            postconditions_ok=True,
        )
    )
    authority.acknowledge_progress(
        manifest,
        attempt_id="progress-attempt",
        owner_nonce_sha256=owner,
        terminal_pause=False,
    )
    _restore(run_dir, snapshot)

    with pytest.raises(
        DurableAuthorityBusy,
        match="does not match the external monotonic authority",
    ):
        DurableAuthority(run_dir, CheckpointStore(run_dir)).validate(manifest)


def test_observation_only_progress_does_not_require_a_delivery_edge(
    tmp_path: Path,
) -> None:
    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="observation-attempt",
        now=now,
    )
    store.write_checkpoint(
        RunCheckpoint(
            run_id=manifest.run_id,
            workflow_name=manifest.workflow_name,
            bundle_version="sha256:bundle-v1",
            step_index=0,
            step_id="wait-for-ready",
            intent="wait for ready state",
            next_step_index=1,
            params=dict(manifest.params),
            postconditions_ok=True,
        )
    )
    store.clear_pending()

    authority.acknowledge_progress(
        manifest,
        attempt_id="observation-attempt",
        owner_nonce_sha256=owner,
        terminal_pause=False,
    )

    assert authority.validate(manifest).delivery_sequence == 0


def test_same_path_restore_cannot_reopen_completed_run(tmp_path: Path) -> None:
    run_dir, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    snapshot = tmp_path / "paused-snapshot"
    _snapshot(run_dir, snapshot)

    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="terminal-attempt",
        now=now,
    )
    report = run_dir / ".report.terminal-candidate.json"
    report.write_bytes(b'{"outcome":"VERIFIED"}\n')
    report_sha256 = "sha256:" + hashlib.sha256(report.read_bytes()).hexdigest()
    authority.prepare_terminal(
        manifest,
        attempt_id="terminal-attempt",
        owner_nonce_sha256=owner,
        report_sha256=report_sha256,
    )
    store.clear_pending()
    authority.finalize_terminal(
        manifest,
        attempt_id="terminal-attempt",
        owner_nonce_sha256=owner,
        report_sha256=report_sha256,
    )
    _restore(run_dir, snapshot)

    with pytest.raises(DurableAuthorityBusy, match="completed"):
        DurableAuthority(run_dir, CheckpointStore(run_dir)).validate(manifest)


def test_only_one_unexpired_continuation_attempt_can_own_pause(
    tmp_path: Path,
) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    first_owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="attempt-one",
        now=now,
    )

    with pytest.raises(DurableAuthorityBusy, match="already in progress"):
        _acquire(
            authority,
            manifest,
            pending,
            attempt="attempt-two",
            now=now,
        )

    authority.release(
        manifest,
        attempt_id="attempt-one",
        owner_nonce_sha256=first_owner,
    )
    _acquire(
        authority,
        manifest,
        pending,
        attempt="attempt-two",
        now=now,
    )


def test_expired_pre_delivery_attempt_can_be_replaced(tmp_path: Path) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    started = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    first_owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="expired-validating",
        now=started,
        ttl_s=1.0,
    )

    second_owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="replacement",
        now=started + timedelta(seconds=2),
    )
    with pytest.raises(DurableAuthorityBusy, match="no longer owned"):
        authority.before_delivery(
            manifest,
            attempt_id="expired-validating",
            owner_nonce_sha256=first_owner,
        )
    authority.before_delivery(
        manifest,
        attempt_id="replacement",
        owner_nonce_sha256=second_owner,
    )


def test_expired_post_delivery_attempt_requires_reconciliation(
    tmp_path: Path,
) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    started = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    first_owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="expired-delivery",
        now=started,
        ttl_s=1.0,
    )
    authority.before_delivery(
        manifest,
        attempt_id="expired-delivery",
        owner_nonce_sha256=first_owner,
    )

    with pytest.raises(DurableAuthorityBusy, match="reconciliation is required"):
        _acquire(
            authority,
            manifest,
            pending,
            attempt="unsafe-replacement",
            now=started + timedelta(seconds=2),
        )
    with pytest.raises(DurableAuthorityBusy, match="reconciliation_required"):
        authority.validate(manifest)


def test_reject_before_delivery_preempts_attempt(tmp_path: Path) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="pre-delivery-reject",
        now=now,
    )

    assert (
        authority.request_reject(
            expected_run_id=manifest.run_id,
            expected_pause_binding=approval_pause_digest(pending),
        )
        == "preempted"
    )
    with pytest.raises(DurableAuthorityBusy, match="rejected"):
        authority.before_delivery(
            manifest,
            attempt_id="pre-delivery-reject",
            owner_nonce_sha256=owner,
        )


def test_reject_after_delivery_requires_reconciliation(tmp_path: Path) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    pending = _pause(store, manifest, authority)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    owner = _acquire(
        authority,
        manifest,
        pending,
        attempt="post-delivery-reject",
        now=now,
    )
    authority.before_delivery(
        manifest,
        attempt_id="post-delivery-reject",
        owner_nonce_sha256=owner,
    )

    assert (
        authority.request_reject(
            expected_run_id=manifest.run_id,
            expected_pause_binding=approval_pause_digest(pending),
        )
        == "uncertain"
    )
    with pytest.raises(DurableAuthorityBusy, match="reconciliation_required"):
        authority.validate(manifest)


def test_missing_external_authority_fails_closed(tmp_path: Path) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    authority.db_path.unlink()

    with pytest.raises(DurableAuthorityBusy, match="missing"):
        store.validate_namespace(manifest)


def test_corrupt_external_authority_fails_closed(tmp_path: Path) -> None:
    _, store, manifest, authority = _fresh_run(tmp_path)
    authority.db_path.write_bytes(b"not a sqlite database")

    with pytest.raises(StateDiverged, match="authority is unavailable"):
        store.validate_namespace(manifest)


def test_attended_journal_hmac_tamper_fails_closed(tmp_path: Path) -> None:
    _actions, authority, manifest = _attended_context(tmp_path)
    snapshot = AttendedDecisionLog(decisions=[_decision(1)]).model_dump_json(indent=2)
    authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=authority.read_attended_snapshot(
            expected_run_id=manifest.run_id
        )[1],
        snapshot_json=snapshot,
    )
    with sqlite3.connect(authority.db_path) as connection:
        connection.execute(
            "UPDATE attended_journal SET record_mac = ? "
            "WHERE path_key = ? AND sequence = 1",
            ("hmac-sha256:" + ("0" * 64), authority.path_key),
        )

    with pytest.raises(DurableAuthorityBusy, match="HMAC does not verify"):
        authority.read_attended_snapshot(expected_run_id=manifest.run_id)


def test_attended_journal_hash_chain_tamper_fails_closed(tmp_path: Path) -> None:
    _actions, authority, manifest = _attended_context(tmp_path)
    first_decision = _decision(1)
    first = AttendedDecisionLog(decisions=[first_decision]).model_dump_json(indent=2)
    first_head = authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=authority.read_attended_snapshot(
            expected_run_id=manifest.run_id
        )[1],
        snapshot_json=first,
    )
    second = AttendedDecisionLog(
        decisions=[first_decision, _decision(2)]
    ).model_dump_json(indent=2)
    authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=first_head,
        snapshot_json=second,
    )
    with sqlite3.connect(authority.db_path) as connection:
        connection.execute(
            "UPDATE attended_journal SET previous_record_digest = ? "
            "WHERE path_key = ? AND sequence = 2",
            ("sha256:" + ("f" * 64), authority.path_key),
        )

    with pytest.raises(DurableAuthorityBusy, match="chain is invalid"):
        authority.read_attended_snapshot(expected_run_id=manifest.run_id)


def test_deleted_attended_journal_tail_cannot_restore_an_older_snapshot(
    tmp_path: Path,
) -> None:
    _actions, authority, manifest = _attended_context(tmp_path)
    first_decision = _decision(1)
    first = AttendedDecisionLog(decisions=[first_decision]).model_dump_json(indent=2)
    first_head = authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=authority.read_attended_snapshot(
            expected_run_id=manifest.run_id
        )[1],
        snapshot_json=first,
    )
    second = AttendedDecisionLog(
        decisions=[first_decision, _decision(2)]
    ).model_dump_json(indent=2)
    authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=first_head,
        snapshot_json=second,
    )
    with sqlite3.connect(authority.db_path) as connection:
        connection.execute(
            "DELETE FROM attended_journal WHERE path_key = ? AND sequence = 2",
            (authority.path_key,),
        )

    with pytest.raises(DurableAuthorityBusy, match="tail was restored"):
        authority.read_attended_snapshot(expected_run_id=manifest.run_id)


def test_attended_snapshot_must_extend_the_authenticated_log(tmp_path: Path) -> None:
    _actions, authority, manifest = _attended_context(tmp_path)
    first = AttendedDecisionLog(decisions=[_decision(1)]).model_dump_json(indent=2)
    head = authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=authority.read_attended_snapshot(
            expected_run_id=manifest.run_id
        )[1],
        snapshot_json=first,
    )

    with pytest.raises(DurableAuthorityBusy, match="not append-only"):
        authority.append_attended_snapshot(
            expected_run_id=manifest.run_id,
            expected_head_digest=head,
            snapshot_json=AttendedDecisionLog().model_dump_json(indent=2),
        )


def test_authority_database_cannot_be_restored_with_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    monkeypatch.setenv(AUTHORITY_DB_ENV, str(run_dir / "authority.sqlite3"))

    with pytest.raises(DurableAuthorityBusy, match="outside the run directory"):
        DurableAuthority(run_dir, CheckpointStore(run_dir))


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation needs privilege")
def test_attended_journal_key_must_not_be_a_symlink(tmp_path: Path) -> None:
    _actions, authority, manifest = _attended_context(tmp_path)
    target = tmp_path / "attacker-key"
    target.write_bytes(b"x" * 32)
    target.chmod(0o600)
    key_path = authority.db_path.with_name(authority.db_path.name + ".journal-key")
    key_path.symlink_to(target)
    snapshot = AttendedDecisionLog(decisions=[_decision(1)]).model_dump_json(indent=2)

    with pytest.raises(DurableAuthorityBusy, match="key is unavailable"):
        authority.append_attended_snapshot(
            expected_run_id=manifest.run_id,
            expected_head_digest=authority.read_attended_snapshot(
                expected_run_id=manifest.run_id
            )[1],
            snapshot_json=snapshot,
        )


def test_local_attended_journal_rollback_is_repaired_from_authority(
    tmp_path: Path,
) -> None:
    actions, _authority, _manifest = _attended_context(tmp_path)
    actions.append(_decision(1))
    first_projection = actions.decisions_path.read_bytes()
    actions.append(_decision(2))
    authoritative_projection = actions.decisions_path.read_bytes()
    assert first_projection != authoritative_projection

    actions.decisions_path.write_bytes(first_projection)
    repaired = actions._read_log()  # noqa: SLF001 - test the repair seam

    assert [entry.decision_id for entry in repaired.decisions] == [
        "decision-0001",
        "decision-0002",
    ]
    assert actions.decisions_path.read_bytes() == authoritative_projection


def test_deleted_local_attended_projection_is_repaired_from_authority(
    tmp_path: Path,
) -> None:
    actions, _authority, _manifest = _attended_context(tmp_path)
    decision = _decision(1)
    actions.append(decision)
    authoritative_projection = actions.decisions_path.read_bytes()
    actions.decisions_path.unlink()

    repaired = actions._read_log()  # noqa: SLF001 - test the repair seam

    assert repaired.decisions == [decision]
    assert actions.decisions_path.read_bytes() == authoritative_projection


def test_two_external_attended_writers_use_head_compare_and_swap(
    tmp_path: Path,
) -> None:
    actions, first_writer, manifest = _attended_context(tmp_path)
    second_writer = DurableAuthority(
        actions.run_dir,
        CheckpointStore(actions.run_dir),
    )
    _snapshot_one, shared_head = first_writer.read_attended_snapshot(
        expected_run_id=manifest.run_id
    )
    assert (
        second_writer.read_attended_snapshot(expected_run_id=manifest.run_id)[1]
        == shared_head
    )

    first_decision = _decision(1)
    first_snapshot = AttendedDecisionLog(decisions=[first_decision]).model_dump_json(
        indent=2
    )
    first_head = first_writer.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=shared_head,
        snapshot_json=first_snapshot,
    )
    stale_second_snapshot = AttendedDecisionLog(
        decisions=[_decision(2)]
    ).model_dump_json(indent=2)
    with pytest.raises(DurableAuthorityBusy, match="changed before append"):
        second_writer.append_attended_snapshot(
            expected_run_id=manifest.run_id,
            expected_head_digest=shared_head,
            snapshot_json=stale_second_snapshot,
        )

    merged_snapshot = AttendedDecisionLog(
        decisions=[first_decision, _decision(2)]
    ).model_dump_json(indent=2)
    second_head = second_writer.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=first_head,
        snapshot_json=merged_snapshot,
    )
    retained, retained_head = first_writer.read_attended_snapshot(
        expected_run_id=manifest.run_id
    )
    assert retained == merged_snapshot
    assert retained_head == second_head


def test_crash_after_external_append_repairs_missing_local_projection(
    tmp_path: Path,
) -> None:
    actions, authority, manifest = _attended_context(tmp_path)
    decision = _decision(1)
    snapshot = AttendedDecisionLog(decisions=[decision]).model_dump_json(indent=2)
    _prior_snapshot, prior_head = authority.read_attended_snapshot(
        expected_run_id=manifest.run_id
    )

    # This is the authority-first crash window: SQLite commits, then the process
    # exits before it can publish attended_decisions.json.
    authority.append_attended_snapshot(
        expected_run_id=manifest.run_id,
        expected_head_digest=prior_head,
        snapshot_json=snapshot,
    )
    assert not actions.decisions_path.exists()

    repaired = actions._read_log()  # noqa: SLF001 - test restart recovery

    assert repaired.decisions == [decision]
    assert actions.decisions_path.read_text() == snapshot

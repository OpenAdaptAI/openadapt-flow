"""Behavioral contract for the real-RDP visual fault campaign."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_campaign_native_application_identity_is_a_valid_environment_boundary() -> None:
    """The qualified RDP application id must be executable, not display copy."""

    from benchmark.rdp_multiapp.run_qualification import (
        APPLICATION_IDENTITY,
        APPLICATION_VERSION,
        ENVIRONMENT_MARKER,
    )
    from openadapt_flow.qualification import EnvironmentBoundary
    from openadapt_flow.qualification_environment import (
        BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256,
    )

    boundary = EnvironmentBoundary(
        target_kind="rdp",
        application="RDP multi-window synthetic suite",
        application_identity=APPLICATION_IDENTITY,
        application_version=APPLICATION_VERSION,
        environment_observer_id=(
            "backend:openadapt_flow.backends.rdp_backend.FreeRDPBackend"
        ),
        environment_observer_contract_sha256=(
            BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256
        ),
        environment_digest="a" * 64,
        runtime_version="test",
        required_capabilities=[ENVIRONMENT_MARKER],
    )

    assert boundary.expected_application_identity == APPLICATION_IDENTITY


def test_campaign_session_identity_is_a_non_sensitive_client_window_digest() -> None:
    from benchmark.rdp_multiapp.run_qualification import MultiappRdpTransport

    class Transport(MultiappRdpTransport):
        def _client_window_id(self) -> int:
            return 4242

    transport = Transport("fixture", display=":9")

    observed = transport.session_identity()

    assert len(observed) == 64
    assert set(observed) <= set("0123456789abcdef")


def test_failure_artifact_exports_only_exact_failed_step_frames(tmp_path: Path) -> None:
    from benchmark.rdp_multiapp.run_qualification import _export_failed_step_frames

    run_dir = tmp_path / "run-healthy-1"
    steps = run_dir / "steps"
    steps.mkdir(parents=True)
    (steps / "step_008_before.png").write_bytes(b"before")
    (steps / "step_008_after.png").write_bytes(b"after")
    (steps / "step_007_before.png").write_bytes(b"unrelated")
    (run_dir / "report.json").write_text('{"not": "exported"}', encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    exported = _export_failed_step_frames(
        artifact_root=artifact_root,
        run_dir=run_dir,
        failed_step_ids=["step_008"],
    )

    assert [item["kind"] for item in exported] == [
        "failed_step_before_frame",
        "failed_step_after_frame",
    ]
    assert [item["path"] for item in exported] == [
        "failure/run-healthy-1/step_008_before.png",
        "failure/run-healthy-1/step_008_after.png",
    ]
    assert sorted(
        path.relative_to(artifact_root).as_posix()
        for path in artifact_root.rglob("*")
        if path.is_file()
    ) == [
        "failure/run-healthy-1/step_008_after.png",
        "failure/run-healthy-1/step_008_before.png",
    ]


def test_failure_artifact_omits_runtime_pseudo_steps_without_frames(
    tmp_path: Path,
) -> None:
    from benchmark.rdp_multiapp.run_qualification import _export_failed_step_frames

    run_dir = tmp_path / "run-authorization-refusal"
    run_dir.mkdir()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    assert _export_failed_step_frames(
        artifact_root=artifact_root,
        run_dir=run_dir,
        failed_step_ids=["<authorization>"],
    ) == [
        {
            "kind": "failed_step_frame_unavailable",
            "step_id": "<authorization>",
            "reason": "runtime_pseudo_step_has_no_retained_frame",
        }
    ]
    assert list(artifact_root.iterdir()) == []


def test_failure_artifact_records_missing_retained_action_frame(tmp_path: Path) -> None:
    from benchmark.rdp_multiapp.run_qualification import _export_failed_step_frames

    run_dir = tmp_path / "run-incomplete-media"
    steps = run_dir / "steps"
    steps.mkdir(parents=True)
    (steps / "step_008_before.png").write_bytes(b"before")
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    exported = _export_failed_step_frames(
        artifact_root=artifact_root,
        run_dir=run_dir,
        failed_step_ids=["step_008"],
    )

    assert exported[0]["kind"] == "failed_step_before_frame"
    assert exported[1] == {
        "kind": "failed_step_frame_unavailable",
        "step_id": "step_008",
        "phase": "after",
        "reason": "retained_frame_not_found",
    }


def test_visual_campaign_has_repeated_trials_and_business_oracles() -> None:
    campaign = json.loads(
        (ROOT / "benchmark/rdp_multiapp/campaign.json").read_text(encoding="utf-8")
    )
    conditions = campaign["conditions"]

    assert campaign["trials_per_condition"] >= 3
    assert len({condition["id"] for condition in conditions}) == len(conditions)
    assert {"sqlite", "csv", "maildir"} == {
        oracle for condition in conditions for oracle in condition["oracle"]
    }
    assert {
        "silent_incorrect_successes",
        "over_halts",
        "wrong_record_writes",
        "duplicate_effects",
    } <= set(campaign["required_metrics"])

    by_id = {condition["id"]: condition for condition in conditions}
    assert by_id["row_reordered"]["expect"] == "verified"
    assert by_id["wrong_record_before_write"]["expect"] == "safe_halt"
    assert by_id["focus_theft_before_write"]["expect"] == "safe_halt"
    assert by_id["duplicate_save_control"] == {
        "id": "duplicate_save_control",
        "expect": "safe_halt",
        "fault": "A competing Save appointment control appears in the same visual state.",
        "oracle": ["sqlite"],
    }
    assert by_id["partial_render"] == {
        "id": "partial_render",
        "expect": "safe_halt",
        "fault": "The target window is incomplete when the next action is due.",
        "oracle": ["sqlite", "csv", "maildir"],
    }
    assert by_id["commit_then_timeout"]["oracle"] == ["sqlite"]


def test_campaign_completion_requires_every_trial_for_every_condition() -> None:
    from benchmark.rdp_multiapp.run_qualification import _campaign_coverage

    conditions = ("healthy", "drift")
    incomplete = _campaign_coverage(
        conditions,
        [{"condition": "healthy"}],
        required_trials=3,
    )
    assert incomplete == {
        "configured_conditions": ["healthy", "drift"],
        "implemented_conditions": ["healthy"],
        "condition_trial_counts": {"healthy": 1, "drift": 0},
        "full_campaign_complete": False,
        "full_campaign_pending_conditions": ["healthy", "drift"],
    }

    complete = _campaign_coverage(
        conditions,
        [
            *({"condition": "healthy"} for _ in range(3)),
            *({"condition": "drift"} for _ in range(3)),
        ],
        required_trials=3,
    )
    assert complete["full_campaign_complete"] is True
    assert complete["full_campaign_pending_conditions"] == []


def test_commit_then_timeout_fault_raises_only_after_one_real_save_delivery() -> None:
    from benchmark.rdp_multiapp.run_qualification import (
        _install_commit_then_timeout_fault,
    )
    from openadapt_flow.backend import ActionDeliveryUncertain
    from openadapt_flow.ir import ActionDeliveryReceipt

    class Backend:
        def __init__(self) -> None:
            self.calls = 0

        def click_guarded(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            return ActionDeliveryReceipt(
                receipt_id="real-save-delivery",
                operation="rdp_click",
                native=False,
                target_fingerprint="a" * 64,
                delivered_at="2026-08-02T00:00:00+00:00",
            )

    backend = Backend()
    state = _install_commit_then_timeout_fault(
        backend,
        condition="commit_then_timeout",
        save_region=(0, 0, 100, 100),
    )

    try:
        backend.click_guarded(10, 20, expected_frame_sha256="0" * 64)
    except ActionDeliveryUncertain as exc:
        assert exc.operation == "rdp_click"
        assert exc.cause_type == "TimeoutError"
        assert exc.target_fingerprint == "a" * 64
    else:  # pragma: no cover - the assertion describes the fault contract
        raise AssertionError("the post-delivery timeout was not injected")

    assert backend.calls == 1
    assert state == {"injected": True, "save_delivery_calls": 1}

    backend.click_guarded(10, 20, expected_frame_sha256="0" * 64)
    assert backend.calls == 2
    assert state == {"injected": True, "save_delivery_calls": 2}

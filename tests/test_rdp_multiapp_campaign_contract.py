"""Behavioral contract for the real-RDP visual fault campaign."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


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
    assert by_id["commit_then_timeout"]["oracle"] == ["sqlite"]


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
        save_pointer_acquisition=7,
        acquisition_count=lambda: 7,
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

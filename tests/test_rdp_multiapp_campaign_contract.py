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

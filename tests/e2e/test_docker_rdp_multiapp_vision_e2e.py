"""Opt-in real-RDP multi-window visual workflow qualification."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from benchmark.rdp_multiapp.run_qualification import run


def test_real_rdp_multiapp_visual_subset(tmp_path: Path) -> None:
    container = os.getenv("OPENADAPT_RDP_MULTIAPP_CONTAINER")
    oracle = os.getenv("OPENADAPT_RDP_MULTIAPP_ORACLE_ROOT")
    if not container or not oracle:
        pytest.skip(
            "set OPENADAPT_RDP_MULTIAPP_CONTAINER and "
            "OPENADAPT_RDP_MULTIAPP_ORACLE_ROOT for the real-RDP fixture"
        )

    output = tmp_path / "results.json"
    result = run(container, Path(oracle), output, tmp_path / "work")

    assert result["accepted_subset"] is True
    assert result["run_count"] == 12
    assert result["silent_incorrect_successes"] == 0
    assert result["over_halts"] == 0
    assert result["model_calls"] == 0
    assert result["full_campaign_complete"] is False
    assert json.loads(output.read_text()) == result

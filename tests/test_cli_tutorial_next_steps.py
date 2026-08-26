"""The closing "next steps" block after a plain VERIFIED tutorial run.

Browser-free, mirroring the rejected-write CLI tests: the
tutorial's heavy loop is faked and only the CLI wiring is proven here.

* :func:`openadapt_flow.tutorial._next_steps_block` carries the real
  record, compile, inspect, lint, replay, and qualification path;
* the plain VERIFIED tutorial prints the block after the receipt paths;
* an advanced rejected-write simulation and a non-VERIFIED run do not print
  it because the block belongs to the primary success rail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow import tutorial as tutorial_module
from openadapt_flow.__main__ import main
from openadapt_flow.tutorial import (
    BreakItResult,
    TutorialResult,
    _next_steps_block,
)

QUALIFY_URL = "https://openadapt.ai/qualify"


def _result(
    root: Path,
    *,
    execution_outcome: str = "VERIFIED",
    break_it: Optional[BreakItResult] = None,
) -> TutorialResult:
    verified = execution_outcome == "VERIFIED"
    return TutorialResult(
        recording_dir=root / "recording",
        bundle_dir=root / "bundle",
        run_dir=root / "run",
        execution_outcome=execution_outcome,
        transaction_outcome=execution_outcome,
        execution_profile="standard",
        transaction_billable=verified,
        model_calls=0,
        effects_required=2,
        effects_confirmed=2 if verified else 0,
        effect_tier=1 if verified else None,
        bundle_digest="d" * 64,
        system_of_record_records=1 if verified else 0,
        receipt_paths=(
            {
                "png": root / "run" / "receipt.png",
                "json": root / "run" / "receipt.json",
            }
            if verified
            else {}
        ),
        break_it=break_it,
    )


def _broken_result(root: Path) -> BreakItResult:
    return BreakItResult(
        run_dir=root / "run-rejected-write",
        report_path=root / "run-rejected-write" / "REPORT.md",
        fault="optimistic",
        execution_outcome="HALTED",
        transaction_outcome="RECONCILIATION_REQUIRED",
        transaction_billable=False,
        screen_claimed_success=True,
        screen_claim_text="Encounter saved",
        effects_required=2,
        effects_refuted=1,
        halt_reason="record_written refuted against the rest system of record",
        system_of_record_records=0,
    )


def _wire(monkeypatch: pytest.MonkeyPatch, result_for: Any) -> None:
    def fake_run_tutorial(work_dir: Path, **kwargs: Any) -> TutorialResult:
        return result_for(Path(work_dir), kwargs)

    monkeypatch.setattr(tutorial_module, "run_tutorial", fake_run_tutorial)


def test_next_steps_block_carries_the_real_first_workflow() -> None:
    block = _next_steps_block()
    assert "openadapt-flow record --backend web" in block
    assert "openadapt-flow compile recording" in block
    assert "openadapt-flow visualize bundle" in block
    assert "openadapt-flow lint bundle" in block
    assert "openadapt-flow replay bundle" in block
    assert QUALIFY_URL in block
    assert "identity, effect, and policy evidence" in block
    assert "--simulate-rejected-write" not in block
    assert "--break-it" not in block


def test_verified_tutorial_prints_the_block_after_the_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _wire(monkeypatch, lambda root, kwargs: _result(root))
    assert main(["tutorial", "--out", str(tmp_path / "t")]) == 0

    out = capsys.readouterr().out
    assert _next_steps_block() in out
    # After the receipt paths, at the very end of the run's story.
    assert out.index("receipt.json") < out.index("openadapt-flow record")


def test_rejected_write_simulation_does_not_print_the_first_workflow_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _wire(
        monkeypatch,
        lambda root, kwargs: _result(root, break_it=_broken_result(root)),
    )
    assert (
        main(
            [
                "tutorial",
                "--simulate-rejected-write",
                "--out",
                str(tmp_path / "t"),
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert QUALIFY_URL not in out
    assert "openadapt-flow record --backend web" not in out


def test_unverified_tutorial_does_not_print_the_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _wire(
        monkeypatch,
        lambda root, kwargs: _result(root, execution_outcome="HALTED"),
    )
    assert main(["tutorial", "--out", str(tmp_path / "t")]) == 1

    out = capsys.readouterr().out
    assert QUALIFY_URL not in out
    assert "openadapt-flow record --backend web" not in out

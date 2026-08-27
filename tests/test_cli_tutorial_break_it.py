"""CLI wiring for the advanced rejected-write verification fixture.

Browser-free: the tutorial's heavy loop is faked, and the real end-to-end
behavior (record, run, halt) is owned by ``tests/e2e/test_tutorial_break_it_e2e.py``.
What is proven here, cheaply and on every unit run:

* ``--simulate-rejected-write`` reaches
  :func:`openadapt_flow.tutorial.run_tutorial` and its result
  drives the printed narrative -- screen claim, refuted verifier read, HALTED
  outcome, evidence path, and the no-receipt rule -- from run evidence, not
  from a script;
* the plain tutorial points at a real first workflow, never this fixture;
* the deprecated ``--break-it`` alias stays hidden and warns on stderr;
* :func:`openadapt_flow.tutorial._run_break_it` raises loudly when the engine
  does NOT halt on the injected fault, and extracts the narrative facts from a
  halted report when it does.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import openadapt_flow.mockmed.fault_server as fault_server_module
import openadapt_flow.report as report_module
from openadapt_flow import tutorial as tutorial_module
from openadapt_flow.__main__ import main
from openadapt_flow.tutorial import (
    BreakItResult,
    TutorialError,
    TutorialResult,
    _run_break_it,
)

# ---------------------------------------------------------------------------
# CLI narrative
# ---------------------------------------------------------------------------


def _verified_result(
    root: Path, *, break_it: Optional[BreakItResult]
) -> TutorialResult:
    return TutorialResult(
        recording_dir=root / "recording",
        bundle_dir=root / "bundle",
        run_dir=root / "run",
        execution_outcome="VERIFIED",
        transaction_outcome="VERIFIED",
        execution_profile="standard",
        transaction_billable=True,
        model_calls=0,
        effects_required=2,
        effects_confirmed=2,
        effect_tier=1,
        bundle_digest="d" * 64,
        system_of_record_records=1,
        receipt_paths={
            "png": root / "run" / "receipt.png",
            "json": root / "run" / "receipt.json",
        },
        break_it=break_it,
    )


def _broken_result(
    root: Path, *, run_dir_name: str = "run-rejected-write"
) -> BreakItResult:
    run_dir = root / run_dir_name
    return BreakItResult(
        run_dir=run_dir,
        report_path=run_dir / "REPORT.md",
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
        rejected_writes=1,
    )


def test_simulate_rejected_write_reaches_tutorial_and_drives_the_narrative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_tutorial(work_dir: Path, **kwargs: Any) -> TutorialResult:
        seen.update(kwargs)
        return _verified_result(Path(work_dir), break_it=_broken_result(Path(work_dir)))

    monkeypatch.setattr(tutorial_module, "run_tutorial", fake_run_tutorial)
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
    assert seen["simulate_rejected_write"] is True
    assert seen["break_it"] is False

    out = capsys.readouterr().out
    # The two runs are labeled so VERIFIED is never misread as the broken one.
    assert "clean run" in out
    assert "rejected-write verification" in out
    # The narrative's three beats, from evidence fields.
    assert "every on-screen check passed" in out
    assert '"Encounter saved"' in out
    assert "1/2 declared effect(s) REFUTED" in out
    assert "0 record(s)" in out
    assert "1 rejected write attempt (no retry)" in out
    assert "HALTED at the consequential step" in out
    assert "RECONCILIATION_REQUIRED" in out
    # The engine's own halt reason is quoted, not paraphrased.
    assert "record_written refuted" in out
    # Where the evidence lives, and what may NOT be claimed.
    assert str(tmp_path / "t" / "run-rejected-write" / "REPORT.md") in out
    assert "NOT a success receipt" in out
    assert "No shareable receipt for the halted run" in out
    assert "openadapt-flow tutorial --break-it" not in out


def test_plain_tutorial_points_at_a_real_first_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_tutorial(work_dir: Path, **kwargs: Any) -> TutorialResult:
        seen.update(kwargs)
        return _verified_result(Path(work_dir), break_it=None)

    monkeypatch.setattr(tutorial_module, "run_tutorial", fake_run_tutorial)
    assert main(["tutorial", "--out", str(tmp_path / "t")]) == 0
    assert seen["simulate_rejected_write"] is False
    assert seen["break_it"] is False

    out = capsys.readouterr().out
    assert "REPORT.md" in out
    assert "receipt.json" in out
    assert "one small read-only task with test data" in out
    assert "openadapt-flow record --backend web" in out
    assert "openadapt-flow compile recording" in out
    assert "openadapt-flow visualize bundle" in out
    assert "openadapt-flow lint bundle" in out
    assert "openadapt-flow replay bundle" in out
    assert "--run-dir first-run" in out
    assert "first-run/REPORT.md" in out
    assert "before Flow first actuates it" in out
    assert out.index("openadapt-flow lint bundle") < out.index(
        "before Flow first actuates it"
    )
    assert out.index("before Flow first actuates it") < out.index(
        "openadapt-flow replay bundle"
    )
    assert "--simulate-rejected-write" not in out
    assert "--break-it" not in out


def test_rejected_write_help_hides_the_deprecated_alias(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["tutorial", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--simulate-rejected-write" in out
    assert "--break-it" not in out


def test_deprecated_break_it_alias_warns_and_runs_the_same_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_tutorial(work_dir: Path, **kwargs: Any) -> TutorialResult:
        seen.update(kwargs)
        return _verified_result(
            Path(work_dir),
            break_it=_broken_result(Path(work_dir), run_dir_name="run-broken"),
        )

    monkeypatch.setattr(tutorial_module, "run_tutorial", fake_run_tutorial)
    assert main(["tutorial", "--break-it", "--out", str(tmp_path / "t")]) == 0
    assert seen["simulate_rejected_write"] is False
    assert seen["break_it"] is True

    captured = capsys.readouterr()
    assert "rejected-write verification" in captured.out
    assert str(tmp_path / "t" / "run-broken" / "REPORT.md") in captured.out
    assert captured.err == (
        "warning: --break-it is deprecated; use --simulate-rejected-write.\n"
    )


# ---------------------------------------------------------------------------
# _run_break_it: fail loud, extract honestly
# ---------------------------------------------------------------------------


def _fake_workflow() -> Any:
    effect = SimpleNamespace(
        kind=SimpleNamespace(value="record_written"),
        needs_operator_confirmation=False,
    )
    step = SimpleNamespace(id="step_005", risk="irreversible", effects=[effect])
    return SimpleNamespace(steps=[step])


def _wire_break_it_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    report: Any,
    *,
    record_count: int = 0,
    rejected_writes: int = 1,
) -> None:
    fault_db = SimpleNamespace(
        snapshot=lambda: {"rejected_writes": rejected_writes}
    )
    monkeypatch.setattr(
        fault_server_module,
        "serve",
        lambda: ("http://127.0.0.1:9/", fault_db, lambda: None),
    )
    monkeypatch.setattr(
        tutorial_module,
        "_records",
        lambda base_url: [{} for _ in range(record_count)],
    )
    monkeypatch.setattr(
        tutorial_module, "run_tutorial_workflow", lambda **kwargs: report
    )
    monkeypatch.setattr(
        report_module,
        "render_run_report",
        lambda run_dir: Path(run_dir) / "REPORT.md",
    )


def test_run_break_it_refuses_an_uncaught_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected fault the engine misses is a product failure, not a demo."""

    report = SimpleNamespace(execution_outcome="VERIFIED")
    _wire_break_it_fakes(monkeypatch, tmp_path, report)
    with pytest.raises(TutorialError, match="FAILED to catch the injected fault"):
        _run_break_it(
            workflow=_fake_workflow(),
            bundle_dir=tmp_path / "bundle",
            run_dir=tmp_path / "run-rejected-write",
            headed=False,
            say=lambda message: None,
        )


def _halted_report() -> Any:
    save_result = SimpleNamespace(
        step_id="step_005",
        postconditions_ok=True,
        effect_evidence=[SimpleNamespace(final_verdict="refuted")],
    )
    return SimpleNamespace(
        execution_outcome="HALTED",
        transaction_outcome="RECONCILIATION_REQUIRED",
        transaction_billable=False,
        results=[save_result],
        halt=SimpleNamespace(
            observed_texts=["MockMed", "Encountersaved-"],
            reason="record_written refuted -- nothing landed",
        ),
        outcome_envelope=SimpleNamespace(
            required_contracts=SimpleNamespace(effect=2)
        ),
    )


def test_run_break_it_extracts_the_narrative_from_the_halted_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _halted_report()
    _wire_break_it_fakes(monkeypatch, tmp_path, report)

    broken = _run_break_it(
        workflow=_fake_workflow(),
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run-rejected-write",
        headed=False,
        say=lambda message: None,
    )
    assert broken.execution_outcome == "HALTED"
    assert broken.fault == "optimistic"
    assert broken.screen_claimed_success is True
    assert broken.screen_claim_text == "Encountersaved-"
    assert broken.effects_required == 2
    assert broken.effects_refuted == 1
    assert broken.system_of_record_records == 0
    assert broken.rejected_writes == 1
    assert broken.halt_reason == "record_written refuted -- nothing landed"
    assert broken.report_path == tmp_path / "run-rejected-write" / "REPORT.md"


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_transaction_outcome",
        "billable",
        "unknown_billing",
        "missing_consequential_result",
        "screen_postcondition_failed",
        "effect_not_refuted",
        "effect_not_required",
        "unrelated_halt_reason",
        "missing_halt",
        "record_persisted",
        "no_rejected_write",
        "retried_rejected_write",
    ],
)
def test_run_break_it_requires_the_complete_rejected_write_contract(
    defect: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _halted_report()
    record_count = 0
    rejected_writes = 1

    if defect == "wrong_transaction_outcome":
        report.transaction_outcome = "HALTED"
    elif defect == "billable":
        report.transaction_billable = True
    elif defect == "unknown_billing":
        report.transaction_billable = None
    elif defect == "missing_consequential_result":
        report.results = []
    elif defect == "screen_postcondition_failed":
        report.results[0].postconditions_ok = False
    elif defect == "effect_not_refuted":
        report.results[0].effect_evidence[0].final_verdict = "confirmed"
    elif defect == "effect_not_required":
        report.outcome_envelope.required_contracts.effect = 0
    elif defect == "unrelated_halt_reason":
        report.halt.reason = "the browser closed"
    elif defect == "missing_halt":
        report.halt = None
    elif defect == "record_persisted":
        record_count = 1
    elif defect == "no_rejected_write":
        rejected_writes = 0
    elif defect == "retried_rejected_write":
        rejected_writes = 2
    else:  # pragma: no cover - the parametrization owns this value.
        raise AssertionError(f"unknown defect: {defect}")

    _wire_break_it_fakes(
        monkeypatch,
        tmp_path,
        report,
        record_count=record_count,
        rejected_writes=rejected_writes,
    )
    with pytest.raises(TutorialError, match="FAILED its evidence contract"):
        _run_break_it(
            workflow=_fake_workflow(),
            bundle_dir=tmp_path / "bundle",
            run_dir=tmp_path / "run-rejected-write",
            headed=False,
            say=lambda message: None,
        )

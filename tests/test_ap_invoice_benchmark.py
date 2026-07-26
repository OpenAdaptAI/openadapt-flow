"""CI guard for the AP invoice multi-system benchmark.

Runs the REAL harness once (n=1; deterministic, localhost only, zero model
calls) and pins the qualitative claims the committed
``benchmark/ap_invoice/results.json`` publishes, so they can never silently
regress:

- the healthy path is a 30+ action, 2-application, branching workflow that
  the governed standard-profile stack classifies ``VERIFIED`` (email
  confirmation verified by reading the outbox maildir; payments via the REST
  oracle; ERP rows via read-only SQL) with ZERO model calls;
- the missing-PO and ambiguous-duplicate intakes HALT safely with no
  persisted effect (``HALTED_BEFORE_EFFECT``);
- the collateral adjacent-record overwrite is CAUGHT by the governed arm and
  silently accepted by the banner-oracle arm (the honest differentiator);
- the payment-confirmation outage routes to ``RECONCILIATION_REQUIRED`` and a
  retry under the same idempotency key is SUPPRESSED (``REJECTED_POLICY``),
  never double-paid;
- the independent ground truth (direct SQLite + maildir reads) agrees.
"""

from __future__ import annotations

import pytest

from benchmark.ap_invoice.run import ARMS, SCENARIOS, run_benchmark


@pytest.fixture(scope="module")
def results() -> dict:
    return run_benchmark(n=1, log=lambda _m: None)


def _rows(results: dict, arm: str, scenario: str) -> list[dict]:
    return [r for r in results["runs"] if r["arm"] == arm and r["scenario"] == scenario]


def test_every_scenario_ran_under_both_arms(results):
    for arm in ARMS:
        for scenario in SCENARIOS:
            assert _rows(results, arm, scenario), (arm, scenario)


def test_zero_model_calls_everywhere(results):
    assert all(int(r["model_calls"] or 0) == 0 for r in results["runs"])


def test_every_executed_step_used_the_api_tier(results):
    for r in results["runs"]:
        if r["executed_action_steps"]:
            assert r["actuation_kinds"] == ["api"], r


def test_healthy_path_is_long_branching_and_multi_row(results):
    for arm in ARMS:
        row = _rows(results, arm, "healthy")[0]
        assert row["worklist_rows"] == 5
        assert 25 <= row["executed_action_steps"] <= 60
        assert row["reported_success"] is True
        assert row["gt_correct"] is True


def test_governed_healthy_run_is_verified_and_billable(results):
    row = _rows(results, "governed", "healthy")[0]
    assert row["transaction_outcome"] == "VERIFIED"
    assert row["transaction_billable"] is True
    assert row["execution_outcome"] == "VERIFIED"


def test_naive_healthy_run_is_never_a_production_success(results):
    row = _rows(results, "naive", "healthy")[0]
    assert row["transaction_outcome"] == "COMPLETED_UNVERIFIED"
    assert row["transaction_billable"] is False


@pytest.mark.parametrize("scenario", ["missing_po", "duplicate_invoice"])
def test_entry_exceptions_halt_safely_with_no_effect(results, scenario):
    for arm in ARMS:
        row = _rows(results, arm, scenario)[0]
        assert row["halted"] is True
        assert row["gt_correct"] is True, row["gt_violations"]
        assert row["transaction_outcome"] == "HALTED_BEFORE_EFFECT"
        # Nothing consequential persisted anywhere (echo banner excluded).
        assert all(delta == 0 for delta in row["table_deltas"].values()), row


def test_collateral_overwrite_is_caught_by_governed_and_silent_under_naive(results):
    naive = _rows(results, "naive", "collateral_approve")[0]
    governed = _rows(results, "governed", "collateral_approve")[0]
    assert naive["silent_wrong"] is True
    assert "adjacent_invoice_modified" in naive["gt_violations"]
    assert governed["caught"] is True
    assert governed["silent_wrong"] is False
    assert governed["transaction_outcome"] == "RECONCILIATION_REQUIRED"


def test_uncertain_payment_routes_to_reconciliation_and_never_double_pays(results):
    governed = _rows(results, "governed", "payment_confirm_outage")[0]
    assert governed["transaction_outcome"] == "RECONCILIATION_REQUIRED"
    assert governed["halted"] is True
    # The retry under the same idempotency key was suppressed, not re-actuated.
    assert governed["retry_suppressed"] is True
    assert governed["retry_transaction_outcome"] == "REJECTED_POLICY"
    # Ground truth: exactly one payment landed; no duplicate.
    assert governed["gt_correct"] is True, governed["gt_violations"]


def test_headline_silent_wrong_counts(results):
    per_arm = results["metrics"]["per_arm"]
    assert per_arm["governed"]["silent_wrong"] == 0
    assert per_arm["governed"]["over_halts"] == 0
    assert per_arm["naive"]["silent_wrong"] >= 1

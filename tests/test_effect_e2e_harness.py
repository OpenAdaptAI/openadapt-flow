"""CI guard for the end-to-end silent-wrong-effect (SWER) harness.

Runs a small N of the REAL harness (no model calls, localhost only) and asserts
the qualitative claims the harness exists to publish, so the measured numbers
can never silently regress:

- every write goes through the REAL replayer's api tier (not raw requests);
- **screen-verify** has a large NONZERO end-to-end silent-wrong-effect rate on
  the 2xx-but-wrong persistence faults (the banner accepts wrong writes);
- **effect-verify (REST record oracle)** drives that rate WAY down by reading
  the record out of band -- but does NOT reach zero: the collateral write to an
  unaudited surface slips its read path (the honest structural limit); and
- **effect-verify (complete SQL read path)** closes that gap and reaches zero.

It also asserts the THREE paths are actually distinct (the anti-circularity
property the older definitional benchmark lacks): the ground truth reads the
sqlite file directly and disagrees with the screen arm on the wrong-effect
faults, and each arm's decision is the replayer's real halt/pass.
"""

from __future__ import annotations

import pytest

from benchmark.effect_e2e.run import (
    ARMS,
    SCENARIOS,
    aggregate,
    render_markdown,
    run_benchmark,
)


@pytest.fixture(scope="module")
def results() -> dict:
    # Run the real end-to-end harness ONCE for the module (N=1 fixes each
    # deterministic per-class verdict); every test reads this shared result.
    return run_benchmark(n=1, log=lambda _m: None)


@pytest.fixture(scope="module")
def per_arm(results) -> dict:
    return aggregate(results["runs"])["per_arm"]


# The 2xx-but-wrong classes a screen oracle silently accepts.
SILENT_UNDER_SCREEN = {
    "no_persist",
    "partial",
    "duplicate",
    "wrong_record",
    "stale",
    "collateral_unaudited",
}
# The class the encounters-scoped REST record oracle structurally cannot catch.
SLIPS_THROUGH_REST = "collateral_unaudited"


def test_write_goes_through_the_real_replayer_api_tier(results):
    # Every non-fall-through run was actuated by the replayer's api tier
    # (actuation == "api"), i.e. through ApiActuator, never raw requests.
    api_runs = [r for r in results["runs"] if r["actuation"] == "api"]
    # All 2xx and rejection faults are api-tier; only a truly unavailable
    # endpoint would fall through, which never happens here.
    assert len(api_runs) == len(results["runs"]), "some run bypassed the api tier"


def test_screen_has_large_nonzero_silent_rate(per_arm):
    m = per_arm
    screen = m["screen"]
    # The screen banner silently accepts every 2xx-but-wrong persistence fault.
    assert screen["silent_wrong_count"] == len(SILENT_UNDER_SCREEN)
    assert screen["silent_wrong_action_rate"] > 0.0
    assert screen["undetected_wrong_rate"] > 0.0
    assert set(screen["silent_wrong_scenarios"]) == SILENT_UNDER_SCREEN


def test_effect_rest_catches_record_faults_but_collateral_write_slips(per_arm):
    m = per_arm
    rest = m["effect_rest"]
    # It drives the silent rate WAY down but NOT to zero -- the honest result.
    assert rest["silent_wrong_count"] == 1
    assert rest["silent_wrong_scenarios"] == [SLIPS_THROUGH_REST]
    assert (
        0.0 < rest["silent_wrong_action_rate"] < m["screen"]["silent_wrong_action_rate"]
    )
    # Every record-surface fault the screen missed is now CAUGHT (halted).
    for name in SILENT_UNDER_SCREEN - {SLIPS_THROUGH_REST}:
        ps = rest["per_scenario"][name]
        assert ps["gt_correct"] is False, name
        assert ps["halted"] is True, name
        assert ps["silent_wrong_rate"] == 0.0, name
    # The collateral write to the unaudited surface genuinely slips through.
    slip = rest["per_scenario"][SLIPS_THROUGH_REST]
    assert slip["gt_correct"] is False
    assert slip["halted"] is False
    assert slip["silent_wrong_rate"] == 1.0


def test_effect_full_read_path_closes_the_gap_to_zero(per_arm):
    m = per_arm
    full = m["effect_full"]
    # A complete read path (encounters AND billing) catches everything.
    assert full["silent_wrong_count"] == 0
    assert full["silent_wrong_action_rate"] == 0.0
    # Including the collateral write the REST oracle missed.
    slip = full["per_scenario"][SLIPS_THROUGH_REST]
    assert slip["gt_correct"] is False
    assert slip["halted"] is True


def test_ground_truth_is_independent_of_the_screen_arm(results):
    # Anti-circularity: the independent ground truth (direct sqlite file read)
    # disagrees with the screen arm's reported success on the wrong-effect
    # faults -- exactly the disagreement the older in-process benchmark could
    # not produce (its judge and oracle read the same object).
    disagreements = [
        r
        for r in results["runs"]
        if r["arm"] == "screen" and r["reported_success"] and not r["gt_correct"]
    ]
    assert len(disagreements) == len(SILENT_UNDER_SCREEN)
    # And the ground truth fault classes are the real, specific ones.
    faults = {r["scenario"]: r["gt_fault"] for r in disagreements}
    assert faults["no_persist"] == "absent"
    assert faults["partial"] == "partial"
    assert faults["duplicate"] == "duplicate"
    assert faults["wrong_record"] == "wrong_record"
    assert faults["stale"] == "collateral_loss"
    assert faults["collateral_unaudited"] == "collateral_write"


def test_clean_control_passes_every_arm(per_arm):
    m = per_arm
    for arm in ARMS:
        ps = m[arm]["per_scenario"]["ok"]
        assert ps["gt_correct"] is True, arm
        assert ps["halted"] is False, arm


def test_markdown_renders_and_states_the_numbers(results):
    md = render_markdown(results)
    assert "silent-wrong-effect rate" in md
    assert "three independent paths" in md.lower()
    assert "collateral write" in md.lower()
    # Every scenario appears in the per-fault matrix.
    for sc in SCENARIOS:
        assert f"`{sc.name}`" in md

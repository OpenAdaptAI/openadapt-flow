"""Tests for passive routine discovery (Robotic Process Mining).

Evaluation is on SYNTHETIC logs with KNOWN ground truth (the standard
process-mining method): the harness (``openadapt_flow.mining.synth``) interleaves
instances of two MockMed-style tasks with noise, and the tests assert discovery
recovers each routine with the right instance count, rejects the noise, exercises
the calibration thresholds, and that the multi-source adapters
(``openadapt_flow.mining.sources``) feed the SAME pipeline.
"""

from __future__ import annotations

import pytest

from openadapt_flow.mining import (
    CandidateRoutine,
    RoutineInstance,
    SegmentedRoutine,
    action_signature,
    discover_routines,
)
from openadapt_flow.mining.routine_discovery import (
    RoutineInstanceLike,
    _frequent_grams,
    _nonoverlap_starts,
)
from openadapt_flow.mining.sources import (
    from_agent_trajectory,
    from_playwright_script,
)
from openadapt_flow.mining.synth import generate_log


# -- headline: recover both routines, reject noise ---------------------------


def _routine_matching(result, signature: tuple[str, ...]):
    """The discovered routine whose signature starts with ``signature``, or None.

    Discovery recovers the invariant CORE; an instance's optional/variable tail
    is not part of it, so we match on the core being a prefix.
    """
    for r in result.routines:
        if r.signature[: len(signature)] == signature:
            return r
    return None


def test_recovers_both_routines_and_rejects_noise():
    log = generate_log(n_a=3, n_b=2, noise_between=2, seed=1)
    result = discover_routines(log.events)

    a_core = log.core_signatures("A")
    b_core = log.core_signatures("B")

    a = _routine_matching(result, a_core)
    b = _routine_matching(result, b_core)
    assert a is not None, "task A routine not recovered"
    assert b is not None, "task B routine not recovered"
    assert a.support == 3, f"task A should have 3 instances, got {a.support}"
    assert b.support == 2, f"task B should have 2 instances, got {b.support}"

    # Every injected noise action is rejected (claimed by no routine instance).
    claimed = {
        i
        for r in result.routines
        for inst in r.instances
        for i in range(inst.start_index, inst.end_index)
    }
    for noise_idx in log.noise_indices:
        assert noise_idx not in claimed, f"noise event {noise_idx} was claimed"

    # A's recovered instances line up with the ground-truth cores.
    gt_a_starts = sorted(i.core_start for i in log.instances if i.task == "A")
    assert sorted(inst.start_index for inst in a.instances) == gt_a_starts


def test_instances_are_aligned_to_the_signature():
    # The alignment invariant a downstream inducer relies on: position k is the
    # same action signature in every instance.
    log = generate_log(n_a=3, n_b=2, seed=2)
    result = discover_routines(log.events)
    assert result.routines
    for routine in result.routines:
        for inst in routine.instances:
            assert len(inst.events) == len(routine.signature)
            assert inst.signatures == routine.signature


def test_optional_dialog_is_not_part_of_the_routine():
    # The survey modal appears after only SOME task-A saves; being below support
    # it must not be folded into the invariant routine.
    log = generate_log(n_a=3, n_b=2, a_survey_every=3, seed=3)
    result = discover_routines(log.events)
    survey_idx = [
        i
        for i, e in enumerate(log.events)
        if action_signature(e) == "click@#dismiss-survey"
    ]
    assert survey_idx, "harness did not inject the optional dialog"
    for routine in result.routines:
        assert "click@#dismiss-survey" not in routine.signature
    claimed = {
        i
        for r in result.routines
        for inst in r.instances
        for i in range(inst.start_index, inst.end_index)
    }
    for idx in survey_idx:
        assert idx not in claimed


def test_variable_worklist_length_does_not_break_core_recovery():
    # Task B instances iterate a DIFFERENT number of worklist rows; the fixed
    # core must still be recovered across both.
    log = generate_log(n_a=2, n_b=2, b_worklist_lens=(1, 3), seed=4)
    result = discover_routines(log.events)
    b = _routine_matching(result, log.core_signatures("B")[:4])
    assert b is not None
    assert b.support == 2
    # Per-row-distinct actions of the LONGER worklist are not in the shared core.
    assert "click@.task-row-2" not in b.signature


# -- threshold sensitivity (the calibration knobs) ---------------------------


def test_min_support_threshold_drops_the_rarer_routine():
    log = generate_log(n_a=3, n_b=2, seed=5)
    a_core, b_core = log.core_signatures("A"), log.core_signatures("B")

    # min_support=3: task B (2 instances) falls below support and is NOT
    # promoted to a routine (the rarer routine is missed — the classic
    # false-negative direction of an over-high support threshold).
    result = discover_routines(log.events, min_support=3)
    assert _routine_matching(result, a_core) is not None
    assert _routine_matching(result, b_core) is None

    # min_support=2 recovers both.
    result2 = discover_routines(log.events, min_support=2)
    assert _routine_matching(result2, a_core) is not None
    assert _routine_matching(result2, b_core) is not None


def test_discarded_patterns_are_reported_not_silently_dropped():
    # Honesty: near-miss patterns are surfaced with a reason. A subsumed
    # sub-skeleton of task A (same support, shorter) must appear as discarded.
    log = generate_log(n_a=3, n_b=2, seed=5)
    result = discover_routines(log.events)
    reasons = {d.reason for d in result.discarded}
    assert "subsumed_by_longer" in reasons
    for d in result.discarded:
        assert d.reason in (
            "subsumed_by_longer",
            "below_min_length",
            "below_support",
            "below_support_after_segmentation",
        )


def test_min_pattern_length_threshold():
    log = generate_log(n_a=3, n_b=2, seed=6)
    # A long floor rejects even task A (core length 11): nothing qualifies.
    result = discover_routines(log.events, min_pattern_length=20)
    assert result.routines == ()
    # A moderate floor keeps A (len 11) but the too-short recurring 'scroll' noise
    # bigram never becomes a routine.
    result2 = discover_routines(log.events, min_pattern_length=5)
    assert _routine_matching(result2, log.core_signatures("A")) is not None


def test_thresholds_recorded_in_result():
    log = generate_log(n_a=2, n_b=2, seed=7)
    result = discover_routines(
        log.events, min_support=2, min_pattern_length=4, spatial_bucket_px=32
    )
    assert result.params["min_support"] == 2
    assert result.params["min_pattern_length"] == 4
    assert result.params["spatial_bucket_px"] == 32


# -- signature encoding ------------------------------------------------------


def test_signature_abstracts_typed_value_but_keeps_structure():
    # Two instances differing only in the typed value share a signature sequence.
    e1 = {"kind": "type", "text": "Follow-up in 2 weeks", "param": "note"}
    e2 = {"kind": "type", "text": "Discharge today", "param": "note"}
    assert action_signature(e1) == action_signature(e2) == "type:param=note"


def test_signature_prefers_structural_then_spatial_bucket():
    struct = {"kind": "click", "structural": {"selector": "#save"}}
    assert action_signature(struct) == "click@#save"
    role = {"kind": "click", "structural": {"role": "button", "name": "Save"}}
    assert action_signature(role) == "click@button/Save"
    # Pixel-only click falls back to a coarse spatial bucket.
    a = {"kind": "click", "x": 100, "y": 100}
    b = {"kind": "click", "x": 110, "y": 120}
    assert action_signature(a, spatial_bucket_px=64) == action_signature(
        b, spatial_bucket_px=64
    )
    assert action_signature(a, spatial_bucket_px=8) != action_signature(
        b, spatial_bucket_px=8
    )


# -- multi-source adapters feed the SAME pipeline ----------------------------


def test_agent_trajectory_adapter_produces_a_minable_log():
    # A computer-use agent runs the same 4-step task twice, with unrelated
    # actions (and a screenshot observation) between the runs.
    def task_run(n: int) -> list[dict]:
        return [
            {"action": "left_click", "selector": "#patient-search"},
            {"action": "type", "text": f"Smith {n}", "param": "query"},
            {"action": "key", "text": "Return"},
            {"action": "left_click", "selector": "#result-row"},
        ]

    trajectory = (
        task_run(1)
        + [
            {"action": "screenshot"},  # observation: skipped, not an action
            {"action": "left_click", "selector": "#unrelated-help"},
        ]
        + task_run(2)
    )
    events = from_agent_trajectory(trajectory)
    # Screenshot dropped; the two clicks/type/key/click runs are present.
    assert all("kind" in e for e in events)
    assert events[2] == {"kind": "key", "key": "Enter"}

    result = discover_routines(events, min_support=2, min_pattern_length=3)
    assert result.routines, "no routine mined from the agent trajectory"
    routine = result.routines[0]
    assert routine.support == 2
    assert routine.signature[0] == "click@#patient-search"


def test_agent_trajectory_adapter_rejects_unmappable_actions():
    with pytest.raises(ValueError, match="no flow equivalent"):
        from_agent_trajectory([{"action": "right_click", "coordinate": [1, 2]}])
    with pytest.raises(ValueError, match="unknown agent action"):
        from_agent_trajectory([{"action": "teleport"}])


def test_rpa_script_adapter_playwright():
    script = """
        from playwright.sync_api import sync_playwright
        page.goto("https://mockmed.example/login")     # navigation: skipped
        page.locator("#username").fill("nurse.demo")
        page.locator("#password").fill("mockmed-demo-pass")
        page.get_by_role("button", name="Sign In").click()
        page.locator(".open-btn").click()
        page.locator("#note").fill("Follow-up in 2 weeks")
        page.locator("#save-encounter").click()
        expect(page.locator(".toast")).to_be_visible()  # assertion: skipped
    """
    events = from_playwright_script(script, params={"note": "Follow-up in 2 weeks"})
    kinds = [e["kind"] for e in events]
    # fill -> click(field) + type; get_by_role -> click; plain clicks -> click.
    assert kinds == [
        "click", "type",  # username
        "click", "type",  # password
        "click",          # Sign In (role)
        "click",          # .open-btn
        "click", "type",  # note (parameterized)
        "click",          # save
    ]
    assert events[4] == {
        "kind": "click",
        "structural": {"role": "button", "name": "Sign In"},
    }
    # The note fill was tagged as the 'note' parameter (per-run data).
    note_type = events[7]
    assert note_type["kind"] == "type" and note_type.get("param") == "note"


def test_rpa_script_adapter_selenium_and_key():
    script = """
        driver.find_element(By.ID, "worklist-tab").click()
        driver.find_element(By.CSS_SELECTOR, "#filter-open").click()
        driver.find_element(By.NAME, "q").send_keys("open referrals")
        driver.find_element(By.NAME, "q").send_keys(Keys.RETURN)
    """
    events = from_playwright_script(script)
    assert events[0] == {"kind": "click", "structural": {"selector": "#worklist-tab"}}
    assert events[-1] == {"kind": "key", "key": "Enter"}
    # send_keys with a literal -> focus click + type.
    assert events[2]["kind"] == "click" and events[3]["kind"] == "type"


def test_rpa_script_adapter_rejects_unrecognized_actuation():
    # An actuation verb the adapter does not model must NOT be silently skipped.
    with pytest.raises(ValueError, match="unrecognized actuation"):
        from_playwright_script('page.frobnicate("#x").click()\npage.tap("#y")')


def test_two_rpa_script_runs_are_minable():
    # The same script executed twice (e.g. two log captures) + noise between
    # them → discovery recovers the one routine with support 2.
    script = """
        page.locator("#worklist-tab").click()
        page.locator("#filter-open").click()
        page.locator("#q").fill("open")
        page.locator("#q").press("Enter")
    """
    run = from_playwright_script(script)
    noise = [{"kind": "click", "structural": {"selector": "#stray"}}]
    log = run + noise + run
    result = discover_routines(log, min_support=2, min_pattern_length=3)
    assert len(result.routines) == 1
    assert result.routines[0].support == 2


# -- hand-off protocols (decoupled from induction) ---------------------------


def test_result_types_satisfy_handoff_protocols():
    log = generate_log(n_a=2, n_b=2, seed=8)
    result = discover_routines(log.events)
    assert result.routines
    for routine in result.routines:
        assert isinstance(routine, CandidateRoutine)
        # The thin protocol the sibling induction PR consumes.
        assert isinstance(routine, SegmentedRoutine)
        for inst in routine.instances:
            assert isinstance(inst, RoutineInstance)
            assert isinstance(inst, RoutineInstanceLike)


def test_candidate_routine_rejects_misaligned_instances():
    inst = RoutineInstance(start_index=0, end_index=1, events=({"kind": "click"},))
    with pytest.raises(ValueError, match="misaligned"):
        CandidateRoutine(
            routine_id="r", signature=("a", "b"), instances=(inst,), support=1,
            confidence=0.5,
        )


# -- helpers / edge cases ----------------------------------------------------


def test_empty_and_single_event_logs():
    assert discover_routines([]).routines == ()
    assert discover_routines([{"kind": "click", "x": 1, "y": 1}]).routines == ()


def test_synth_is_deterministic_given_seed():
    a = generate_log(n_a=3, n_b=2, seed=42)
    b = generate_log(n_a=3, n_b=2, seed=42)
    assert a.events == b.events
    assert [i.start for i in a.instances] == [i.start for i in b.instances]


def test_nonoverlap_support_counts_non_overlapping_occurrences():
    # Occurrences at 0,1,2 of a length-2 pattern -> 0..1 then 2 = 2 non-overlap.
    assert _nonoverlap_starts([0, 1, 2], 2) == [0, 2]


def test_frequent_grams_prunes_below_support():
    sigs = ["a", "b", "c", "a", "b", "c", "x"]
    grams = _frequent_grams(sigs, min_support=2, max_length=10)
    assert ("a", "b", "c") in grams
    assert ("x",) not in grams


def test_coverage_reported():
    log = generate_log(n_a=3, n_b=2, seed=9)
    result = discover_routines(log.events)
    assert 0.0 < result.coverage <= 1.0

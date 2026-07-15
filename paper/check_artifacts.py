"""Fail when paper headline constants drift from benchmark artifacts."""

from __future__ import annotations

import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(relative: str) -> dict:
    with (ROOT / relative).open(encoding="utf-8") as handle:
        return json.load(handle)


def require_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, found {actual!r}")


def require_close(actual: float, expected: float, label: str) -> None:
    if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=0.051):
        raise AssertionError(f"{label}: expected {expected}, found {actual}")


def main() -> None:
    comparison_artifact = load("benchmark/comparison_artifact/comparison.json")
    require_equal(
        comparison_artifact["model_calls_compiled"], 0, "compiled model calls"
    )
    comparison = comparison_artifact["benchmarks"]
    openemr = comparison["openemr"]["arms"]
    mockmed = comparison["mockmed"]["arms"]

    require_equal(openemr["compiled"]["n"], 20, "OpenEMR compiled n")
    require_equal(openemr["compiled"]["success_count"], 20, "OpenEMR compiled success")
    require_close(openemr["compiled"]["wall_s_p50"], 39.2, "OpenEMR compiled p50")
    require_equal(openemr["agent"]["n"], 10, "OpenEMR agent n")
    require_equal(openemr["agent"]["success_count"], 10, "OpenEMR agent success")
    require_close(openemr["agent"]["wall_s_p50"], 70.4, "OpenEMR agent p50")
    require_close(openemr["agent"]["cost_usd_per_run"], 0.55, "OpenEMR agent cost")

    require_equal(mockmed["compiled"]["n"], 100, "MockMed compiled n")
    require_equal(mockmed["compiled"]["success_count"], 100, "MockMed compiled success")
    require_close(mockmed["compiled"]["wall_s_p50"], 4.9, "MockMed compiled p50")
    require_equal(mockmed["agent"]["n"], 20, "MockMed agent n")
    require_equal(mockmed["agent"]["success_count"], 20, "MockMed agent success")
    require_close(mockmed["agent"]["wall_s_p50"], 37.5, "MockMed agent p50")
    require_close(mockmed["agent"]["cost_usd_per_run"], 0.27, "MockMed agent cost")

    reliability = load("benchmark/reliability/results.json")
    require_equal(len(reliability["results"]), 29, "reliability apps")
    outcomes = reliability["summary"]["outcomes"]
    require_equal(outcomes.get("success"), 17, "reliability successes")
    require_equal(outcomes.get("safe_halt"), 10, "reliability safe halts")
    require_equal(outcomes.get("wrong_action"), 2, "reliability wrong actions")

    faults = load("benchmark/fault_model/results.json")
    require_equal(faults["meta"]["repeats"], 10, "fault repeats")
    require_equal(faults["meta"]["model_calls"], 0, "fault-model model calls")
    require_equal(len(faults["runs"]), 90, "fault-model runs")
    expected_faults = {
        "ok": ({"SUCCESS": 10}, 0),
        "partial": ({"UNDETECTED-FAILURE": 10}, 10),
        "duplicate": ({"WRONG-ACTION": 10}, 10),
        "timeout": ({"FALSE-ABORT": 10}, 0),
        "optimistic": ({"UNDETECTED-FAILURE": 10}, 10),
        "session": ({"SAFE-HALT": 10}, 0),
        "stale": ({"WRONG-ACTION": 10}, 10),
        "double": ({"WRONG-ACTION": 10}, 10),
        "idempotent": ({"SUCCESS": 10}, 0),
    }
    require_equal(len(faults["classes"]), len(expected_faults), "fault classes")
    for result in faults["classes"]:
        mode = result["mode"]
        expected_outcomes, expected_silent = expected_faults[mode]
        require_equal(result["repeats"], 10, f"{mode} repeats")
        require_equal(result["outcome_counts"], expected_outcomes, f"{mode} outcomes")
        require_equal(
            result["silently_mishandled_count"],
            expected_silent,
            f"{mode} silently mishandled",
        )

    silent = load("benchmark/silent_wrong_action/results.json")
    metrics = silent["metrics"]
    require_equal(metrics["n_runs"], 90, "silent-wrong runs")
    require_equal(metrics["screen"]["silent_wrong_count"], 50, "screen silent wrong")
    require_equal(metrics["screen"]["false_abort_count"], 10, "screen false abort")
    require_equal(metrics["effect"]["silent_wrong_count"], 0, "effect silent wrong")
    require_equal(metrics["effect"]["false_abort_count"], 0, "effect false abort")
    require_equal(
        metrics["screen"]["silent_wrong_action_rate"],
        50 / 90,
        "screen silent-wrong rate",
    )
    require_equal(
        metrics["effect"]["silent_wrong_action_rate"],
        0.0,
        "effect silent-wrong rate",
    )
    expected_verdicts = {
        "ok": (0.0, "confirmed"),
        "partial": (1.0, "refuted"),
        "optimistic": (1.0, "refuted"),
        "duplicate": (1.0, "refuted"),
        "double": (1.0, "refuted"),
        "stale": (1.0, "refuted"),
        "timeout": (0.0, "confirmed"),
        "session": (0.0, "refuted"),
        "idempotent": (0.0, "confirmed"),
    }
    for scenario, (screen_rate, effect_verdict) in expected_verdicts.items():
        result = metrics["per_scenario"][scenario]
        require_equal(result["n"], 10, f"{scenario} silent-wrong n")
        require_equal(
            result["screen_silent_wrong_rate"],
            screen_rate,
            f"{scenario} screen silent-wrong rate",
        )
        require_equal(
            result["effect_verdict"], effect_verdict, f"{scenario} effect verdict"
        )
        require_equal(
            result["effect_silent_wrong_rate"],
            0.0,
            f"{scenario} effect silent-wrong rate",
        )

    identity = load("benchmark/identity_ladder/identity_ladder.json")
    expected_identity = {
        "structured": (14, 14, 0, 0.0),
        "pixel_stable": (14, 14, 14, 1.0),
        "pixel_drift_vlm_on": (42, 42, 42, 1.0),
        "pixel_drift_vlm_off": (42, 42, 42, 1.0),
        "ocr_only_confusable": (42, 42, 42, 1.0),
    }
    configs = identity["summary"]["configs"]
    require_equal(set(configs), set(expected_identity), "identity configs")
    for name, result in configs.items():
        n_correct, n_wrong, over_halt, over_halt_rate = expected_identity[name]
        require_equal(result["n_correct"], n_correct, f"{name} correct n")
        require_equal(result["n_wrong"], n_wrong, f"{name} wrong n")
        require_equal(result["false_accept"], 0, f"{name} false accepts")
        require_equal(result["over_halt"], over_halt, f"{name} over halt")
        require_equal(
            result["over_halt_rate"], over_halt_rate, f"{name} over-halt rate"
        )

    print("paper artifact constants: OK")


if __name__ == "__main__":
    main()

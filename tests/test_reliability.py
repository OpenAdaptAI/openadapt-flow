"""Tests for the compile-reliability study harness.

The pure-logic tests (taxonomy, outcome mapping, aggregation, manifest
serialization) run with no browser or network. One end-to-end integration
test drives the full record -> compile -> replay -> ground-truth path against
a LOCAL http server (no network), so the harness itself is exercised exactly
as the study uses it.
"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

from openadapt_flow.benchmark import reliability as rel
from openadapt_flow.benchmark.reliability import (
    AppSpec,
    Step,
    Verify,
    aggregate,
    classify_failure,
    evaluate_verify,
)
from openadapt_flow.benchmark.reliability_corpus import CORPUS
from openadapt_flow.ir import Resolution, RunReport, StepResult


# --------------------------------------------------------------------------
# Pure logic: outcome mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "report_success, ground_truth, expected",
    [
        (True, True, "success"),
        (True, False, "wrong_action"),
        (False, True, "false_halt"),
        (False, False, "safe_halt"),
        (True, None, "success"),  # self-reported
        (False, None, "safe_halt"),  # self-reported
    ],
)
def test_outcome_truth_table(report_success, ground_truth, expected):
    assert rel._outcome(report_success, ground_truth) == expected


# --------------------------------------------------------------------------
# Pure logic: failure taxonomy
# --------------------------------------------------------------------------


def _report(*results: StepResult) -> RunReport:
    return RunReport(
        workflow_name="t",
        started_at="now",
        results=list(results),
        success=all(r.ok for r in results),
    )


def _ok(step_id="s0") -> StepResult:
    return StepResult(
        step_id=step_id,
        intent="ok",
        ok=True,
        resolution=Resolution(
            rung="template", point=(1, 1), confidence=1.0, elapsed_ms=1.0
        ),
    )


def _fail(error: str, *, resolution: bool = True, step_id="s1") -> StepResult:
    return StepResult(
        step_id=step_id,
        intent="boom",
        ok=False,
        resolution=(
            Resolution(rung="ocr", point=(1, 1), confidence=0.9, elapsed_ms=1.0)
            if resolution
            else None
        ),
        error=error,
    )


@pytest.mark.parametrize(
    "error, resolution, expected",
    [
        (
            "Could not resolve target ...: all resolution rungs failed",
            False,
            "resolution_failed",
        ),
        ("Identity check failed for step 's1' ...", True, "identity_mismatch"),
        (
            "Postconditions failed for step 's1' ...: failed: text_present 'x'",
            True,
            "postcondition_failed",
        ),
        (
            "Typed input could not be verified for step 's1' ...",
            True,
            "typed_input_unverified",
        ),
        (
            "Step 's1' ...: closed-loop scroll exhausted its budget ...",
            True,
            "scroll_exhausted",
        ),
        (
            "Step 's1' is irreversible but only resolved ... refusing to act",
            True,
            "risk_gate",
        ),
        ("Step 's1' raised RuntimeError: nope", True, "step_exception"),
        ("something else entirely", True, "other_halt"),
    ],
)
def test_classify_failure_categories(error, resolution, expected):
    report = _report(_ok(), _fail(error, resolution=resolution))
    tax = classify_failure(report, None)
    assert tax["category"] == expected


def test_classify_failure_record_and_compile():
    assert (
        classify_failure(None, "record RuntimeError: x")["category"] == "record_error"
    )
    assert (
        classify_failure(None, "compile ValueError: y")["category"] == "compile_error"
    )


def test_classify_failure_no_failing_step():
    report = _report(_ok("a"), _ok("b"))
    assert classify_failure(report, None)["category"] == "none"


@pytest.mark.parametrize(
    "text, expected_hint",
    [
        ("HTTP 403 Forbidden", "anti_bot"),
        ("captcha detected", "anti_bot"),
        ("timed out waiting for settle", "timing"),
        ("an iframe reference failed", "iframe"),
        ("canvas has no dom", "canvas"),
        ("net::ERR_CONNECTION_REFUSED", "navigation"),
        ("Postconditions failed ... text_present 'x'", "dynamic_content"),
    ],
)
def test_root_cause_specific_buckets(text, expected_hint):
    assert rel._root_cause(text) == expected_hint


def test_root_cause_empty_is_none():
    assert rel._root_cause("") is None


# --------------------------------------------------------------------------
# Pure logic: aggregation
# --------------------------------------------------------------------------


def test_aggregate_distribution():
    results = [
        {
            "compile_success": True,
            "outcome": "success",
            "failure_category": None,
            "root_cause_hint": None,
            "rung_counts": {"template": 3},
        },
        {
            "compile_success": True,
            "outcome": "safe_halt",
            "failure_category": "postcondition_failed",
            "root_cause_hint": "dynamic_content",
            "rung_counts": {"template": 1},
        },
        {
            "compile_success": True,
            "outcome": "wrong_action",
            "failure_category": "wrong_action_silent",
            "root_cause_hint": "ground_truth_disagrees",
            "rung_counts": {},
        },
        {
            "compile_success": False,
            "outcome": "record_error",
            "failure_category": "record_error",
            "root_cause_hint": None,
            "rung_counts": {},
        },
    ]
    s = aggregate(results)
    assert s["n_apps"] == 4
    assert s["n_recorded"] == 3
    assert s["n_compiled"] == 3
    assert s["outcomes"]["success"] == 1
    assert s["wrong_action_count"] == 1
    assert s["safe_halt_count"] == 1
    assert s["record_error_count"] == 1
    assert s["compile_success_rate_over_recorded"] == 1.0
    assert s["replay_success_rate_over_replayed"] == round(1 / 3, 3)
    assert s["failure_categories"]["postcondition_failed"] == 1


# --------------------------------------------------------------------------
# Corpus manifest is serializable and well-formed
# --------------------------------------------------------------------------


def test_corpus_is_serializable_and_valid():
    import json

    assert len(CORPUS) >= 20, "study needs a broad corpus"
    ids = [a.id for a in CORPUS]
    assert len(ids) == len(set(ids)), "duplicate corpus ids"
    valid_actions = {"click", "double_click", "type", "press", "scroll"}
    valid_verify = {
        "url_contains",
        "dom_text_contains",
        "dom_visible",
        "dom_count",
        "dom_value_equals",
        "dom_value_nonempty",
        "dom_checked",
        "self_reported",
    }
    for app in CORPUS:
        assert app.url.startswith("http"), app.id
        assert app.steps, f"{app.id} has no steps"
        assert 1 <= len(app.steps) <= 12
        for st in app.steps:
            assert st.action in valid_actions, (app.id, st.action)
            if st.action in ("click", "double_click"):
                assert st.selector, f"{app.id}: click needs a selector"
        assert app.verify.kind in valid_verify, (app.id, app.verify.kind)
        # round-trips through JSON
        json.dumps(app.to_manifest())


def test_corpus_diversity():
    cats = {a.category for a in CORPUS}
    frameworks = {a.framework for a in CORPUS}
    # A real generalization test must span many app classes + frameworks.
    assert len(cats) >= 6, cats
    assert len(frameworks) >= 6, frameworks


def test_evaluate_verify_self_reported_is_none():
    assert evaluate_verify(Verify(kind="self_reported"), page=None) is None


# --------------------------------------------------------------------------
# End-to-end integration against a LOCAL http server (no network)
# --------------------------------------------------------------------------

FORM_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>local form</title>
<style>body{font-family:sans-serif;padding:40px}
input,button{font-size:18px;padding:8px;margin:8px;display:block}
#result{margin-top:20px;font-size:22px;color:#0a0}</style></head>
<body>
<h1>Local Test Form</h1>
<input id="name" placeholder="name">
<button id="go" onclick="document.getElementById('result').textContent
  = 'Saved: ' + document.getElementById('name').value">Save</button>
<div id="result"></div>
</body></html>"""


@pytest.fixture()
def local_form_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    (root / "index.html").write_text(FORM_HTML)
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(root)
    )
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/index.html"
    httpd.shutdown()


@pytest.fixture()
def _browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


def test_run_target_end_to_end_success(local_form_url, _browser, tmp_path: Path):
    """Full record -> compile -> replay -> ground truth on a local form."""
    app = AppSpec(
        id="local_form",
        name="local form",
        url=local_form_url,
        category="form",
        framework="static",
        description="type a name and save",
        steps=[
            Step(action="click", selector="#name"),
            Step(action="type", text="Ada", param="name"),
            Step(action="click", selector="#go"),
        ],
        verify=Verify(kind="dom_text_contains", selector="#result", value="Saved: Ada"),
        params={"name": "Ada"},
    )
    result = rel.run_target(app, tmp_path / "wd", _browser)
    assert result["compile_success"] is True, result
    assert result["outcome"] == "success", result
    assert result["ground_truth"] is True
    assert result["rung_counts"].get("grounder", 0) == 0  # zero model calls
    assert result["heal_count"] == 0


def test_run_target_record_error_on_bad_selector(
    local_form_url, _browser, tmp_path: Path
):
    """A selector that never appears is captured as a record_error, not a crash."""
    app = AppSpec(
        id="local_bad",
        name="bad selector",
        url=local_form_url,
        category="form",
        framework="static",
        description="click a nonexistent element",
        steps=[Step(action="click", selector="#does-not-exist")],
        verify=Verify(kind="self_reported"),
    )
    result = rel.run_target(app, tmp_path / "wd", _browser)
    assert result["outcome"] == "record_error"
    assert result["compile_success"] is False
    assert result["error"]

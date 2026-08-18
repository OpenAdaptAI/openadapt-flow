"""Regression contracts for cross-platform GitHub Actions selection."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"
QUICKSTART = ROOT / ".github/workflows/quickstart-lifecycle.yml"
VALIDATE_CLAIMS = ROOT / ".github/workflows/validate-claims.yml"


def test_playwright_version_probes_are_valid_python() -> None:
    workflow = CI.read_text(encoding="utf-8")
    probe = (
        'python -c "import importlib.metadata as m; '
        "print('version=' + m.version('playwright'))\" >> \"$GITHUB_OUTPUT\""
    )

    assert workflow.count("- name: Resolve Playwright version") == 3
    assert workflow.count(probe) == 3
    assert r"m.version(\"playwright\")" not in workflow


def test_playwright_installs_and_enclosing_jobs_are_bounded() -> None:
    """A stalled external browser download cannot consume a runner for hours."""
    workflow = CI.read_text(encoding="utf-8")
    test_start = workflow.index("\n  test:")
    e2e_start = workflow.index("\n  e2e-browser:")
    linux_start = workflow.index("\n  linux-atspi-x11:")
    matrix_start = workflow.index("\n  test-matrix:")
    windows_start = workflow.index("\n  windows-mock:")

    test_job = workflow[test_start:e2e_start]
    e2e_job = workflow[e2e_start:linux_start]
    matrix_job = workflow[matrix_start:windows_start]
    invocation = (
        "python scripts/install_playwright_browser.py\n"
        "          --attempts 2 --attempt-timeout-seconds 600"
    )

    for job, timeout in (
        (test_job, "timeout-minutes: 55"),
        (e2e_job, "timeout-minutes: 50"),
        (matrix_job, "timeout-minutes: 75"),
    ):
        assert timeout in job
        install_start = job.index("- name: Install Playwright browser")
        install_end = job.index("\n\n", install_start)
        install_step = job[install_start:install_end]
        assert "timeout-minutes: 22" in install_step
        assert invocation in install_step

    assert workflow.count(invocation) == 3
    assert "run: playwright install --with-deps chromium" not in workflow

    # The timeout wrapper changes only external installation. Keep the exact
    # required test and coverage selections intact.
    assert "pytest -q --ignore=tests/e2e --basetemp=runs/ci" in test_job
    assert "coverage report --fail-under=85" in test_job
    assert "pytest -q tests/e2e/test_free_path_e2e.py" in e2e_job
    assert "pytest -q tests/e2e \\" in e2e_job
    assert "--ignore=tests/e2e/test_free_path_e2e.py" in e2e_job
    assert "pytest -q --basetemp=runs/ci" in matrix_job


def test_windows_required_job_proves_retained_installer_job_object() -> None:
    workflow = CI.read_text(encoding="utf-8")
    windows_start = workflow.index("\n  windows-mock:")
    wheel_start = workflow.index("\n  wheel:")
    windows_job = workflow[windows_start:wheel_start]

    assert "Windows retained installer Job Object (non-injecting)" in windows_job
    assert "pytest -q tests/test_install_playwright_browser.py" in windows_job


def test_full_matrix_can_be_dispatched_on_an_exact_branch() -> None:
    workflow = CI.read_text(encoding="utf-8")
    on_start = workflow.index("on:\n")
    jobs_start = workflow.index("\njobs:\n", on_start)
    triggers = workflow[on_start:jobs_start]

    assert "  workflow_dispatch:\n" in triggers


def test_full_matrix_runs_only_nightly_or_when_explicitly_dispatched() -> None:
    workflow = CI.read_text(encoding="utf-8")
    matrix_start = workflow.index("\n  test-matrix:")
    strategy_start = workflow.index("\n    strategy:", matrix_start)
    matrix_header = workflow[matrix_start:strategy_start]

    assert "github.event_name == 'schedule'" in matrix_header
    assert "github.event_name == 'workflow_dispatch'" in matrix_header
    assert "inputs." not in matrix_header
    assert "github.event_name != 'pull_request'" not in matrix_header
    assert "github.event_name == 'push'" not in matrix_header


def test_required_context_comments_match_actual_checkrun_names() -> None:
    workflow = CI.read_text(encoding="utf-8")
    start = workflow.index("# REQUIRED_CONTEXTS")
    end = workflow.index("# `gate` comes from", start)
    documented = set(
        re.findall(r"^#   - ([a-z0-9-]+)$", workflow[start:end], re.MULTILINE)
    )

    assert documented == {
        "lint",
        "python-compatibility",
        "mypy-strict-safety",
        "phi-guard",
        "windows-mock",
        "docs-consistency",
        "effectbench-standalone",
        "interop-types",
        "test",
        "e2e-browser",
        "linux-atspi-x11",
        "wheel",
        "gate",
    }

    claims = VALIDATE_CLAIMS.read_text(encoding="utf-8")
    claims_header = claims[: claims.index("\non:\n")]
    assert '"gate" (the actual CheckRun job name' in claims_header
    assert '"validate-claims" to block a PR' not in claims_header


def test_macos_deselects_only_redundant_heavy_identity_harness() -> None:
    workflow = CI.read_text(encoding="utf-8")
    node = "tests/test_identity_ladder.py::test_harness_zero_false_accept_all_configs"
    linux_start = workflow.index(
        "- name: Test (full suite incl. e2e, canonical Ubuntu)"
    )
    macos_start = workflow.index(
        "- name: Test (full suite incl. e2e, macOS platform coverage)"
    )
    upload_start = workflow.index("- name: Upload run artifacts", macos_start)
    linux_step = workflow[linux_start:macos_start]
    macos_step = workflow[macos_start:upload_start]

    assert "if: runner.os == 'Linux'" in linux_step
    assert "--deselect" not in linux_step
    assert "pytest -q --basetemp=runs/ci" in linux_step
    assert "if: runner.os == 'macOS'" in macos_step
    assert macos_step.count(f"--deselect={node}") == 1
    assert workflow.count(f"--deselect={node}") == 1


def test_exhaustive_identity_ladder_corpus_runs_in_the_slow_lane_only() -> None:
    """The exhaustive identity-ladder sweep stays OUT of the fast PR lane.

    The fast `test` job runs the bounded class-covering harness; the
    nightly/dispatch canonical-Ubuntu matrix leg opts into the exhaustive
    corpus via OPENADAPT_IDENTITY_LADDER_EXHAUSTIVE. This pins both sides so
    the exhaustive sweep can neither silently stop running anywhere nor creep
    back into the fast lane whose 900s budget it intermittently exceeded.
    """
    workflow = CI.read_text(encoding="utf-8")
    flag = "OPENADAPT_IDENTITY_LADDER_EXHAUSTIVE"

    linux_start = workflow.index(
        "- name: Test (full suite incl. e2e, canonical Ubuntu)"
    )
    macos_start = workflow.index(
        "- name: Test (full suite incl. e2e, macOS platform coverage)"
    )
    linux_step = workflow[linux_start:macos_start]
    assert f'{flag}: "1"' in linux_step

    fast_start = workflow.index("- name: Test (fast unit suite)")
    fast_end = workflow.index("- name: Coverage (whole-package visibility)")
    assert flag not in workflow[fast_start:fast_end]
    # exactly one opt-in: the canonical Ubuntu matrix leg
    assert workflow.count(f'{flag}: "1"') == 1


def test_clean_machine_lifecycle_declares_utf8_on_every_os() -> None:
    workflow = QUICKSTART.read_text(encoding="utf-8")
    lifecycle_start = workflow.index("  lifecycle:")
    strategy_start = workflow.index("    strategy:", lifecycle_start)
    lifecycle_header = workflow[lifecycle_start:strategy_start]

    assert 'PYTHONUTF8: "1"' in lifecycle_header
    assert 'PYTHONIOENCODING: "utf-8"' in lifecycle_header

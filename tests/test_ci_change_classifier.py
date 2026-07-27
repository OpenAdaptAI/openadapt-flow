"""Contracts for the fail-closed paper-only CI path."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLASSIFIER = ROOT / ".github/scripts/classify_changes.py"
CI = ROOT / ".github/workflows/ci.yml"

spec = importlib.util.spec_from_file_location("classify_changes", CLASSIFIER)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_only_normalized_paper_descendants_take_the_light_path() -> None:
    assert module.classify_paths(
        ["paper/main.tex", "paper/figures/result.pdf"]
    ).paper_only

    for paths in (
        ["openadapt_flow/ir.py"],
        ["paper/main.tex", ".github/workflows/ci.yml"],
        ["benchmark/effectbench/result.json"],
        [],
        ["paper"],
        ["paperish/main.tex"],
        ["paper/../openadapt_flow/ir.py"],
    ):
        assert module.classify_paths(paths).run_runtime


def test_non_diff_events_and_unusable_shas_run_the_full_gate() -> None:
    assert module.classify_event("workflow_dispatch", "", "").run_runtime
    assert module.classify_event("schedule", "", "").run_runtime
    assert module.classify_event("push", "a" * 40, "b" * 40).run_runtime
    assert module.classify_event("push", "0" * 40, "a" * 40).run_runtime


def test_runtime_file_moved_into_paper_keeps_both_paths(
    tmp_path: Path, monkeypatch
) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.name", "CI")
    git("config", "user.email", "ci@example.invalid")
    (tmp_path / "openadapt_flow").mkdir()
    (tmp_path / "openadapt_flow/runtime.py").write_text("runtime = True\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")

    (tmp_path / "paper").mkdir()
    git("mv", "openadapt_flow/runtime.py", "paper/runtime.py")
    git("commit", "-qam", "move")

    monkeypatch.chdir(tmp_path)
    paths = module.changed_paths(base, git("rev-parse", "HEAD"))
    assert paths == ["openadapt_flow/runtime.py", "paper/runtime.py"]
    assert module.classify_paths(paths).run_runtime


def test_required_runtime_contexts_remain_declared_and_docs_stays_on() -> None:
    workflow = CI.read_text(encoding="utf-8")
    runtime_jobs = {
        "lint",
        "python-compatibility",
        "mypy-strict-safety",
        "effectbench-standalone",
        "interop-types",
        "phi-guard",
        "test",
        "e2e-browser",
        "linux-atspi-x11",
        "windows-mock",
        "wheel",
    }
    for job in runtime_jobs:
        marker = f"\n  {job}:\n"
        start = workflow.index(marker)
        header = workflow[start : workflow.index("    runs-on:", start)]
        assert "needs: classify-changes" in header
        assert "!cancelled()" in header
        assert "outputs.run_runtime != 'false'" in header

    docs_start = workflow.index("\n  docs-consistency:\n")
    docs_header = workflow[docs_start : workflow.index("    runs-on:", docs_start)]
    assert "needs: classify-changes" not in docs_header

"""Contracts for the fail-closed paper-only CI path."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLASSIFIER = ROOT / ".github/scripts/classify_changes.py"

spec = importlib.util.spec_from_file_location("classify_changes", CLASSIFIER)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_only_paper_descendants_take_the_light_path() -> None:
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


def test_only_pull_requests_can_take_the_light_path() -> None:
    for event in ("workflow_dispatch", "schedule", "push"):
        assert module.classify_event(event, "a" * 40, "b" * 40).run_runtime
    assert module.classify_event("pull_request", "0" * 40, "a" * 40).run_runtime


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_a_runtime_file_move_into_paper_stays_on_the_full_path(
    tmp_path: Path, monkeypatch
) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "CI")
    _git(tmp_path, "config", "user.email", "ci@example.invalid")
    (tmp_path / "openadapt_flow").mkdir()
    (tmp_path / "openadapt_flow/runtime.py").write_text("runtime = True\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    (tmp_path / "paper").mkdir()
    _git(tmp_path, "mv", "openadapt_flow/runtime.py", "paper/runtime.py")
    _git(tmp_path, "commit", "-qam", "move")
    monkeypatch.chdir(tmp_path)

    paths = module.changed_paths(base, _git(tmp_path, "rev-parse", "HEAD"))
    assert paths == ["openadapt_flow/runtime.py", "paper/runtime.py"]
    assert module.classify_paths(paths).run_runtime


def test_unrelated_target_branch_changes_do_not_change_the_pr_scope(
    tmp_path: Path, monkeypatch
) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "CI")
    _git(tmp_path, "config", "user.email", "ci@example.invalid")
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper/main.tex").write_text("base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    target_branch = _git(tmp_path, "branch", "--show-current")

    _git(tmp_path, "checkout", "-qb", "paper-change")
    (tmp_path / "paper/main.tex").write_text("paper change\n")
    _git(tmp_path, "commit", "-qam", "paper")
    head = _git(tmp_path, "rev-parse", "HEAD")

    _git(tmp_path, "checkout", "-q", target_branch)
    (tmp_path / "openadapt_flow").mkdir()
    (tmp_path / "openadapt_flow/runtime.py").write_text("runtime = True\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "target branch change")
    base = _git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)

    paths = module.changed_paths(base, head)
    assert paths == ["paper/main.tex"]
    assert module.classify_paths(paths).paper_only

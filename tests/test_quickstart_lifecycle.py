"""Fast structural tests for the clean-wheel lifecycle harness."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "quickstart_lifecycle.py"


def _module():
    spec = importlib.util.spec_from_file_location("quickstart_lifecycle", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_wheel_requires_exactly_one_match(tmp_path):
    lifecycle = _module()
    with pytest.raises(ValueError, match="exactly one"):
        lifecycle._resolve_wheel(str(tmp_path / "*.whl"))
    (tmp_path / "one.whl").write_bytes(b"wheel")
    assert lifecycle._resolve_wheel(str(tmp_path / "*.whl")).name == "one.whl"
    (tmp_path / "two.whl").write_bytes(b"wheel")
    with pytest.raises(ValueError, match="matched 2"):
        lifecycle._resolve_wheel(str(tmp_path / "*.whl"))


def test_inspect_artifacts_requires_reports_repairs_and_healed_bundle(tmp_path):
    lifecycle = _module()
    artifacts = tmp_path / "artifacts"
    for name, report in {
        "baseline-run": {"success": True, "model_calls": 0, "heal_count": 0},
        "theme-drift-run": {
            "success": True,
            "model_calls": 0,
            "heal_count": 1,
            "results": [{"heal": {"applied": True}}],
        },
    }.items():
        run = artifacts / name
        run.mkdir(parents=True)
        (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (run / "REPORT.md").write_text("# report\n", encoding="utf-8")
    patch = artifacts / "theme-drift-run" / "heals" / "step_001" / "patch.json"
    patch.parent.mkdir(parents=True)
    patch.write_text("{}", encoding="utf-8")
    healed = artifacts / "healed-bundle"
    healed.mkdir()
    (healed / "workflow.json").write_text("{}", encoding="utf-8")
    (healed / "manifest.json").write_text("{}", encoding="utf-8")

    summary = lifecycle._inspect_artifacts(artifacts)

    assert summary["drift_heals"] == 1
    assert summary["repair_patches"] == 1


def test_inspect_artifacts_rejects_missing_patch(tmp_path):
    lifecycle = _module()
    artifacts = tmp_path / "artifacts"
    for name, report in {
        "baseline-run": {"success": True, "model_calls": 0, "heal_count": 0},
        "theme-drift-run": {
            "success": True,
            "model_calls": 0,
            "heal_count": 1,
            "results": [{"heal": {"applied": True}}],
        },
    }.items():
        run = artifacts / name
        run.mkdir(parents=True)
        (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (run / "REPORT.md").write_text("# report\n", encoding="utf-8")
    healed = artifacts / "healed-bundle"
    healed.mkdir()
    (healed / "workflow.json").write_text("{}", encoding="utf-8")
    (healed / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(AssertionError, match="heal evidence is incomplete"):
        lifecycle._inspect_artifacts(artifacts)

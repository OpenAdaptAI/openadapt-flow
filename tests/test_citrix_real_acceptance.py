from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PATH = REPO / "benchmark/citrix_ica_hdx/run_real_acceptance.py"
spec = importlib.util.spec_from_file_location("citrix_real_acceptance", PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _config() -> dict:
    return {
        "schema_version": "openadapt.citrix-real-acceptance.v1", "infrastructure_operation": "none",
        "fingerprints": {key: {"version": "test"} for key in mod.REQUIRED_FINGERPRINTS},
        "independent_oracle": {"screen_only": False, "command": ["oracle"]},
        "trials": [
            {"id": f"healthy-{n}", "condition": "healthy", "expected": "VERIFIED", "run_command": ["run"]}
            for n in range(3)
        ] + [
            {"id": condition, "condition": condition, "expected": "HALTED", "run_command": ["run"]}
            for condition in sorted(mod.REQUIRED_CONDITIONS - {"healthy"})
        ],
    }


def test_preflight_requires_all_fingerprints_conditions_and_an_independent_oracle(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config()))
    assert mod._load(path)["infrastructure_operation"] == "none"
    bad = _config()
    bad["independent_oracle"]["screen_only"] = True
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="independent"):
        mod._load(path)


def test_preflight_refuses_infrastructure_mutation(tmp_path: Path) -> None:
    config = _config()
    config["infrastructure_operation"] = "start"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="cannot start"):
        mod._load(path)

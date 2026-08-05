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

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _write(path: Path, value: str | dict) -> dict:
    if isinstance(value, dict):
        path.write_text(json.dumps(value, sort_keys=True))
    else:
        path.write_text(value)
    return {"path": str(path), "sha256": mod._sha256(path)}


def _fingerprints(runner_sha: str, oracle_sha: str) -> dict:
    return {
        "citrix_workspace": {
            "product": "Citrix Workspace",
            "version": "1.2.3",
            "binary_sha256": SHA_A,
        },
        "ica_hdx": {
            "protocol": "ICA/HDX",
            "vda_version": "2603",
            "policy_sha256": SHA_A,
            "transport_sha256": SHA_B,
        },
        "application": {
            "name": "Synthetic App",
            "version": "7.4",
            "binary_sha256": SHA_B,
        },
        "session": {
            "session_id_sha256": SHA_A,
            "published_resource_sha256": SHA_B,
            "vda_sha256": SHA_C,
        },
        "display": {
            "width_px": 1280,
            "height_px": 800,
            "dpi_x": 96,
            "dpi_y": 96,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "monitor_topology_sha256": SHA_C,
            "window_mode": "published_application",
        },
        "runner": {
            "name": "OpenAdapt",
            "version": "1.0",
            "binary_sha256": runner_sha,
            "host_sha256": SHA_A,
        },
        "bundle": {
            "workflow_version": "1",
            "bundle_sha256": SHA_B,
            "source_commit": "d" * 40,
        },
        "verifier": {
            "name": "Customer Oracle",
            "version": "1",
            "binary_sha256": oracle_sha,
            "principal_sha256": SHA_C,
        },
        "environment": {
            "environment_sha256": SHA_B,
            "os_build": "Windows 11",
            "network_zone_sha256": SHA_A,
        },
    }


def _config(tmp_path: Path) -> dict:
    runner_path = tmp_path / "approved-runner"
    oracle_path = tmp_path / "read-only-oracle"
    runner_path.write_text("runner")
    oracle_path.write_text("oracle")
    runner_path.chmod(0o700)
    oracle_path.chmod(0o700)
    runner_sha = mod._sha256(runner_path)
    oracle_sha = mod._sha256(oracle_path)
    fingerprints = _fingerprints(runner_sha, oracle_sha)
    runner_approval = _write(
        tmp_path / "runner-approval.json",
        {
            "schema_version": "openadapt.customer-runner-approval.v1",
            "authority": "customer_approved",
            "principal_sha256": SHA_A,
            "executable_sha256": runner_sha,
            "session_id_sha256": fingerprints["session"]["session_id_sha256"],
            "allowed_operation": "citrix_acceptance_trial",
            "infrastructure_lifecycle_authority": False,
        },
    )
    oracle_approval = _write(
        tmp_path / "oracle-approval.json",
        {
            "schema_version": "openadapt.customer-oracle-approval.v1",
            "authority": "customer_approved",
            "principal_sha256": SHA_C,
            "executable_sha256": oracle_sha,
            "environment_sha256": fingerprints["environment"]["environment_sha256"],
            "allowed_operation": "read_only_effect_observation",
            "separately_authenticated": True,
            "write_authority": False,
        },
    )
    trials = []
    for condition, expected in mod.EXPECTED_OUTCOMES.items():
        for number in range(3):
            trials.append(
                {
                    "id": f"{condition}-{number}",
                    "condition": condition,
                    "expected": expected,
                    "entity_sha256": SHA_A,
                    "effect_contract_sha256": SHA_B,
                }
            )
    return {
        "schema_version": mod.SCHEMA,
        "fingerprints": fingerprints,
        "runner_authority": {
            "mode": "customer_approved_session_runner",
            "pre_existing_session": True,
            "infrastructure_lifecycle_authority": False,
            "command": [str(runner_path)],
            "executable_sha256": runner_sha,
            "principal_sha256": SHA_A,
            "approval_artifact": runner_approval,
        },
        "independent_oracle": {
            "command": [str(oracle_path)],
            "executable_sha256": oracle_sha,
            "authority": "authenticated_read_only",
            "principal_sha256": SHA_C,
            "approval_artifact": oracle_approval,
        },
        "trials": trials,
    }


def _load_from(tmp_path: Path, config: dict) -> dict:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return mod.load_config(path)


def test_complete_campaign_contract_passes_preflight(tmp_path: Path) -> None:
    assert len(_load_from(tmp_path, _config(tmp_path))["trials"]) == 24


def test_requires_three_trials_for_every_condition(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["trials"] = [
        trial
        for trial in config["trials"]
        if not (trial["condition"] == "ambiguity" and trial["id"].endswith("-2"))
    ]
    with pytest.raises(ValueError, match="three trials"):
        _load_from(tmp_path, config)


def test_fixed_expected_outcomes_cannot_be_weakened(tmp_path: Path) -> None:
    config = _config(tmp_path)
    row = next(t for t in config["trials"] if t["condition"] == "commit_timeout")
    row["expected"] = "VERIFIED"
    with pytest.raises(ValueError, match="HALTED_UNCERTAIN"):
        _load_from(tmp_path, config)


def test_fingerprints_are_exact_complete_and_structured(tmp_path: Path) -> None:
    config = _config(tmp_path)
    del config["fingerprints"]["display"]["dpi_x"]
    with pytest.raises(ValueError, match="display"):
        _load_from(tmp_path, config)


def test_rejects_arbitrary_per_trial_commands(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["trials"][0]["run_command"] = ["anything"]
    with pytest.raises(ValueError, match="exact trial binding"):
        _load_from(tmp_path, config)


def test_runner_requires_preexisting_session_and_exact_customer_approval(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config["runner_authority"]["pre_existing_session"] = False
    with pytest.raises(ValueError, match="must exist"):
        _load_from(tmp_path, config)

    config = _config(tmp_path)
    approval_path = Path(config["runner_authority"]["approval_artifact"]["path"])
    approval = json.loads(approval_path.read_text())
    approval["infrastructure_lifecycle_authority"] = True
    approval_path.write_text(json.dumps(approval))
    config["runner_authority"]["approval_artifact"]["sha256"] = mod._sha256(
        approval_path
    )
    with pytest.raises(ValueError, match="approval"):
        _load_from(tmp_path, config)


def test_pinned_executable_change_after_preflight_is_refused(tmp_path: Path) -> None:
    config = _load_from(tmp_path, _config(tmp_path))
    runner = Path(config["runner_authority"]["command"][0])
    runner.write_text("replaced")
    with pytest.raises(ValueError, match="changed after preflight"):
        mod._run_pinned(
            config["runner_authority"]["command"],
            config["runner_authority"]["executable_sha256"],
            ["execute-trial"],
        )


def test_oracle_requires_separate_read_only_identity_and_exact_approval(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config["independent_oracle"]["principal_sha256"] = SHA_A
    config["fingerprints"]["verifier"]["principal_sha256"] = SHA_A
    with pytest.raises(ValueError, match="separate principals"):
        _load_from(tmp_path, config)

    config = _config(tmp_path)
    config["independent_oracle"]["authority"] = "read_write"
    with pytest.raises(ValueError, match="read-only"):
        _load_from(tmp_path, config)


def _evidence(tmp_path: Path, name: str) -> dict:
    return _write(tmp_path / name, name)


def _oracle_observation(tmp_path: Path, trial: dict, phase: str) -> dict:
    return {
        "schema_version": "openadapt.citrix-oracle-observation.v1",
        "phase": phase,
        "trial_id": trial["id"],
        "entity_sha256": trial["entity_sha256"],
        "effect_contract_sha256": trial["effect_contract_sha256"],
        "principal_sha256": SHA_C,
        "authority": "authenticated_read_only",
        "effect_status": "REFUTED",
        "state_digest": SHA_A,
        "evidence": _evidence(tmp_path, f"oracle-{phase}.json"),
    }


def test_oracle_evidence_is_phase_trial_entity_effect_and_digest_bound(
    tmp_path: Path,
) -> None:
    trial = _config(tmp_path)["trials"][0]
    evidence = _oracle_observation(tmp_path, trial, "before")
    result = {"returncode": 0, "stdout": json.dumps(evidence), "stderr": ""}
    assert (
        mod._validate_oracle_evidence(
            result, trial=trial, phase="before", oracle_principal_sha256=SHA_C
        )["trial_id"]
        == trial["id"]
    )

    evidence["entity_sha256"] = SHA_B
    result["stdout"] = json.dumps(evidence)
    with pytest.raises(ValueError, match="entity_sha256"):
        mod._validate_oracle_evidence(
            result, trial=trial, phase="before", oracle_principal_sha256=SHA_C
        )

    evidence = _oracle_observation(tmp_path, trial, "before")
    evidence["evidence"]["sha256"] = SHA_C
    result["stdout"] = json.dumps(evidence)
    with pytest.raises(ValueError, match="digest"):
        mod._validate_oracle_evidence(
            result, trial=trial, phase="before", oracle_principal_sha256=SHA_C
        )


def _receipt(tmp_path: Path, trial: dict, config: dict) -> dict:
    is_timeout = trial["condition"] == "commit_timeout"
    delivery_state = {
        "healthy": "dispatched",
        "partial_effect": "dispatched",
        "commit_timeout": "uncertain",
    }.get(trial["condition"], "not_dispatched")
    transport_evidence = _write(
        tmp_path / f"transport-{trial['id']}.json",
        {
            "schema_version": "openadapt.citrix-retained-transport-evidence.v1",
            "proof_source": "native_workspace_session_diagnostics",
            "protocol": "ICA/HDX",
            "standin": False,
            "trial_id": trial["id"],
            "session_id_sha256": config["fingerprints"]["session"]["session_id_sha256"],
            "transport_sha256": config["fingerprints"]["ica_hdx"]["transport_sha256"],
            "workspace_version": config["fingerprints"]["citrix_workspace"]["version"],
            "vda_version": config["fingerprints"]["ica_hdx"]["vda_version"],
            "captured_at": "2026-08-06T12:00:00Z",
        },
    )
    return {
        "schema_version": "openadapt.citrix-trial-receipt.v1",
        "trial_id": trial["id"],
        "condition": trial["condition"],
        "outcome": trial["expected"],
        "delivery_state": delivery_state,
        "retry_count": 0,
        "reconciliation_required": is_timeout,
        "session_transport_proof": {
            "schema_version": "openadapt.citrix-session-transport-proof.v1",
            "kind": "real_ica_hdx",
            "trial_id": trial["id"],
            "session_id_sha256": config["fingerprints"]["session"]["session_id_sha256"],
            "transport_sha256": config["fingerprints"]["ica_hdx"]["transport_sha256"],
            "evidence": transport_evidence,
        },
    }


def test_each_receipt_requires_retained_real_ica_hdx_proof(tmp_path: Path) -> None:
    config = _config(tmp_path)
    trial = config["trials"][0]
    receipt = _receipt(tmp_path, trial, config)
    result = {"returncode": 0, "stdout": json.dumps(receipt), "stderr": ""}
    assert (
        mod._validate_runner_receipt(
            result, trial=trial, fingerprints=config["fingerprints"]
        )["session_transport_proof"]["kind"]
        == "real_ica_hdx"
    )

    receipt["session_transport_proof"]["kind"] = "standin"
    result["stdout"] = json.dumps(receipt)
    with pytest.raises(ValueError, match="real ICA/HDX"):
        mod._validate_runner_receipt(
            result, trial=trial, fingerprints=config["fingerprints"]
        )


def test_transport_evidence_cannot_be_a_relabelled_standin(tmp_path: Path) -> None:
    config = _config(tmp_path)
    trial = config["trials"][0]
    receipt = _receipt(tmp_path, trial, config)
    binding = receipt["session_transport_proof"]["evidence"]
    evidence_path = Path(binding["path"])
    evidence = json.loads(evidence_path.read_text())
    evidence["standin"] = True
    evidence_path.write_text(json.dumps(evidence))
    binding["sha256"] = mod._sha256(evidence_path)
    result = {"returncode": 0, "stdout": json.dumps(receipt), "stderr": ""}
    with pytest.raises(ValueError, match="standin"):
        mod._validate_runner_receipt(
            result, trial=trial, fingerprints=config["fingerprints"]
        )


def test_condition_delivery_state_is_fixed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    trial = next(t for t in config["trials"] if t["condition"] == "ambiguity")
    receipt = _receipt(tmp_path, trial, config)
    receipt["delivery_state"] = "dispatched"
    result = {"returncode": 0, "stdout": json.dumps(receipt), "stderr": ""}
    with pytest.raises(ValueError, match="not_dispatched"):
        mod._validate_runner_receipt(
            result, trial=trial, fingerprints=config["fingerprints"]
        )


def test_healthy_requires_refuted_before_confirmed_after_and_state_change(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    trial = config["trials"][0]
    receipt = _receipt(tmp_path, trial, config)
    receipt["delivery_state"] = "dispatched"
    before = _oracle_observation(tmp_path, trial, "before")
    after = _oracle_observation(tmp_path, trial, "after")
    after["effect_status"] = "CONFIRMED"
    after["state_digest"] = SHA_B
    assert mod._trial_passed(trial, receipt, before, after)
    before["effect_status"] = "CONFIRMED"
    assert not mod._trial_passed(trial, receipt, before, after)


def test_commit_timeout_stays_uncertain_never_retries_and_reconciles(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    trial = next(t for t in config["trials"] if t["condition"] == "commit_timeout")
    receipt = _receipt(tmp_path, trial, config)
    result = {"returncode": 0, "stdout": json.dumps(receipt), "stderr": ""}
    assert (
        mod._validate_runner_receipt(
            result, trial=trial, fingerprints=config["fingerprints"]
        )["outcome"]
        == "HALTED_UNCERTAIN"
    )

    receipt["retry_count"] = 1
    result["stdout"] = json.dumps(receipt)
    with pytest.raises(ValueError, match="must not retry"):
        mod._validate_runner_receipt(
            result, trial=trial, fingerprints=config["fingerprints"]
        )

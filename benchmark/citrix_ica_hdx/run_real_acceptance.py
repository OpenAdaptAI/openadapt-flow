#!/usr/bin/env python3
"""Run a counted, customer-local, real Citrix ICA/HDX acceptance campaign.

The public harness defines the contract. Customer-specific commands, approval
artifacts, identifiers, recipes, and evidence stay inside the private customer
boundary. The harness can actuate only through one approved runner executable
bound to a pre-existing Citrix session. It has no infrastructure lifecycle
operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "openadapt.citrix-real-acceptance.v2"
REPORT_SCHEMA = "openadapt.citrix-real-acceptance-report.v2"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
EXPECTED_OUTCOMES = {
    "healthy": "VERIFIED",
    "wrong_session_or_entity": "HALTED",
    "ambiguity": "HALTED",
    "stale_state": "HALTED",
    "display_drift": "HALTED",
    "partial_effect": "HALTED",
    "reconnect": "HALTED",
    "commit_timeout": "HALTED_UNCERTAIN",
}
REQUIRED_FINGERPRINT_FIELDS = {
    "citrix_workspace": {"product", "version", "binary_sha256"},
    "ica_hdx": {"protocol", "vda_version", "policy_sha256", "transport_sha256"},
    "application": {"name", "version", "binary_sha256"},
    "session": {"session_id_sha256", "published_resource_sha256", "vda_sha256"},
    "display": {
        "width_px",
        "height_px",
        "dpi_x",
        "dpi_y",
        "scale_x",
        "scale_y",
        "monitor_topology_sha256",
        "window_mode",
    },
    "runner": {"name", "version", "binary_sha256", "host_sha256"},
    "bundle": {"workflow_version", "bundle_sha256", "source_commit"},
    "verifier": {"name", "version", "binary_sha256", "principal_sha256"},
    "environment": {"environment_sha256", "os_build", "network_zone_sha256"},
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _require_nonempty(value: Any, label: str) -> None:
    if isinstance(value, str) and value.strip():
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return
    raise ValueError(f"{label} must be a non-empty structured fingerprint value")


def _validate_fingerprints(fingerprints: Any) -> None:
    if not isinstance(fingerprints, dict):
        raise ValueError("fingerprints must be an object")
    if set(fingerprints) != set(REQUIRED_FINGERPRINT_FIELDS):
        raise ValueError("fingerprints must contain the exact required component set")
    for component, fields in REQUIRED_FINGERPRINT_FIELDS.items():
        value = fingerprints[component]
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError(f"fingerprint {component} must contain {sorted(fields)}")
        for field, item in value.items():
            _require_nonempty(item, f"fingerprints.{component}.{field}")
            if field.endswith("sha256"):
                _require_sha256(item, f"fingerprints.{component}.{field}")
    if fingerprints["ica_hdx"]["protocol"] != "ICA/HDX":
        raise ValueError(
            "ICA/HDX protocol fingerprint must name the real ICA/HDX protocol"
        )
    source_commit = fingerprints["bundle"]["source_commit"]
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ValueError("bundle source_commit must be a full lowercase Git commit")


def _validate_file_binding(binding: Any, label: str) -> Path:
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise ValueError(f"{label} must contain only path and sha256")
    path = Path(binding["path"])
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError(f"{label}.path must be an existing absolute regular file")
    _require_sha256(binding["sha256"], f"{label}.sha256")
    if _sha256(path) != binding["sha256"]:
        raise ValueError(f"{label} digest does not match its retained file")
    return path


def _load_approval(binding: Any, label: str) -> dict:
    path = _validate_file_binding(binding, label)
    try:
        approval = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must contain one JSON approval object") from exc
    if not isinstance(approval, dict):
        raise ValueError(f"{label} must contain one JSON approval object")
    return approval


def _validate_authorities(data: dict) -> None:
    boundary = data.get("runner_authority")
    if not isinstance(boundary, dict):
        raise ValueError("runner_authority must be an object")
    required = {
        "mode",
        "pre_existing_session",
        "infrastructure_lifecycle_authority",
        "command",
        "executable_sha256",
        "principal_sha256",
        "approval_artifact",
    }
    if set(boundary) != required:
        raise ValueError("runner_authority has an incomplete or unknown field set")
    if boundary["mode"] != "customer_approved_session_runner":
        raise ValueError(
            "runner must use the customer-approved session-runner boundary"
        )
    if boundary["pre_existing_session"] is not True:
        raise ValueError("the Citrix session must exist before this harness starts")
    if boundary["infrastructure_lifecycle_authority"] is not False:
        raise ValueError("the runner must have no infrastructure lifecycle authority")
    command = boundary["command"]
    if not isinstance(command, list) or len(command) != 1:
        raise ValueError(
            "runner command must be one approved executable without arguments"
        )
    executable = Path(command[0])
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or executable.is_symlink()
        or not os.access(executable, os.X_OK)
    ):
        raise ValueError("runner executable must be an existing absolute regular file")
    _require_sha256(boundary["executable_sha256"], "runner executable_sha256")
    if _sha256(executable) != boundary["executable_sha256"]:
        raise ValueError("runner executable digest mismatch")
    if boundary["executable_sha256"] != data["fingerprints"]["runner"]["binary_sha256"]:
        raise ValueError("runner authority does not match the runner fingerprint")
    _require_sha256(boundary["principal_sha256"], "runner principal_sha256")
    runner_approval = _load_approval(
        boundary["approval_artifact"], "runner approval_artifact"
    )
    expected_runner_approval = {
        "schema_version": "openadapt.customer-runner-approval.v1",
        "authority": "customer_approved",
        "principal_sha256": boundary["principal_sha256"],
        "executable_sha256": boundary["executable_sha256"],
        "session_id_sha256": data["fingerprints"]["session"]["session_id_sha256"],
        "allowed_operation": "citrix_acceptance_trial",
        "infrastructure_lifecycle_authority": False,
    }
    if runner_approval != expected_runner_approval:
        raise ValueError("runner approval is not exact or session-bound")

    oracle = data.get("independent_oracle")
    if not isinstance(oracle, dict):
        raise ValueError("independent_oracle must be an object")
    oracle_required = {
        "command",
        "executable_sha256",
        "authority",
        "principal_sha256",
        "approval_artifact",
    }
    if set(oracle) != oracle_required:
        raise ValueError("independent_oracle has an incomplete or unknown field set")
    if oracle["authority"] != "authenticated_read_only":
        raise ValueError("the oracle authority must be authenticated and read-only")
    if oracle["principal_sha256"] == boundary["principal_sha256"]:
        raise ValueError("the oracle and runner must use separate principals")
    oracle_command = oracle["command"]
    if not isinstance(oracle_command, list) or len(oracle_command) != 1:
        raise ValueError(
            "oracle command must be one approved executable without arguments"
        )
    oracle_executable = Path(oracle_command[0])
    if (
        not oracle_executable.is_absolute()
        or not oracle_executable.is_file()
        or oracle_executable.is_symlink()
        or not os.access(oracle_executable, os.X_OK)
    ):
        raise ValueError("oracle executable must be an existing absolute regular file")
    _require_sha256(oracle["executable_sha256"], "oracle executable_sha256")
    if _sha256(oracle_executable) != oracle["executable_sha256"]:
        raise ValueError("oracle executable digest mismatch")
    if oracle["executable_sha256"] != data["fingerprints"]["verifier"]["binary_sha256"]:
        raise ValueError("oracle authority does not match the verifier fingerprint")
    if (
        oracle["principal_sha256"]
        != data["fingerprints"]["verifier"]["principal_sha256"]
    ):
        raise ValueError("oracle principal does not match the verifier fingerprint")
    oracle_approval = _load_approval(
        oracle["approval_artifact"], "oracle approval_artifact"
    )
    expected_oracle_approval = {
        "schema_version": "openadapt.customer-oracle-approval.v1",
        "authority": "customer_approved",
        "principal_sha256": oracle["principal_sha256"],
        "executable_sha256": oracle["executable_sha256"],
        "environment_sha256": data["fingerprints"]["environment"]["environment_sha256"],
        "allowed_operation": "read_only_effect_observation",
        "separately_authenticated": True,
        "write_authority": False,
    }
    if oracle_approval != expected_oracle_approval:
        raise ValueError("oracle approval is not exact, read-only, and separate")


def _validate_trials(trials: Any) -> None:
    if not isinstance(trials, list):
        raise ValueError("trials must be a list")
    counts = {condition: 0 for condition in EXPECTED_OUTCOMES}
    seen: set[str] = set()
    required = {
        "id",
        "condition",
        "expected",
        "entity_sha256",
        "effect_contract_sha256",
    }
    for trial in trials:
        if not isinstance(trial, dict) or set(trial) != required:
            raise ValueError("each trial must contain the exact trial binding fields")
        trial_id = trial["id"]
        if not isinstance(trial_id, str) or not trial_id or trial_id in seen:
            raise ValueError("trial ids must be non-empty and unique")
        seen.add(trial_id)
        condition = trial["condition"]
        if condition not in EXPECTED_OUTCOMES:
            raise ValueError(f"unknown condition {condition!r}")
        expected = EXPECTED_OUTCOMES[condition]
        if trial["expected"] != expected:
            raise ValueError(
                f"condition {condition} has fixed expected outcome {expected}"
            )
        _require_sha256(trial["entity_sha256"], f"trial {trial_id} entity_sha256")
        _require_sha256(
            trial["effect_contract_sha256"],
            f"trial {trial_id} effect_contract_sha256",
        )
        counts[condition] += 1
    short = [name for name, count in counts.items() if count < 3]
    if short:
        raise ValueError(
            f"at least three trials are required for every condition: {short}"
        )


def load_config(path: Path) -> dict:
    data = json.loads(path.read_text())
    if data.get("schema_version") != SCHEMA:
        raise ValueError("unsupported acceptance configuration schema")
    allowed = {
        "schema_version",
        "fingerprints",
        "runner_authority",
        "independent_oracle",
        "trials",
    }
    if set(data) != allowed:
        raise ValueError(
            "configuration has an incomplete or unknown top-level field set"
        )
    _validate_fingerprints(data["fingerprints"])
    _validate_authorities(data)
    _validate_trials(data["trials"])
    return data


def _run(command: list[str], args: list[str], *, timeout_s: int = 300) -> dict:
    result = subprocess.run(
        [*command, *args],
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr[-4000:],
    }


def _run_pinned(
    command: list[str],
    expected_sha256: str,
    args: list[str],
    *,
    timeout_s: int = 300,
) -> dict:
    executable = Path(command[0])
    if (
        not executable.is_file()
        or executable.is_symlink()
        or _sha256(executable) != expected_sha256
    ):
        raise ValueError("approved executable changed after preflight")
    return _run(command, args, timeout_s=timeout_s)


def _load_command_json(result: dict, label: str) -> dict:
    if result["returncode"] != 0:
        raise ValueError(f"{label} command failed")
    try:
        value = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must return one JSON object")
    return value


def _validate_oracle_evidence(
    result: dict,
    *,
    trial: dict,
    phase: str,
    oracle_principal_sha256: str,
) -> dict:
    evidence = _load_command_json(result, f"oracle {phase}")
    required = {
        "schema_version",
        "phase",
        "trial_id",
        "entity_sha256",
        "effect_contract_sha256",
        "principal_sha256",
        "authority",
        "effect_status",
        "state_digest",
        "evidence",
    }
    if set(evidence) != required:
        raise ValueError(f"oracle {phase} evidence has incomplete or unknown fields")
    expected = {
        "schema_version": "openadapt.citrix-oracle-observation.v1",
        "phase": phase,
        "trial_id": trial["id"],
        "entity_sha256": trial["entity_sha256"],
        "effect_contract_sha256": trial["effect_contract_sha256"],
        "principal_sha256": oracle_principal_sha256,
        "authority": "authenticated_read_only",
    }
    for key, value in expected.items():
        if evidence[key] != value:
            raise ValueError(f"oracle {phase} evidence is not bound to {key}")
    if evidence["effect_status"] not in {"CONFIRMED", "REFUTED", "INDETERMINATE"}:
        raise ValueError(f"oracle {phase} effect_status is invalid")
    _require_sha256(evidence["state_digest"], f"oracle {phase} state_digest")
    _validate_file_binding(evidence["evidence"], f"oracle {phase} evidence")
    return evidence


def _validate_session_proof(proof: Any, *, trial: dict, fingerprints: dict) -> dict:
    required = {
        "schema_version",
        "kind",
        "trial_id",
        "session_id_sha256",
        "transport_sha256",
        "evidence",
    }
    if not isinstance(proof, dict) or set(proof) != required:
        raise ValueError("session transport proof has incomplete or unknown fields")
    if proof["schema_version"] != "openadapt.citrix-session-transport-proof.v1":
        raise ValueError("session transport proof schema is invalid")
    if proof["kind"] != "real_ica_hdx":
        raise ValueError("each trial requires retained real ICA/HDX proof")
    if proof["trial_id"] != trial["id"]:
        raise ValueError("session transport proof is not bound to the trial")
    if proof["session_id_sha256"] != fingerprints["session"]["session_id_sha256"]:
        raise ValueError(
            "session transport proof does not match the session fingerprint"
        )
    if proof["transport_sha256"] != fingerprints["ica_hdx"]["transport_sha256"]:
        raise ValueError(
            "session transport proof does not match the transport fingerprint"
        )
    evidence_path = _validate_file_binding(
        proof["evidence"], "session transport evidence"
    )
    try:
        evidence = json.loads(evidence_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError("session transport evidence must be structured JSON") from exc
    required_evidence = {
        "schema_version",
        "proof_source",
        "protocol",
        "standin",
        "trial_id",
        "session_id_sha256",
        "transport_sha256",
        "workspace_version",
        "vda_version",
        "captured_at",
    }
    if not isinstance(evidence, dict) or set(evidence) != required_evidence:
        raise ValueError("session transport evidence has incomplete or unknown fields")
    expected_evidence = {
        "schema_version": "openadapt.citrix-retained-transport-evidence.v1",
        "proof_source": "native_workspace_session_diagnostics",
        "protocol": "ICA/HDX",
        "standin": False,
        "trial_id": trial["id"],
        "session_id_sha256": fingerprints["session"]["session_id_sha256"],
        "transport_sha256": fingerprints["ica_hdx"]["transport_sha256"],
        "workspace_version": fingerprints["citrix_workspace"]["version"],
        "vda_version": fingerprints["ica_hdx"]["vda_version"],
    }
    for key, value in expected_evidence.items():
        if evidence[key] != value:
            raise ValueError(f"session transport evidence is not bound to {key}")
    _require_nonempty(evidence["captured_at"], "session transport captured_at")
    return proof


def _validate_runner_receipt(result: dict, *, trial: dict, fingerprints: dict) -> dict:
    receipt = _load_command_json(result, "session runner")
    required = {
        "schema_version",
        "trial_id",
        "condition",
        "outcome",
        "delivery_state",
        "retry_count",
        "session_transport_proof",
        "reconciliation_required",
    }
    if set(receipt) != required:
        raise ValueError("session runner receipt has incomplete or unknown fields")
    if receipt["schema_version"] != "openadapt.citrix-trial-receipt.v1":
        raise ValueError("session runner receipt schema is invalid")
    if receipt["trial_id"] != trial["id"] or receipt["condition"] != trial["condition"]:
        raise ValueError("session runner receipt is not bound to the trial")
    if receipt["outcome"] != trial["expected"]:
        raise ValueError(
            "session runner receipt does not match the fixed expected outcome"
        )
    if receipt["retry_count"] != 0:
        raise ValueError("a counted acceptance trial must not retry")
    _validate_session_proof(
        receipt["session_transport_proof"],
        trial=trial,
        fingerprints=fingerprints,
    )
    required_delivery = {
        "healthy": "dispatched",
        "wrong_session_or_entity": "not_dispatched",
        "ambiguity": "not_dispatched",
        "stale_state": "not_dispatched",
        "display_drift": "not_dispatched",
        "partial_effect": "dispatched",
        "reconnect": "not_dispatched",
        "commit_timeout": "uncertain",
    }[trial["condition"]]
    if receipt["delivery_state"] != required_delivery:
        raise ValueError(
            f"condition {trial['condition']} requires delivery state {required_delivery}"
        )
    if trial["condition"] == "commit_timeout":
        if receipt["reconciliation_required"] is not True:
            raise ValueError("commit-timeout requires independent reconciliation")
    elif receipt["reconciliation_required"] is not False:
        raise ValueError("only commit-timeout uses the uncertainty reconciliation path")
    return receipt


def _trial_passed(trial: dict, receipt: dict, before: dict, after: dict) -> bool:
    if trial["condition"] == "healthy":
        return (
            before["effect_status"] == "REFUTED"
            and after["effect_status"] == "CONFIRMED"
            and before["state_digest"] != after["state_digest"]
        )
    if trial["condition"] == "commit_timeout":
        # The terminal result stays HALTED_UNCERTAIN even if reconciliation
        # later proves or refutes the effect. A blind retry is never allowed.
        return after["effect_status"] in {"CONFIRMED", "REFUTED", "INDETERMINATE"}
    if trial["condition"] == "partial_effect":
        return before["state_digest"] != after["state_digest"] and after[
            "effect_status"
        ] in {"REFUTED", "INDETERMINATE"}
    return (
        before["state_digest"] == after["state_digest"]
        and before["effect_status"] == after["effect_status"]
    )


def run_campaign(config: dict) -> dict:
    report = {
        "schema_version": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {"platform": platform.platform(), "python": sys.version.split()[0]},
        "fingerprints": config["fingerprints"],
        "trials": [],
    }
    runner = config["runner_authority"]["command"]
    runner_sha256 = config["runner_authority"]["executable_sha256"]
    oracle = config["independent_oracle"]["command"]
    oracle_sha256 = config["independent_oracle"]["executable_sha256"]
    oracle_principal = config["independent_oracle"]["principal_sha256"]
    for trial in config["trials"]:
        binding_args = [
            "--trial-id",
            trial["id"],
            "--entity-sha256",
            trial["entity_sha256"],
            "--effect-contract-sha256",
            trial["effect_contract_sha256"],
        ]
        before_result = _run_pinned(
            oracle,
            oracle_sha256,
            ["observe", "--phase", "before", *binding_args],
        )
        before = _validate_oracle_evidence(
            before_result,
            trial=trial,
            phase="before",
            oracle_principal_sha256=oracle_principal,
        )
        runner_result = _run_pinned(
            runner,
            runner_sha256,
            ["execute-trial", "--condition", trial["condition"], *binding_args],
        )
        receipt = _validate_runner_receipt(
            runner_result,
            trial=trial,
            fingerprints=config["fingerprints"],
        )
        after_result = _run_pinned(
            oracle,
            oracle_sha256,
            ["observe", "--phase", "after", *binding_args],
        )
        after = _validate_oracle_evidence(
            after_result,
            trial=trial,
            phase="after",
            oracle_principal_sha256=oracle_principal,
        )
        report["trials"].append(
            {
                "id": trial["id"],
                "condition": trial["condition"],
                "expected": trial["expected"],
                "passed": _trial_passed(trial, receipt, before, after),
                "receipt": receipt,
                "oracle_before": before,
                "oracle_after": after,
            }
        )
    report["accepted"] = all(row["passed"] for row in report["trials"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="invoke the approved runner against the confirmed pre-existing session",
    )
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        report = {
            "schema_version": REPORT_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
            "preflight": "passed",
            "executed": bool(args.execute),
            "fingerprints": config["fingerprints"],
            "trials": [],
        }
        if args.execute:
            report = run_campaign(config)
            report["config_sha256"] = hashlib.sha256(
                args.config.read_bytes()
            ).hexdigest()
            report["preflight"] = "passed"
            report["executed"] = True
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"acceptance refused: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if not args.execute:
        print("preflight passed; no trial ran")
        return 0
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

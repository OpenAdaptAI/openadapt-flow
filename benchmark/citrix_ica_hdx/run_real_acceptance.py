#!/usr/bin/env python3
"""Run a counted, customer-local Citrix ICA/HDX acceptance campaign.

This is a public harness mechanism.  The configuration, commands, record
identifiers, and oracle recipe belong in a private repository.  It never starts
or stops cloud resources.  It defaults to a validation-only preflight; passing
``--execute`` is the deliberate, one-command actuation acknowledgement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_FINGERPRINTS = {
    "citrix_workspace", "ica_hdx", "application", "session", "display",
    "runner", "bundle", "verifier", "environment",
}
REQUIRED_CONDITIONS = {
    "healthy", "wrong_session_or_entity", "ambiguity", "stale_state",
    "display_drift", "partial_effect", "reconnect", "commit_timeout",
}


def _load(path: Path) -> dict:
    data = json.loads(path.read_text())
    if data.get("schema_version") != "openadapt.citrix-real-acceptance.v1":
        raise ValueError("unsupported acceptance configuration schema")
    if data.get("infrastructure_operation") != "none":
        raise ValueError("this harness cannot start, stop, or create infrastructure")
    missing = REQUIRED_FINGERPRINTS - set(data.get("fingerprints", {}))
    if missing:
        raise ValueError(f"missing fingerprints: {', '.join(sorted(missing))}")
    oracle = data.get("independent_oracle", {})
    if not oracle.get("command") or oracle.get("screen_only") is not False:
        raise ValueError("an independent non-screen oracle command is required")
    trials = data.get("trials", [])
    conditions = {trial.get("condition") for trial in trials}
    absent = REQUIRED_CONDITIONS - conditions
    if absent:
        raise ValueError(f"missing required conditions: {', '.join(sorted(absent))}")
    healthy = [t for t in trials if t.get("condition") == "healthy"]
    if len(healthy) < 3:
        raise ValueError("at least three healthy trials are required")
    for trial in trials:
        if not trial.get("id") or not trial.get("run_command"):
            raise ValueError("each trial requires an id and a run_command")
        if trial.get("expected") not in {"VERIFIED", "HALTED"}:
            raise ValueError("each trial expected result must be VERIFIED or HALTED")
    return data


def _run(command: list[str], *, timeout_s: int) -> dict:
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout_s)
    return {"returncode": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="run the declared customer-local trials")
    args = parser.parse_args()
    try:
        config = _load(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"preflight refused: {exc}", file=sys.stderr)
        return 2

    preflight = {
        "schema_version": "openadapt.citrix-real-acceptance-report.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "host": {"platform": platform.platform(), "python": sys.version.split()[0]},
        "fingerprints": config["fingerprints"],
        "independent_oracle": {k: v for k, v in config["independent_oracle"].items() if k != "command"},
        "infrastructure_operation": "none",
        "preflight": "passed",
        "executed": bool(args.execute),
        "trials": [],
    }
    if not args.execute:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(preflight, indent=2, sort_keys=True) + "\n")
        print("preflight passed; no trial ran. Re-run with --execute after a supervised operator confirms the session.")
        return 0

    oracle_command = config["independent_oracle"]["command"]
    for trial in config["trials"]:
        timeout_s = int(trial.get("timeout_s", 120))
        before = _run(oracle_command + ["before", trial["id"]], timeout_s=timeout_s)
        run = _run(trial["run_command"], timeout_s=timeout_s)
        after = _run(oracle_command + ["after", trial["id"]], timeout_s=timeout_s)
        # Oracle output is retained locally.  The oracle command must emit JSON
        # with ``outcome``; only it can grant VERIFIED after a consequential write.
        try:
            observed = json.loads(after["stdout"]).get("outcome")
        except json.JSONDecodeError:
            observed = None
        passed = observed == trial["expected"] and run["returncode"] == 0
        preflight["trials"].append({"id": trial["id"], "condition": trial["condition"], "expected": trial["expected"], "observed": observed, "passed": passed, "run": run, "oracle_before": before, "oracle_after": after})
    preflight["accepted"] = all(t["passed"] for t in preflight["trials"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(preflight, indent=2, sort_keys=True) + "\n")
    return 0 if preflight["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

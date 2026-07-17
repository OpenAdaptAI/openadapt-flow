#!/usr/bin/env python3
"""Opt-in native macOS TextEdit qualification with independent file oracle.

Nothing runs unless both macOS permissions are present. Each trial launches a
new isolated TextEdit process against a unique file under /tmp, replaces its
contents using the MacOSBackend, saves with Command+S, and confirms the exact
bytes from disk. The ambiguity trial opens two isolated documents whose titles
share a selector and requires a fail-closed refusal with both files unchanged.

This is local substrate evidence, not a general reliability claim. It records
the task, environment, run count, oracle, failures, cleanup state, and caveats.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.backends.macos_backend import MacOSBackend, MacOSBackendError
from openadapt_flow.backends.remote_display import MacWindowClient, WindowInfo

MIN_TRIALS = 3
TEXTEDIT_APP = "TextEdit"


def file_oracle(path: Path, expected: bytes) -> dict[str, Any]:
    """Independent system-of-record verdict for the saved TextEdit document."""
    try:
        observed = path.read_bytes()
    except OSError as error:
        return {"status": "unverifiable", "error": str(error)}
    return {
        "status": "confirmed" if observed == expected else "refuted",
        "expected_bytes": len(expected),
        "observed_bytes": len(observed),
    }


def _wait_for_matches(
    client: MacWindowClient,
    title: str,
    *,
    count: int,
    timeout_s: float = 10.0,
) -> list[WindowInfo]:
    deadline = time.monotonic() + timeout_s
    matches: list[WindowInfo] = []
    while time.monotonic() < deadline:
        matches = client.find_windows(TEXTEDIT_APP, title)
        if len(matches) == count:
            return matches
        time.sleep(0.1)
    raise RuntimeError(
        f"expected {count} TextEdit window(s) matching {title!r}; found {len(matches)}"
    )


def _open_isolated(path: Path) -> None:
    subprocess.run(
        ["open", "-n", "-a", TEXTEDIT_APP, str(path)],
        check=True,
        capture_output=True,
        timeout=10,
    )


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _terminate_isolated(pid: int) -> None:
    """Terminate only the exact `open -n` process created by this harness."""
    if not _process_exists(pid):
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)


def _close_target(
    client: MacWindowClient, title: str, pid: int, *, errors: list[str]
) -> None:
    try:
        # Re-opening an already-open file makes that exact document the main
        # window of its isolated TextEdit process; the backend still verifies
        # the exact topmost window before emitting Command+W.
        matches = client.find_windows(TEXTEDIT_APP, title)
        if len(matches) == 1:
            backend = MacOSBackend(
                client,
                app=TEXTEDIT_APP,
                window_title=title,
                settle_s=0.01,
            )
            backend.press("ControlOrMeta+w")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not client.find_windows(TEXTEDIT_APP, title):
                    break
                time.sleep(0.05)
    except Exception as error:  # noqa: BLE001 - cleanup continues to exact pid
        errors.append(f"window cleanup {title!r}: {error}")
    try:
        _terminate_isolated(pid)
    except Exception as error:  # noqa: BLE001
        errors.append(f"process cleanup pid={pid}: {error}")


def _wait_oracle(path: Path, expected: bytes, timeout_s: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    verdict = file_oracle(path, expected)
    while verdict["status"] != "confirmed" and time.monotonic() < deadline:
        time.sleep(0.1)
        verdict = file_oracle(path, expected)
    return verdict


def _run_trial(
    client: MacWindowClient,
    root: Path,
    run_id: str,
    trial: int,
    cleanup_errors: list[str],
) -> dict[str, Any]:
    title = f"oa-macos-{run_id}-trial-{trial}"
    path = root / f"{title}.txt"
    baseline = f"baseline {trial}\n".encode()
    expected = f"OpenAdapt native macOS trial {trial} {run_id}\n".encode()
    path.write_bytes(baseline)
    pid: Optional[int] = None
    started = time.monotonic()
    try:
        _open_isolated(path)
        matches = _wait_for_matches(client, title, count=1)
        pid = matches[0].pid
        backend = MacOSBackend(
            client,
            app=TEXTEDIT_APP,
            window_title=title,
            settle_s=0.01,
        )
        frame = backend.screenshot()
        backend.press("ControlOrMeta+a")
        backend.type_text(expected.decode())
        backend.press("ControlOrMeta+s")
        oracle = _wait_oracle(path, expected)
        return {
            "trial": trial,
            "status": "passed" if oracle["status"] == "confirmed" else "failed",
            "oracle": oracle,
            "frame_bytes": len(frame),
            "duration_s": round(time.monotonic() - started, 3),
        }
    except Exception as error:  # noqa: BLE001 - evidence records exact failure
        return {
            "trial": trial,
            "status": "failed",
            "failure_type": type(error).__name__,
            "error": str(error),
            "oracle": file_oracle(path, expected),
            "duration_s": round(time.monotonic() - started, 3),
        }
    finally:
        if pid is not None:
            _close_target(client, title, pid, errors=cleanup_errors)


def _run_ambiguity_trial(
    client: MacWindowClient,
    root: Path,
    run_id: str,
    cleanup_errors: list[str],
) -> dict[str, Any]:
    selector = f"oa-macos-{run_id}-ambiguous"
    paths = [root / f"{selector}-{suffix}.txt" for suffix in ("a", "b")]
    baseline = b"must remain unchanged\n"
    pids: dict[str, int] = {}
    try:
        for path in paths:
            path.write_bytes(baseline)
            _open_isolated(path)
            exact = _wait_for_matches(client, path.stem, count=1)
            pids[path.stem] = exact[0].pid
        matches = _wait_for_matches(client, selector, count=2)
        backend = MacOSBackend(
            client,
            app=TEXTEDIT_APP,
            window_title=selector,
            settle_s=0.01,
        )
        try:
            backend.screenshot()
        except MacOSBackendError as error:
            refused = "ambiguous native macOS target" in str(error)
            oracles = [file_oracle(path, baseline) for path in paths]
            passed = refused and all(item["status"] == "confirmed" for item in oracles)
            return {
                "status": "passed" if passed else "failed",
                "matched_windows": len(matches),
                "refused": refused,
                "error": str(error),
                "oracles": oracles,
            }
        return {
            "status": "failed",
            "matched_windows": len(matches),
            "refused": False,
            "error": "ambiguous selector did not halt",
            "oracles": [file_oracle(path, baseline) for path in paths],
        }
    except Exception as error:  # noqa: BLE001
        return {
            "status": "failed",
            "failure_type": type(error).__name__,
            "error": str(error),
            "oracles": [file_oracle(path, baseline) for path in paths],
        }
    finally:
        for title, pid in pids.items():
            _close_target(client, title, pid, errors=cleanup_errors)


def _permission_report(client: MacWindowClient) -> dict[str, bool]:
    return {
        "screen_recording": client.capture_trusted(),
        "accessibility": client.input_trusted(),
    }


def qualify(trials: int) -> tuple[int, dict[str, Any]]:
    client = MacWindowClient()
    permissions = _permission_report(client)
    base: dict[str, Any] = {
        "schema_version": 1,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "task": "replace and save a unique /tmp TextEdit document",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "permissions": permissions,
        },
        "trials_required": trials,
        "oracle": "exact bytes read independently from the target file",
        "failure_taxonomy": [
            "permission_refusal",
            "target_absent",
            "target_ambiguous",
            "wrong_foreground_window",
            "capture_failure",
            "input_delivery_failure",
            "file_effect_refuted",
            "cleanup_failure",
        ],
        "caveats": [
            "TextEdit only; no AX structural resolution claim",
            "synthetic local files; not a design-partner workflow",
            "one macOS host and active keyboard/session state",
        ],
    }
    missing = [name for name, granted in permissions.items() if not granted]
    if missing:
        base.update(
            {
                "status": "blocked",
                "trials_completed": 0,
                "missing_permissions": missing,
                "permission_step": (
                    "Run this script once with --request-permissions, approve "
                    "both macOS prompts for the app that launches Codex/Flow, "
                    "then restart that app and rerun the qualification."
                ),
            }
        )
        return 2, base

    run_id = uuid.uuid4().hex[:10]
    root = Path(tempfile.mkdtemp(prefix=f"openadapt-macos-{run_id}-"))
    original_frontmost = client.frontmost_pid()
    cleanup_errors: list[str] = []
    results: list[dict[str, Any]] = []
    ambiguity: dict[str, Any] = {"status": "not_run"}
    try:
        results = [
            _run_trial(client, root, run_id, trial, cleanup_errors)
            for trial in range(1, trials + 1)
        ]
        ambiguity = _run_ambiguity_trial(client, root, run_id, cleanup_errors)
    finally:
        if original_frontmost is not None:
            client.activate(original_frontmost)
        try:
            shutil.rmtree(root)
        except Exception as error:  # noqa: BLE001
            cleanup_errors.append(f"temporary directory cleanup {root}: {error}")

    passed = (
        all(result["status"] == "passed" for result in results)
        and ambiguity["status"] == "passed"
        and not cleanup_errors
    )
    base.update(
        {
            "status": "passed" if passed else "failed",
            "trials_completed": len(results),
            "trials": results,
            "ambiguity_trial": ambiguity,
            "cleanup_errors": cleanup_errors,
            "silent_incorrect_successes": sum(
                result.get("status") == "passed"
                and result.get("oracle", {}).get("status") != "confirmed"
                for result in results
            ),
            "over_halts": sum(result.get("status") == "failed" for result in results),
        }
    )
    return (0 if passed else 1), base


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=MIN_TRIALS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--request-permissions", action="store_true")
    args = parser.parse_args()
    if args.trials < MIN_TRIALS:
        parser.error(f"--trials must be >= {MIN_TRIALS}")

    client = MacWindowClient()
    if args.request_permissions:
        client.request_capture_access()
        client.request_input_access()
        print(
            "macOS permission prompts requested. Approve Screen & System Audio "
            "Recording and Accessibility for the app that launches this process, "
            "restart it, then rerun without --request-permissions."
        )
        return 2

    code, report = qualify(args.trials)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

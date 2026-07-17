"""Snapshot-safe, opt-in real RDP protocol qualification on Parallels.

This is deliberately separate from the Citrix/remote-window analog. It opens
exactly three independent network RDP sessions through
:class:`AardwolfTransport`, captures a painted framebuffer, sends a unique
command through RDP keyboard input, then verifies the resulting file through
``prlctl exec``. The oracle is outside the protocol/client surface under test.

Safety:

* skipped unless ``OAFLOW_PARALLELS_RDP_E2E=1``;
* requires the preserved base snapshot to be current before any mutation;
* snapshots before resume/account/firewall changes;
* creates only a random qualification account and Public Documents directory;
* switches back to the preserved base and deletes only the exact
  harness-owned snapshot id, never a pre-existing snapshot or user asset;
* binds the result to exact base/candidate commits and exactly three trials;
* reports silent-incorrect, over-halt, and model-call counts explicitly.

macOS Local Network permission is executable-specific. Run this with a Python
3.10-3.12 interpreter that can connect to the Parallels shared subnet and has
``openadapt-flow[rdp]`` installed.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

RUN = os.environ.get("OAFLOW_PARALLELS_RDP_E2E") == "1"
TRIALS = int(os.environ.get("OAFLOW_RDP_QUAL_TRIALS", "3"))
CANDIDATE_ENV = "OAFLOW_RDP_QUAL_CANDIDATE"
BASE_COMMIT_ENV = "OAFLOW_RDP_QUAL_BASE_COMMIT"
BASE_SNAPSHOT_ENV = "OAFLOW_PARALLELS_BASE_SNAPSHOT_ID"
HOST_STORAGE_PATH_ENV = "OAFLOW_PARALLELS_STORAGE_PATH"

pytestmark = [
    pytest.mark.skipif(
        not RUN,
        reason="opt-in live RDP qualification; set OAFLOW_PARALLELS_RDP_E2E=1",
    ),
    pytest.mark.timeout(1200),
]


def _wait_guest(vm, *, timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            result = vm.exec_cmd("echo OAFLOW_RDP_READY", timeout=20)
            if result.returncode == 0 and "OAFLOW_RDP_READY" in result.stdout:
                return
        except Exception:  # noqa: BLE001 - bounded readiness polling
            pass
        time.sleep(3)
    raise AssertionError("Parallels guest tools did not become ready")


def _wait_tcp(host: str, port: int, *, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(1)
    raise AssertionError(f"RDP listener {host}:{port} unreachable: {last_error}")


def _wait_user_shell(vm, account: str, *, timeout_s: float = 90.0) -> None:
    """Wait until the RDP account's Explorer shell is present after first logon."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = vm.exec_cmd('tasklist /V /FI "IMAGENAME eq explorer.exe"', timeout=20)
        if result.returncode == 0 and account.lower() in (result.stdout or "").lower():
            return
        time.sleep(2)
    raise AssertionError(f"RDP desktop shell did not become ready for {account}")


def _oracle_read(vm, guest_path: str, *, timeout_s: float = 30.0) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = vm.exec_cmd(f'type "{guest_path}" 2>nul', timeout=20)
        if result.returncode == 0 and (result.stdout or "").strip():
            return (result.stdout or "").strip()
        time.sleep(0.5)
    return None


def _wait_state(vm, expected: str, *, timeout_s: float = 60.0) -> str:
    deadline = time.monotonic() + timeout_s
    observed = vm.status()
    while time.monotonic() < deadline:
        observed = vm.status()
        if observed == expected:
            return observed
        time.sleep(2)
    return observed


def _painted_readiness(png: bytes) -> bool:
    """Protocol-test readiness: valid, nonblank pixels (not app certification)."""
    try:
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        image = Image.open(io.BytesIO(png)).convert("RGB")
        colors = image.getcolors(maxcolors=1 << 24)
        return colors is None or len(colors) > 1
    except Exception:  # noqa: BLE001 - malformed frame means not ready
        return False


def _assert_painted_frame(png: bytes, expected_size: tuple[int, int]) -> None:
    assert _painted_readiness(png), "RDP returned an invalid/blank frame"
    assert Image.open(io.BytesIO(png)).size == expected_size


def _write_summary(path: Path, summary: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


def _git_revision(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_real_rdp_three_trial_independent_oracle(tmp_path) -> None:
    from openadapt_flow.backends.parallels_vm import (
        DEFAULT_VM_UUID,
        ParallelsError,
        ParallelsVM,
    )
    from openadapt_flow.backends.rdp_backend import AardwolfTransport, FreeRDPBackend

    vm = ParallelsVM(os.environ.get("OAFLOW_PARALLELS_VM_UUID", DEFAULT_VM_UUID))
    candidate_commit = os.environ.get(CANDIDATE_ENV)
    base_commit = os.environ.get(BASE_COMMIT_ENV)
    base_snapshot_id = os.environ.get(BASE_SNAPSHOT_ENV)
    report_path = Path(
        os.environ.get(
            "OAFLOW_RDP_QUAL_REPORT", str(tmp_path / "rdp_qualification.json")
        )
    ).resolve()
    sha_pattern = r"[0-9a-f]{40}"
    if candidate_commit is None or re.fullmatch(sha_pattern, candidate_commit) is None:
        raise RuntimeError(f"{CANDIDATE_ENV} must name one exact candidate commit")
    if base_commit is None or re.fullmatch(sha_pattern, base_commit) is None:
        raise RuntimeError(f"{BASE_COMMIT_ENV} must name one exact base commit")
    if base_snapshot_id is None:
        raise RuntimeError(f"{BASE_SNAPSHOT_ENV} is required for safe cleanup")
    if TRIALS != 3:
        raise RuntimeError("counted RDP qualification requires exactly 3 trials")
    head_commit = _git_revision("rev-parse", "HEAD")
    if head_commit != candidate_commit:
        raise RuntimeError(
            f"candidate mismatch: environment={candidate_commit}, HEAD={head_commit}"
        )
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_commit, candidate_commit],
        check=False,
    )
    if ancestry.returncode != 0:
        raise RuntimeError("declared base commit is not an ancestor of the candidate")

    initial_state = vm.status()
    if initial_state != "suspended":
        raise RuntimeError(
            f"qualification base must begin suspended (state={initial_state!r})"
        )
    snapshots_before = vm.list_snapshots()
    if not any(
        item.snapshot_id == base_snapshot_id and item.current
        for item in snapshots_before
    ):
        raise RuntimeError("declared preserved base snapshot is not current")
    storage_path = os.environ.get(HOST_STORAGE_PATH_ENV, os.getcwd())
    free_bytes_before = vm.require_host_free_space(storage_path=storage_path)

    account = f"oaflowq_{secrets.token_hex(4)}"
    # 13 chars; upper/lower/special and usually digits, within legacy net.exe's
    # non-interactive 14-character prompt threshold.
    password = f"Qa!{secrets.token_hex(5)}"
    guest_dir = r"C:\Users\Public\Documents\OpenAdaptRDPQualification"
    width = int(os.environ.get("OPENADAPT_FLOW_RDP_WIDTH", "1280"))
    height = int(os.environ.get("OPENADAPT_FLOW_RDP_HEIGHT", "800"))
    port = int(os.environ.get("OPENADAPT_FLOW_RDP_PORT", "3389"))
    rows: list[dict[str, object]] = []
    snapshot_id: str | None = None
    cleanup: dict[str, object] = {
        "attempted": False,
        "passed": False,
        "base_snapshot_current_after": False,
        "owned_snapshot_deleted": False,
        "restored_state": None,
        "snapshot_ids_after": [],
        "snapshot_inventory_restored": False,
        "error_type": None,
    }
    environment: dict[str, object] = {
        "vm": vm.uuid,
        "base_snapshot_id": base_snapshot_id,
        "initial_state": initial_state,
        "snapshot_ids_before": [item.snapshot_id for item in snapshots_before],
        "framebuffer": [width, height],
        "transport": "aardwolf",
        "host_free_bytes_before": free_bytes_before,
    }
    summary_base: dict[str, object] = {
        "schema_version": "openadapt.rdp-qualification.v1",
        "candidate_commit": candidate_commit,
        "base_commit": base_commit,
        "substrate": "real-rdp-aardwolf-parallels-win11",
        "task": "create a unique file through the Windows Run dialog over RDP",
        "environment": environment,
        "oracle": "prlctl exec type <unique file written only via RDP input>",
        "readiness": (
            "current-frame valid/nonblank guard for protocol qualification; "
            "not a deployment app-identity or lock-screen certification"
        ),
        "failure_taxonomy": [
            "connect_or_frame_failure",
            "input_delivery_failure",
            "independent_oracle_mismatch",
            "over_halt_or_timeout",
            "environment_restore_failure",
        ],
        "caveat": "This qualifies RDP transport/input only, not Citrix ICA/HDX.",
    }

    def current_summary() -> dict[str, object]:
        failure_counts: dict[str, int] = {}
        for row in rows:
            label = str(row["failure_class"] or "none")
            failure_counts[label] = failure_counts.get(label, 0) + 1
        successes = sum(bool(row["exact"]) for row in rows)
        silent_incorrect = sum(
            bool(row["input_returned_without_error"])
            and row["observed"] is not None
            and row["observed"] != row["expected"]
            for row in rows
        )
        over_halts = sum(row["failure_class"] == "over_halt_or_timeout" for row in rows)
        model_calls = sum(int(row["model_calls"]) for row in rows)
        return {
            **summary_base,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trials": rows,
            "run_count": len(rows),
            "successes": successes,
            "silent_incorrect_successes": silent_incorrect,
            "over_halts": over_halts,
            "failures": len(rows) - successes,
            "failure_taxonomy_counts": failure_counts,
            "model_calls": model_calls,
            "cleanup": cleanup,
            "accepted": (
                len(rows) == 3
                and successes == 3
                and silent_incorrect == 0
                and over_halts == 0
                and model_calls == 0
                and cleanup["passed"] is True
            ),
        }

    active_error: BaseException | None = None
    try:
        # Snapshot before the first guest mutation, including resume. This id
        # is retained in memory and is the only snapshot cleanup may delete.
        snapshot_id = vm.snapshot(
            f"oaflow-rdp-qual-{int(time.time())}",
            description=f"real RDP qualification; candidate={candidate_commit}",
        )
        summary_base["owned_snapshot_id"] = snapshot_id
        print(
            f"[rdp-qual] owned_snapshot={snapshot_id} "
            f"base={base_snapshot_id} candidate={candidate_commit}"
        )
        vm.ensure_running()
        _wait_guest(vm)
        host = vm.guest_ip()
        computer_name = (vm.exec_cmd("hostname").stdout or "").strip()
        assert computer_name, "could not determine Windows computer name"
        guest_version = (vm.exec_cmd("ver").stdout or "").strip()
        environment.update(
            {
                "guest_ip": host,
                "computer_name": computer_name,
                "guest_version": guest_version,
            }
        )

        create = vm.exec_cmd(
            f'net user {account} "{password}" /add /expires:never '
            f'&& net localgroup "Remote Desktop Users" {account} /add',
            timeout=60,
        )
        assert create.returncode == 0, create.stderr or create.stdout
        mkdir = vm.exec_cmd(f'if not exist "{guest_dir}" mkdir "{guest_dir}"')
        assert mkdir.returncode == 0, mkdir.stderr or mkdir.stdout
        firewall = vm.exec_ps(
            "Enable-NetFirewallRule -Name "
            "RemoteDesktop-UserMode-In-TCP,RemoteDesktop-UserMode-In-UDP"
        )
        assert firewall.returncode == 0, firewall.stderr or firewall.stdout
        _wait_tcp(host, port)

        _write_summary(report_path, current_summary())
        print(f"[rdp-qual] durable_report={report_path}")

        for index in range(1, TRIALS + 1):
            token = f"oaflow-rdp-trial-{index}-{secrets.token_hex(8)}"
            guest_path = rf"{guest_dir}\trial-{index}.txt"
            started = time.monotonic()
            backend = None
            observed: str | None = None
            failure_class: str | None = None
            error_type: str | None = None
            input_returned_without_error = False
            phase = "connect_or_frame_failure"
            try:
                transport = AardwolfTransport.from_credentials(
                    host,
                    account,
                    password,
                    domain=computer_name,
                    port=port,
                    width=width,
                    height=height,
                )
                backend = FreeRDPBackend(
                    transport,
                    max_frame_age_s=5.0,
                    readiness_probe=_painted_readiness,
                )
                _wait_user_shell(vm, account)
                png = backend.wait_first_frame(retries=80, settle_s=0.25)
                _assert_painted_frame(png, backend.viewport)
                # The token reaches the guest only through RDP input. prlctl is
                # used exclusively afterward as an independent read oracle.
                phase = "input_delivery_failure"
                backend.press("Meta+r")
                time.sleep(0.5)
                backend.type_text(f'cmd.exe /d /c echo {token}>"{guest_path}"')
                backend.screenshot()  # refresh lease after potentially slow typing
                backend.press("Enter")
                input_returned_without_error = True
                phase = "over_halt_or_timeout"
                observed = _oracle_read(vm, guest_path)
                if observed is None:
                    failure_class = "over_halt_or_timeout"
                elif observed != token:
                    failure_class = "independent_oracle_mismatch"
            except Exception as exc:  # noqa: BLE001 - classify and continue trials
                failure_class = phase
                error_type = type(exc).__name__
            finally:
                if backend is not None:
                    try:
                        backend.close()
                    except Exception as exc:  # noqa: BLE001 - preserve trial row
                        if failure_class is None:
                            failure_class = "input_delivery_failure"
                            error_type = type(exc).__name__

            exact = observed == token and failure_class is None
            rows.append(
                {
                    "trial": index,
                    "expected": token,
                    "observed": observed,
                    "exact": exact,
                    "input_returned_without_error": input_returned_without_error,
                    "failure_class": failure_class,
                    "error_type": error_type,
                    "latency_s": round(time.monotonic() - started, 3),
                    "model_calls": 0,
                }
            )
            # Persist every row before starting the next trial, including
            # failures, so a later exception cannot erase the evidence.
            _write_summary(report_path, current_summary())

        summary = current_summary()
        print(f"[rdp-qual] {json.dumps(summary, sort_keys=True)}")
        assert len(rows) == 3
        assert summary["successes"] == len(rows)
        assert summary["silent_incorrect_successes"] == 0
        assert summary["over_halts"] == 0
        assert summary["model_calls"] == 0
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        # Restore the explicit preserved base. Only after proving it current
        # may cleanup delete the one exact snapshot id created above.
        cleanup["attempted"] = snapshot_id is not None
        cleanup_error: Exception | None = None
        try:
            if snapshot_id is not None:
                vm.restore_base_and_delete_owned_snapshot(
                    base_snapshot_id=base_snapshot_id,
                    owned_snapshot_id=snapshot_id,
                )
                cleanup["owned_snapshot_deleted"] = True
            restored = _wait_state(vm, initial_state)
            cleanup["restored_state"] = restored
            if restored != initial_state:
                raise ParallelsError(
                    f"preserved base did not restore state {initial_state!r}: {restored!r}"
                )
            snapshots_after = vm.list_snapshots()
            after_ids = [item.snapshot_id for item in snapshots_after]
            cleanup["snapshot_ids_after"] = after_ids
            cleanup["base_snapshot_current_after"] = any(
                item.snapshot_id == base_snapshot_id and item.current
                for item in snapshots_after
            )
            if not cleanup["base_snapshot_current_after"]:
                raise ParallelsError("preserved base is not current after cleanup")
            before_ids = [item.snapshot_id for item in snapshots_before]
            cleanup["snapshot_inventory_restored"] = sorted(after_ids) == sorted(
                before_ids
            )
            if not cleanup["snapshot_inventory_restored"]:
                raise ParallelsError("snapshot inventory differs after cleanup")
            environment["host_free_bytes_after"] = vm.require_host_free_space(
                storage_path=storage_path
            )
            cleanup["passed"] = True
        except Exception as exc:  # noqa: BLE001 - persist cleanup failure evidence
            cleanup_error = exc
            cleanup["error_type"] = type(exc).__name__
        try:
            _write_summary(report_path, current_summary())
        except Exception as exc:  # noqa: BLE001 - report persistence is required
            if cleanup_error is None:
                cleanup_error = exc
        print(
            f"[rdp-qual] cleanup={json.dumps(cleanup, sort_keys=True)} "
            f"durable_report={report_path}"
        )
        if cleanup_error is not None:
            if active_error is not None:
                active_error.add_note(
                    f"RDP qualification cleanup/report error: {cleanup_error!r}"
                )
            else:
                raise RuntimeError(
                    "RDP qualification environment cleanup/report failed"
                ) from cleanup_error

    assert current_summary()["accepted"] is True

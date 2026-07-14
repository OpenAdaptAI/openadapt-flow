"""Snapshot-safe, OPT-IN Parallels desktop end-to-end proof.

Proves the DESKTOP path reaches web parity on a REAL Windows app: record ->
compile -> replay through ``WindowsBackend`` against the built-in Windows
Calculator (a deterministic, PHI-free UWP app with a clean UIA tree), driven by
the hardened in-guest ``win_agent`` running in the interactive session (the
session-0 fix). It asserts the DETERMINISTIC ``structural`` (UIA) rung fires for
every click and the run completes.

Safety / isolation (the user's VM is sacred):
    * OPT-IN ONLY. Skipped unless ``OAFLOW_PARALLELS_E2E=1`` — it is collected
      but never runs on CI or any machine without the env var, so ``pytest
      --ignore=tests/e2e`` (macOS CI) and a plain run both stay green.
    * SNAPSHOT-FIRST, REVERT-AFTER. A fresh snapshot is taken before anything
      touches the guest and the VM is reverted to it in ``finally`` — the guest
      returns to its exact pre-test state.
    * NEVER deletes the VM or ANY snapshot (revert only), and never runs unless
      the maintainer explicitly opts in. The maintainer runs this as the live
      proof; CI never does.

Override the VM via ``OAFLOW_PARALLELS_VM_UUID`` (defaults to the documented
"Windows 11" VM).
"""

from __future__ import annotations

import os
import secrets
import time

import pytest

RUN_E2E = os.environ.get("OAFLOW_PARALLELS_E2E") == "1"

pytestmark = [
    pytest.mark.skipif(
        not RUN_E2E,
        reason="opt-in live Parallels e2e; set OAFLOW_PARALLELS_E2E=1 to run",
    ),
    pytest.mark.timeout(900),
]

# Modern Windows Calculator exposes stable AutomationIds per key. A short,
# deterministic sequence: clear, 7, +, 2, = -> display "9".
CALC_SEQUENCE = ("clearButton", "num7Button", "plusButton", "num2Button", "equalButton")


def _post_exec(url: str, token: str, command: str, *, timeout: float = 30.0):
    """POST bare Python to the in-guest agent (session 1), with bearer auth."""
    import requests

    return requests.post(
        f"{url}/execute_windows",
        json={"command": command},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )


def _button_points(backend, ids) -> dict:
    """Live UIA center point for each Calculator AutomationId (via the agent)."""
    from openadapt_flow.ir import StructuralLocator

    points = {}
    for aid in ids:
        handle = backend.locate_structural(StructuralLocator(automation_id=aid))
        assert handle is not None, f"UIA could not locate Calculator button {aid!r}"
        points[aid] = (int(handle.point[0]), int(handle.point[1]))
    return points


def test_desktop_record_compile_replay_structural(tmp_path) -> None:
    from openadapt_flow.adapters.desktop_recorder import (
        record_desktop_demo,
        structural_armed_coverage,
    )
    from openadapt_flow.backends import WindowsBackend
    from openadapt_flow.backends.parallels_vm import DEFAULT_VM_UUID, ParallelsVM
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    uuid = os.environ.get("OAFLOW_PARALLELS_VM_UUID", DEFAULT_VM_UUID)
    vm = ParallelsVM(uuid)
    token = secrets.token_hex(16)

    vm.ensure_running()
    # SNAPSHOT FIRST — everything below is reverted away in finally.
    snap_id = vm.snapshot(
        f"oaflow-e2e-{int(time.time())}", description="openadapt-flow desktop e2e"
    )
    try:
        url = vm.launch_agent(token=token)
        backend = WindowsBackend(server_url=url, auth_token=token)
        assert backend.probe(), "win_agent screenshot probe failed"

        # Launch Calculator IN SESSION 1 (via the agent), then let it settle.
        _post_exec(url, token, "import subprocess; subprocess.Popen(['calc.exe'])")
        time.sleep(6)

        points = _button_points(backend, CALC_SEQUENCE)

        # RECORD the demonstration live -> arms a UIA locator per click.
        def driver(rec) -> None:
            for aid in CALC_SEQUENCE:
                rec.click(*points[aid])
                time.sleep(0.4)

        recording = record_desktop_demo(backend, tmp_path / "recording", driver)

        bundle = compile_recording(recording, tmp_path / "bundle", name="calc-e2e")
        coverage = structural_armed_coverage(bundle)
        assert coverage["armed_coverage"] == 1.0, (
            "desktop bundle is not fully structurally armed (web parity gap): "
            f"{coverage}"
        )

        # REPLAY from a fresh cleared state; structural rung must drive it.
        _post_exec(url, token, "import subprocess; subprocess.Popen(['calc.exe'])")
        time.sleep(4)
        workflow = Workflow.load(bundle)
        report = Replayer(backend, use_structural=True).run(
            workflow,
            params={},
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )

        n_clicks = len(CALC_SEQUENCE)
        assert report.rung_counts.get("structural", 0) >= n_clicks, (
            "structural (UIA) rung did not fire for every click: "
            f"{report.rung_counts}"
        )
        assert report.success, [r.model_dump() for r in report.results]
        # No click was admitted on an identity MISMATCH.
        for result in report.results:
            if result.identity is not None:
                assert result.identity.status != "mismatch"
    finally:
        # REVERT to the pre-test snapshot (never delete it or the VM).
        try:
            vm.revert(snap_id)
        except Exception as e:  # noqa: BLE001 - surface but never mask the test result
            print(f"[e2e] WARNING: revert to {snap_id} failed: {e}")

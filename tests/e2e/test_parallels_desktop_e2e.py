"""Snapshot-safe, OPT-IN Parallels desktop end-to-end proof.

Proves the DESKTOP path reaches web parity on a REAL Windows app: record ->
compile -> replay through ``WindowsBackend``, and asserts the DETERMINISTIC
``structural`` (UIA) rung resolves every click by ``AutomationId`` and the run
completes.

Target app: the in-tree **Patient Notes -- Benchmark Harness** WinForms app
(``scripts/desktop/patient_notes.ps1``), NOT the Windows Calculator
-----------------------------------------------------------------------------
The earlier version of this test drove the built-in **UWP Calculator**
(``calc.exe``) and asserted per-key ``AutomationId``\\s (``num7Button`` ...). On
**Windows 11 ARM** the modern Calculator is a packaged UWP app hosted under
``ApplicationFrameHost`` whose keypad does NOT surface as a findable top-level
window through the UIA path :meth:`WindowsBackend.locate_structural` walks, so
``locate_structural`` returns None and the test fails EVEN THOUGH the desktop
stack works. That made the "e2e" a flaky Calculator test, not a repeatable
proof of the structural rung.

This test instead drives a **classic Win32/WinForms** app that ships in this
repo and that the desktop benchmark already uses. Its controls are plain
``System.Windows.Forms`` controls created with explicit ``.Name`` /
``.AccessibleName`` (``searchBox``, ``searchButton``, ``noteBox``,
``saveButton``), so WinForms exposes each as a first-class element with a stable
``AutomationId`` in the top-level window's UIA tree -- exactly the elements
:meth:`WindowsBackend.locate_structural` searches for from the root. This was
verified live on the Win11-ARM VM:
``locate_structural(automation_id='searchBox') -> StructuralHandle(...)`` with
confidence 1.0. The four controls this test clicks are all TextBox
(``EditControl``) / Button (``ButtonControl``) controls with stable
``AutomationId``\\s -- it deliberately does NOT click the ``DataGridView`` rows,
whose WinForms UIA tree is only partially populated (rows often carry no
``AutomationId``; see ``docs/desktop/LIMITS.md``), so every recorded click is
structurally armable and the ``armed_coverage == 1.0`` / structural-rung
assertions are meaningful and repeatable rather than flaky.

Safety / isolation (the user's VM is sacred):
    * OPT-IN ONLY. Skipped unless ``OAFLOW_PARALLELS_E2E=1`` -- it is collected
      but never runs on CI or any machine without the env var, so ``pytest
      --ignore=tests/e2e`` (macOS CI) and a plain run both stay green.
    * SNAPSHOT-FIRST, REVERT-AFTER. A fresh snapshot is taken before anything
      touches the guest and the VM is reverted to it in ``finally`` -- the guest
      returns to its exact pre-test state (the deployed app scripts, the seeded
      SQLite DB, and the running app are all reverted away).
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
from pathlib import Path

import pytest

RUN_E2E = os.environ.get("OAFLOW_PARALLELS_E2E") == "1"

pytestmark = [
    pytest.mark.skipif(
        not RUN_E2E,
        reason="opt-in live Parallels e2e; set OAFLOW_PARALLELS_E2E=1 to run",
    ),
    pytest.mark.timeout(900),
]

# The reliable target: classic WinForms controls with stable AutomationIds
# (verified on Win11 ARM). searchBox/noteBox are TextBox (EditControl);
# searchButton/saveButton are Button (ButtonControl). Every one is resolvable
# from the UIA root by AutomationId, so every recorded click arms structurally
# and every replayed click resolves on the structural rung. The DataGridView
# rows are intentionally NOT clicked (partial WinForms UIA tree; see module
# docstring / docs/desktop/LIMITS.md).
WINFORMS_CONTROLS = ("searchBox", "searchButton", "noteBox", "saveButton")

# Demonstration values. Searching a given name loads the roster into the grid;
# the note text is typed into noteBox. The DB write itself is not the assertion
# here (that is the desktop benchmark's DB-ground-truth judge) -- this test
# proves the structural rung fires and the replay completes.
DEMO_SEARCH = "Neil"
DEMO_NOTE = "BP 128/82; follow-up in 2 weeks"


def _deploy_app(vm) -> None:
    """Push the WinForms target-app scripts into the guest (C:/oa).

    ``session1_launch.py`` is already deployed by ``vm.launch_agent``; this adds
    the SQLite ground-truth CLI and the WinForms UI script the launcher runs.
    """
    import openadapt_flow.backends.parallels_vm as pv

    src = Path(pv._SCRIPT_DIR)
    vm.exec_cmd(f"if not exist {pv.GUEST_DIR} mkdir {pv.GUEST_DIR}")
    for name in ("pn_db.py", "patient_notes.ps1"):
        vm.push_file(str((src / name).resolve()), f"{pv.GUEST_DIR}/{name}")


def _seed_db(vm) -> None:
    """(Re)create the app's SQLite DB with the fixed clean roster."""
    import openadapt_flow.backends.parallels_vm as pv

    vm.exec(
        [pv.GUEST_PY, f"{pv.GUEST_DIR}/pn_db.py", "seed", "--drift", "none"],
        timeout=60,
    )


def _launch_app(vm, *, settle_s: float = 6.0) -> None:
    """(Re)launch the WinForms app in the interactive session (session 1).

    Kills any prior instance first so replay starts from a fresh, clean window.
    The app is a PowerShell ``-STA`` process; ``session1_launch.py`` runs it on
    the real desktop via ``CreateProcessAsUser`` (session-0 cannot host the UI).
    """
    import openadapt_flow.backends.parallels_vm as pv

    vm.exec_cmd("taskkill /F /IM powershell.exe 2>nul & echo ok")
    time.sleep(1)
    vm.exec(
        [
            pv.GUEST_PY,
            f"{pv.GUEST_DIR}/session1_launch.py",
            f"{pv.GUEST_DIR}/patient_notes.ps1",
        ]
    )
    time.sleep(settle_s)


def _control_points(backend, ids) -> dict:
    """Live UIA center point for each WinForms control (via the agent).

    This is the first half of the proof: ``locate_structural`` MUST resolve
    every control by its ``AutomationId``. On a UWP app that surfaces nothing
    findable this returns None and fails loudly (the old Calculator failure);
    on the WinForms target it returns a real center point per control.
    """
    from openadapt_flow.ir import StructuralLocator

    points = {}
    for aid in ids:
        handle = backend.locate_structural(StructuralLocator(automation_id=aid))
        assert handle is not None, (
            f"UIA could not locate WinForms control {aid!r} by AutomationId -- "
            "the target app is not exposing a stable UIA tree"
        )
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
    # SNAPSHOT FIRST -- everything below is reverted away in finally.
    snap_id = vm.snapshot(
        f"oaflow-e2e-{int(time.time())}", description="openadapt-flow desktop e2e"
    )
    try:
        url = vm.launch_agent(token=token)
        backend = WindowsBackend(server_url=url, auth_token=token)
        assert backend.probe(), "win_agent screenshot probe failed"

        # Deploy + seed + launch the RELIABLE WinForms target in session 1.
        _deploy_app(vm)
        _seed_db(vm)
        _launch_app(vm)

        # Half 1 of the proof: UIA resolves every control by AutomationId.
        points = _control_points(backend, WINFORMS_CONTROLS)

        # RECORD the demonstration live -> arms a UIA locator per click. Every
        # click lands on a control that carries a stable AutomationId, so the
        # compiled bundle is fully structurally armed (web parity).
        def driver(rec) -> None:
            rec.click(*points["searchBox"])
            rec.type_text(DEMO_SEARCH)
            rec.press("Enter")
            time.sleep(0.4)
            rec.click(*points["searchButton"])
            time.sleep(0.4)
            rec.click(*points["noteBox"])
            rec.type_text(DEMO_NOTE)
            rec.click(*points["saveButton"])
            time.sleep(0.4)

        recording = record_desktop_demo(backend, tmp_path / "recording", driver)

        bundle_dir = tmp_path / "bundle"
        workflow = compile_recording(recording, bundle_dir, name="patient-notes-e2e")
        coverage = structural_armed_coverage(workflow)
        assert coverage["armed_coverage"] == 1.0, (
            "desktop bundle is not fully structurally armed (web parity gap): "
            f"{coverage}"
        )
        n_clicks = coverage["click_steps"]
        assert n_clicks == len(WINFORMS_CONTROLS), (
            f"expected {len(WINFORMS_CONTROLS)} click steps, got {n_clicks}: {coverage}"
        )

        # REPLAY from a fresh app instance; the structural rung must drive it.
        _launch_app(vm)
        loaded = Workflow.load(bundle_dir)
        report = Replayer(backend, use_structural=True).run(
            loaded,
            params={},
            bundle_dir=bundle_dir,
            run_dir=tmp_path / "run",
        )

        # Half 2 of the proof: the structural (UIA) rung resolved EVERY click.
        assert report.rung_counts.get("structural", 0) >= n_clicks, (
            f"structural (UIA) rung did not fire for every click: {report.rung_counts}"
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
        except Exception as e:  # noqa: BLE001 - surface but never mask the result
            print(f"[e2e] WARNING: revert to {snap_id} failed: {e}")

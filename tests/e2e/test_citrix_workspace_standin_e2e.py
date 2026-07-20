"""Opt-in Citrix-Workspace-backend e2e over the no-DOM canvas STAND-IN.

Gated by ``OAFLOW_CITRIX_STANDIN_E2E=1`` (needs Docker + the flow stack with
cv2/rapidocr + a Playwright chromium). Builds the Part-1 canvas fixture
(``benchmark/canvas_ladder/fixture``), then drives the REAL
``CitrixWorkspaceBackend`` (window-scoped capture + OS input + all fail-loud
safety gates) over that no-DOM ``<canvas>`` through the ``WindowClient`` seam,
and asserts the contract:

  * healthy record->compile->replay succeeds with ZERO model calls, VISUAL rungs
    only (structural never used), the write EFFECT independently confirmed;
  * severe (illegible) drift HALTS with no write and no model call.

This is the no-DOM-canvas STAND-IN (Citrix Workspace-WEB class), NOT ICA/HDX.
See ``benchmark/citrix_workspace/README.md``.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OAFLOW_CITRIX_STANDIN_E2E") != "1",
    reason="set OAFLOW_CITRIX_STANDIN_E2E=1 to run the Citrix-backend stand-in e2e",
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO / "benchmark" / "canvas_ladder" / "fixture"
HARNESS = REPO / "benchmark" / "citrix_workspace" / "run_citrix_workspace_qualification.py"
IMAGE = "oaflow-canvas-fixture:test"
CONTAINER = "oaflow-citrix-standin-test"
PORT = 6080


def _docker(*args: str, check: bool = True, timeout: int = 900):
    return subprocess.run(["docker", *args], check=check, timeout=timeout,
                          capture_output=True, text=True)


@pytest.fixture(scope="module")
def canvas_fixture():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker not available")
    _docker("build", "-t", IMAGE, str(FIXTURE_DIR))
    _docker("rm", "-f", CONTAINER, check=False)
    _docker("run", "-d", "--name", CONTAINER, "--shm-size=1g",
            "-p", f"{PORT}:6080", IMAGE)
    time.sleep(18)
    try:
        yield CONTAINER
    finally:
        _docker("rm", "-f", CONTAINER, check=False)


def test_citrix_backend_contract_over_canvas_standin(canvas_fixture, tmp_path):
    spec = importlib.util.spec_from_file_location("citrix_ws_harness", HARNESS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["citrix_ws_harness"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    evidence = mod.run_qualification(
        canvas_fixture, out_dir=tmp_path, base_url="http://localhost", port=PORT)

    healthy, severe = evidence["trials"]
    assert healthy["success"], healthy
    assert healthy["model_calls"] == 0, healthy
    assert healthy["structural_rung_used"] == 0, healthy
    assert healthy["visual_rungs_used"], healthy
    assert healthy["effect_confirmed"], healthy
    assert severe["halted"], severe
    assert not severe["silent_write"], severe
    assert severe["model_calls"] == 0, severe
    assert evidence["accepted"], evidence

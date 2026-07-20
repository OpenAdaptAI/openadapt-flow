"""Opt-in real-RDP vision-ladder e2e (Docker FreeRDP round-trip).

Gated by ``OAFLOW_DOCKER_RDP_E2E=1`` (needs Docker + the flow stack with
cv2/rapidocr on PATH). It builds the ``benchmark/rdp_ladder/fixture`` image,
starts the RDP round-trip, runs the qualification harness, and asserts the
validation contract on a real RDP pixel surface:

  * healthy record->compile->replay succeeds with ZERO model calls,
  * resolution used the VISUAL rungs and NEVER the structural rung,
  * the write EFFECT is independently confirmed,
  * the ladder HALTS (no silent write, no model call) under injected drift.

This is the CI-viable Linux analog of the RDP transport proof in
``benchmark/rdp`` (which needs a real Windows target for the aardwolf transport).
See ``benchmark/rdp_ladder/README.md`` for why FreeRDP (not aardwolf) is used.
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
    os.environ.get("OAFLOW_DOCKER_RDP_E2E") != "1",
    reason="set OAFLOW_DOCKER_RDP_E2E=1 to run the real Docker-RDP e2e",
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO / "benchmark" / "rdp_ladder" / "fixture"
HARNESS = REPO / "benchmark" / "rdp_ladder" / "run_rdp_ladder_qualification.py"
IMAGE = "oaflow-rdp-fixture:test"
CONTAINER = "oaflow-rdp-ladder-test"


def _docker(*args: str, check: bool = True, timeout: int = 900):
    return subprocess.run(["docker", *args], check=check, timeout=timeout,
                          capture_output=True, text=True)


@pytest.fixture(scope="module")
def rdp_fixture():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker not available")
    _docker("build", "-t", IMAGE, str(FIXTURE_DIR))
    _docker("rm", "-f", CONTAINER, check=False)
    _docker("run", "-d", "--name", CONTAINER, "--shm-size=1g", IMAGE)
    time.sleep(22)  # kiosk + shadow server + client come up
    try:
        yield CONTAINER
    finally:
        _docker("rm", "-f", CONTAINER, check=False)


def test_real_rdp_vision_ladder_contract(rdp_fixture, tmp_path):
    spec = importlib.util.spec_from_file_location("rdp_ladder_harness", HARNESS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rdp_ladder_harness"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    evidence = mod.run_qualification(rdp_fixture, out_dir=tmp_path)

    healthy = evidence["trials"][0]
    drift = evidence["trials"][1]
    assert healthy["success"], healthy
    assert healthy["model_calls"] == 0, healthy
    assert healthy["structural_rung_used"] == 0, healthy
    assert healthy["visual_rungs_used"], healthy
    assert healthy["effect_confirmed"], healthy
    assert drift["halted"], drift
    assert not drift["silent_write"], drift
    assert drift["model_calls"] == 0, drift
    assert evidence["accepted"], evidence

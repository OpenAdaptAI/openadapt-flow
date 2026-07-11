"""E2E: the grounding rung composed with the identity gate on live MockMed.

Two end-to-end pins, against the real browser + real replayer + real identity
gate (the corpus-wide numbers live in ``tests/test_grounding_rung.py``):

- a HEALTHY replay never consults the grounder (the hot path stays model-free;
  the rung is a last resort);
- injecting a grounder does NOT let a data-drift wrong-entity case through —
  the pre-click identity gate still safe-halts, so false-accept stays 0 with a
  grounder in the loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.validation.grounding_composition import FaithfulMockGrounder

from .conftest import PARAMS, drift_url
from .validation_utils import describe, failing_step, replay_on_page

pytestmark = pytest.mark.timeout(600)


class _SpyGrounder(FaithfulMockGrounder):
    """A grounder that records every consultation (to prove non-use)."""


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


def test_healthy_replay_never_consults_grounder(
    bundle, mockmed_url, _browser, run_dir
) -> None:
    """A clean replay resolves every step deterministically; the injected
    grounder is never called and the run makes zero model calls."""
    grounder = _SpyGrounder()
    report, state = replay_on_page(
        _browser, bundle.dir, mockmed_url, run_dir,
        params=dict(PARAMS), grounder=grounder,
    )
    assert report.success, describe(report, state)
    assert grounder.calls == [], "healthy replay must not consult the grounder"
    assert report.model_calls == 0
    assert state["hash"] == "#patient/p1"


def test_grounder_in_loop_still_safe_halts_on_wrong_entity(
    bundle, mockmed_url, _browser, run_dir
) -> None:
    """With a grounder injected, the look-alike data-drift case (a different
    referral at the recorded position) must STILL safe-halt at the identity
    gate — nothing is saved, no wrong patient is written. The grounder cannot
    buy a wrong target a click, because identity disposes after resolution."""
    grounder = _SpyGrounder()
    report, state = replay_on_page(
        _browser, bundle.dir, drift_url(mockmed_url, "lookalike"), run_dir,
        params=dict(PARAMS), grounder=grounder,
    )
    assert report.success is False, describe(report, state)
    assert state["hash"] == "#tasks", describe(report, state)  # no navigation
    assert state["banner"] is None  # nothing saved to any patient
    failed = failing_step(report)
    assert failed is not None and failed.step_id == "step_005"
    assert "Identity check failed" in (failed.error or "")
    assert failed.identity is not None and failed.identity.status == "mismatch"

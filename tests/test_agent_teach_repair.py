"""A calling agent's demonstration becomes a governed repair candidate.

End to end on the modal-once fixture: a run halts, the agent's one-click
recording is taught through :func:`teach_from_calling_agent`, the candidate
waits for a person, the agent's own review and approval are refused, a person
approves, staging and canary activate it, and the activated bundle replays
without halting.

The vision-dependent campaign verdicts are recorded as fixed results here;
``test_repair_lifecycle`` covers the campaign batteries themselves. One test
runs the production campaign over the taught step to show it finds the step
and refuses a binding with no identity evidence.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from openadapt_flow.repair import Actor, CanaryRunObservation, RepairStore
from openadapt_flow.repair.agent_teach import (
    AgentTeachRefused,
    teach_from_calling_agent,
)
from openadapt_flow.repair.candidate import (
    CampaignResult,
    RepairProvenance,
    ScreenScrubEvidence,
)
from openadapt_flow.repair.cli import _run_campaign
from openadapt_flow.repair.lifecycle import ModelActuationError, RepairLifecycleError
from tests.test_teach_cli import (
    _assert_resolved,
    _base_bundle,
    _dismiss_recording,
    _halt_run,
)

AGENT_ID = "mcp-client:test-agent"
PERSON = Actor(kind="human", id="reviewer@example.org")
AUTOMATION = Actor(kind="automation", id="ci")
TOKEN_SHA = hashlib.sha256(b"repair-token").hexdigest()


def _provenance(**overrides) -> RepairProvenance:
    values = dict(
        agent_id=AGENT_ID,
        halted_run_id="run-0001",
        halted_step_id="s_verify",
        repair_token_sha256=TOKEN_SHA,
        screen_access_enabled_by="admin@example.org",
        screens_shared=1,
        screens_scrubbed=True,
        scrub_evidence=ScreenScrubEvidence(
            provider="presidio", package_version="1.1.2", policy_sha256="ab" * 32
        ),
    )
    values.update(overrides)
    return RepairProvenance(**values)


@pytest.fixture()
def taught(tmp_path: Path):
    bundle = _base_bundle(tmp_path)
    run_dir = tmp_path / "run"
    _halt_run(bundle, run_dir)
    recording = _dismiss_recording(tmp_path / "agent_recording")
    result = teach_from_calling_agent(
        run_dir,
        recording,
        tmp_path / "proposed",
        bundle=bundle,
        provenance=_provenance(),
    )
    return bundle, run_dir, result


def _passed(kind: str) -> CampaignResult:
    return CampaignResult(campaign=kind, passed=True, total=5, failed=0)


def test_halt_to_teach_to_approved_replay(taught, tmp_path: Path) -> None:
    _, _, result = taught
    candidate = result.candidate
    assert candidate.state == "candidate"
    assert candidate.source == "teach"
    assert candidate.provenance is not None
    assert candidate.provenance.repaired_by == "calling_agent"
    assert candidate.provenance.screens_scrubbed is True
    assert [step.step_id for step in candidate.taught_steps] == ["s_click_'dismiss'"]
    assert (
        result.evidence_dir / "heals" / "s_click_'dismiss'" / "screen.png"
    ).is_file()

    store = RepairStore(tmp_path / "store")
    candidate = store.add_candidate(candidate)
    # Until a person approves, nothing is active: the run stays halted.
    assert store.active_pointer() is None

    agent_as_person = Actor(kind="human", id=AGENT_ID)
    with pytest.raises(RepairLifecycleError, match="may not perform repair review"):
        store.review(candidate.candidate_id, agent_as_person)
    with pytest.raises(ModelActuationError):
        store.review(candidate.candidate_id, Actor(kind="model", id=AGENT_ID))

    store.review(candidate.candidate_id, PERSON)
    store.record_campaign(candidate.candidate_id, _passed("replay"), AUTOMATION)
    store.record_campaign(candidate.candidate_id, _passed("fault"), AUTOMATION)

    with pytest.raises(RepairLifecycleError, match="may not perform repair approval"):
        store.approve(candidate.candidate_id, agent_as_person, non_interactive=True)
    with pytest.raises(RepairLifecycleError):
        store.approve(candidate.candidate_id, AUTOMATION)

    candidate = store.approve(candidate.candidate_id, PERSON, non_interactive=True)
    assert candidate.approval.approved_by == PERSON.id
    store.stage(candidate.candidate_id, AUTOMATION)
    store.start_canary(candidate.candidate_id, AUTOMATION, max_runs=1)
    pointer = store.active_pointer()
    active_bundle = store.bundle_path(pointer.active_digest)
    _assert_resolved(active_bundle, tmp_path / "canary-run")
    candidate = store.record_canary_run(
        candidate.candidate_id,
        CanaryRunObservation(run_id="canary-1", verified=True, silent_incorrect=False),
    )
    assert candidate.state == "active"
    assert "taught by calling agent" in candidate.diff_summary()


def test_production_campaign_covers_taught_step_and_fails_closed(taught) -> None:
    _, _, result = taught
    fault = _run_campaign(result.candidate, "fault")
    assert fault.total > 0
    assert all(case.label.startswith("s_click_'dismiss'") for case in fault.cases)
    # The fixture's dismiss button has no identity evidence: refused.
    assert fault.passed is False


def test_refused_teach_leaves_nothing(tmp_path: Path) -> None:
    bundle = _base_bundle(tmp_path)
    run_dir = tmp_path / "run"
    _halt_run(bundle, run_dir)
    empty = tmp_path / "empty_recording"
    (empty / "frames").mkdir(parents=True)
    (empty / "events.jsonl").write_text("")
    (empty / "meta.json").write_text(
        '{"id": "rec-empty", "created_at": "2026-07-14T00:00:00+00:00",'
        ' "viewport": [1280, 800], "app_url": "http://localhost:0/", "params": {}}'
    )
    out = tmp_path / "proposed"
    with pytest.raises(Exception):
        teach_from_calling_agent(
            run_dir, empty, out, bundle=bundle, provenance=_provenance()
        )
    assert not out.exists()
    assert not (tmp_path / "proposed.evidence").exists()


def test_existing_output_is_refused(taught, tmp_path: Path) -> None:
    bundle, run_dir, result = taught
    with pytest.raises(AgentTeachRefused):
        teach_from_calling_agent(
            run_dir,
            tmp_path / "agent_recording",
            result.proposed_bundle,
            bundle=bundle,
            provenance=_provenance(),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        # Unscrubbed screens outside allow_unscrubbed.
        dict(screens_scrubbed=False, scrub_evidence=None),
        dict(
            screen_share_mode="ask_each_time",
            screens_scrubbed=False,
            scrub_evidence=None,
            screen_share_approved_by="reviewer@example.org",
        ),
        # Scrubbed without evidence of what scrubbed them.
        dict(scrub_evidence=None),
        # ask_each_time without a named approver, or approved by the agent.
        dict(screen_share_mode="ask_each_time"),
        dict(screen_share_mode="ask_each_time", screen_share_approved_by=AGENT_ID),
    ],
)
def test_inconsistent_screen_provenance_is_rejected(overrides) -> None:
    with pytest.raises(ValueError):
        _provenance(**overrides)


def test_allowed_screen_provenance_modes() -> None:
    assert _provenance().screen_share_mode == "scrubbed_only"
    asked = _provenance(
        screen_share_mode="ask_each_time",
        screen_share_approved_by="reviewer@example.org",
    )
    assert asked.screen_share_approved_by == "reviewer@example.org"
    raw = _provenance(
        screen_share_mode="allow_unscrubbed",
        screens_scrubbed=False,
        scrub_evidence=None,
    )
    assert raw.screens_scrubbed is False
    assert _provenance(screens_shared=0, scrub_evidence=None).screens_shared == 0

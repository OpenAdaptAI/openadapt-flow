"""Per-effect candidate verifier selection remains fail-closed."""

from __future__ import annotations

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime import Replayer
from openadapt_flow.runtime.effects.adapter import (
    CandidateEffectVerifier,
    RedactingVerifier,
    RedactionPolicy,
    register_verifier_factory,
)
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    ReadbackNav,
    ReadbackSpec,
    Verdict,
)
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier
from openadapt_flow.verification import VerificationTier


class _Verifier:
    def __init__(self, tier, *, reachable=True, name="test"):
        self.verification_tier = tier
        self.substrate = name
        self.reachable = reachable
        self.captures = 0
        self.verifies = 0

    def capture_pre_state(self):
        self.captures += 1
        return EffectState(substrate=self.substrate, reachable=self.reachable)

    def verify(self, effect, before, context=None):
        self.verifies += 1
        return EffectVerdict(
            verdict=Verdict.CONFIRMED if before.reachable else Verdict.INDETERMINATE,
            kind=effect.kind,
            substrate=self.substrate,
            matched_records=[{"patient": "private"}],
            unavailable=not before.reachable,
        )


def _effect(*, different_path=False):
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        field="note",
        value="saved",
        readback=ReadbackSpec(
            region=(0, 0, 10, 10),
            different_path=different_path,
            renavigation=(
                [
                    ReadbackNav(action="click", point=(1, 1)),
                    ReadbackNav(action="type", text="record"),
                    ReadbackNav(action="key", key="Enter"),
                ]
                if different_path
                else []
            ),
        ),
    )


def test_selection_refines_onscreen_tier_per_resolved_effect():
    onscreen = OnScreenReadbackVerifier(backend=None)
    session = _Verifier(VerificationTier.PERSISTED_STATE_REACQUISITION, name="session")
    selector = CandidateEffectVerifier([onscreen, session])
    persisted = _effect(different_path=True)
    same_surface = _effect(different_path=False)

    state = selector.capture_pre_state_for_effects([persisted, same_surface])

    assert state.for_effect(persisted).verifier is onscreen
    assert state.for_effect(same_surface).verifier is session
    assert (
        selector.verification_tier_for(persisted)
        == VerificationTier.PERSISTED_STATE_REACQUISITION
    )
    assert (
        selector.verification_tier_for(same_surface)
        == VerificationTier.PERSISTED_STATE_REACQUISITION
    )


def test_selected_candidate_pre_state_is_captured_before_verification():
    strong = _Verifier(VerificationTier.INDEPENDENT_SYSTEM, name="export")
    weak = _Verifier(VerificationTier.IMMEDIATE_SCREEN, name="screen")
    effect = _effect()
    selector = CandidateEffectVerifier([weak, strong])

    state = selector.capture_pre_state_for_effects([effect])

    assert state.reachable
    assert strong.captures == 1
    assert weak.captures == 0
    assert state.for_effect(effect).state.substrate == "export"


def test_unavailable_selected_candidate_never_downgrades_after_actuation():
    strong = _Verifier(
        VerificationTier.INDEPENDENT_SYSTEM, reachable=False, name="export"
    )
    weak = _Verifier(VerificationTier.IMMEDIATE_SCREEN, name="screen")
    effect = _effect()
    selector = CandidateEffectVerifier([strong, weak])
    state = selector.capture_pre_state_for_effects([effect])

    verdict = selector.verify(effect, state)

    assert not state.reachable
    assert verdict.verdict is Verdict.INDETERMINATE
    assert strong.verifies == 1
    assert weak.verifies == 0


def test_different_path_onscreen_candidate_does_not_require_prestate_readability():
    """Post-action reacquisition can prove a GUI-only write without a delta."""
    effect = _effect(different_path=True)
    verifier = CandidateEffectVerifier([OnScreenReadbackVerifier(backend=None)])
    before = verifier.capture_pre_state_for_effects([effect])

    assert before.for_effect(effect).state.reachable is False
    assert (
        Replayer._required_effect_pre_state_unreadable(verifier, before, [effect])
        is False
    )


def test_bool_plugin_tier_is_rejected():
    class _CompletePlugin(_Verifier):
        def test_connection(self, context=None):
            raise AssertionError("construction must reject the invalid tier first")

        def capture_post_state(self, context=None):
            return self.capture_pre_state(context)

    register_verifier_factory(
        "candidate-bool-tier-test",
        lambda cfg, params: _CompletePlugin(True),
        replace=True,
    )
    with pytest.raises(
        ValueError, match="verification_tier must be a VerificationTier"
    ):
        build_effect_verifier(
            EffectsConfig(candidates=[EffectsConfig(kind="candidate-bool-tier-test")])
        )


def test_tier_only_plugin_is_rejected_during_candidate_construction():
    class _TierOnly:
        verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    register_verifier_factory(
        "candidate-tier-only-test", lambda cfg, params: _TierOnly(), replace=True
    )
    with pytest.raises(
        ValueError,
        match="missing test_connection, capture_pre_state, capture_post_state, verify",
    ):
        build_effect_verifier(
            EffectsConfig(candidates=[EffectsConfig(kind="candidate-tier-only-test")])
        )


def test_connection_aggregates_all_candidates_without_selection_or_raising():
    readable = _Verifier(VerificationTier.INDEPENDENT_SYSTEM, name="export")
    unavailable = _Verifier(
        VerificationTier.IMMEDIATE_SCREEN, reachable=False, name="screen"
    )
    verifier = CandidateEffectVerifier([readable, unavailable])

    probe = verifier.test_connection()

    assert probe.ok is False
    assert probe.substrate == "candidates"
    assert probe.detail["candidates"] == [
        {"substrate": "export", "ok": True, "reason": "reachable"},
        {"substrate": "screen", "ok": False, "reason": "unreachable"},
    ]
    assert readable.captures == 1
    assert unavailable.captures == 1


def test_redacting_wrapper_delegates_candidate_connection_probe():
    verifier = RedactingVerifier(
        CandidateEffectVerifier([_Verifier(VerificationTier.INDEPENDENT_SYSTEM)]),
        RedactionPolicy(),
    )

    probe = verifier.test_connection()

    assert probe.ok is True
    assert probe.substrate == "candidates"


def test_redacting_wrapper_connection_probe_never_raises():
    class _ExplodingConnection(_Verifier):
        def test_connection(self):
            raise RuntimeError("no connection")

    verifier = RedactingVerifier(
        CandidateEffectVerifier(
            [_ExplodingConnection(VerificationTier.INDEPENDENT_SYSTEM)]
        ),
        RedactionPolicy(),
    )

    probe = verifier.test_connection()

    assert probe.ok is False
    assert (
        probe.detail["candidates"][0]["reason"]
        == "connection probe raised: RuntimeError"
    )


def test_plugin_with_incomplete_lifecycle_fails_at_construction():
    class _IncompletePlugin:
        substrate = "incomplete"
        verification_tier = VerificationTier.INDEPENDENT_SYSTEM

        def capture_pre_state(self):
            return EffectState(substrate=self.substrate, reachable=True)

        def verify(self, effect, before, context=None):
            return EffectVerdict(verdict=Verdict.INDETERMINATE, kind=effect.kind)

    register_verifier_factory(
        "candidate-incomplete-plugin-test",
        lambda cfg, params: _IncompletePlugin(),
        replace=True,
    )

    with pytest.raises(ValueError, match="test_connection, capture_post_state"):
        build_effect_verifier(EffectsConfig(kind="candidate-incomplete-plugin-test"))


def test_redacting_wrapper_keeps_selected_candidate_and_redacts_evidence():
    strong = _Verifier(VerificationTier.INDEPENDENT_SYSTEM, name="export")
    effect = _effect()
    verifier = RedactingVerifier(
        CandidateEffectVerifier([strong]), RedactionPolicy(redact_fields=["patient"])
    )

    state = verifier.capture_pre_state_for_effects([effect])
    verdict = verifier.verify(effect, state)

    assert state.for_effect(effect).verifier is strong
    assert verdict.matched_records == [{"patient": "[redacted]"}]

"""Offline unit tests for the on-screen OCR read-back effect verifier.

Deterministic: a fake backend + fake ``vision.ocr`` inject the OCR lines, so
the three-valued verdict logic is asserted without a live screen. The verifier
is the ONLY no-API effect substrate (Citrix exposes no readable system of
record locally); these tests pin its fail-safe posture — CONFIRMED only on a
clean read-back, HALT (INDETERMINATE) when the region is unreadable, HALT
(REFUTED) when the region shows a readable but WRONG value.
"""

from __future__ import annotations

from types import SimpleNamespace

from openadapt_flow.runtime.effects import EffectVerifier
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, Verdict
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier


class _Line:
    def __init__(self, text: str, conf: float = 0.95) -> None:
        self.text = text
        self.region = (0, 0, 10, 10)
        self.confidence = conf


class _Backend:
    def __init__(self) -> None:
        self.shots = 0

    def screenshot(self) -> bytes:
        self.shots += 1
        return b"\x89PNG\r\n\x1a\n" + b"fake"


def _vision(lines: list[_Line]):
    def ocr(png: bytes, *, region=None):
        return list(lines)

    return SimpleNamespace(ocr=ocr)


def _verifier(lines: list[_Line], **kw) -> OnScreenReadbackVerifier:
    return OnScreenReadbackVerifier(
        _Backend(), region=(0, 0, 100, 40), vision=_vision(lines), **kw
    )


def test_implements_effectverifier_protocol() -> None:
    v = _verifier([_Line("x")])
    assert isinstance(v, EffectVerifier)
    assert v.substrate == "onscreen"


def test_confirmed_when_value_reads_back() -> None:
    v = _verifier([_Line("Saved note for patient 3")])
    verdict = v.read_back("Saved note for patient 3")
    assert verdict.verdict is Verdict.CONFIRMED
    assert not verdict.should_halt


def test_confirmed_when_value_embedded_in_label() -> None:
    v = _verifier([_Line("Status: encounter documented and saved")])
    verdict = v.read_back("encounter documented and saved")
    assert verdict.verdict is Verdict.CONFIRMED


def test_refuted_when_region_shows_different_value() -> None:
    v = _verifier([_Line("No patient selected")])
    verdict = v.read_back("Saved note for patient 3")
    assert verdict.verdict is Verdict.REFUTED
    assert verdict.should_halt


def test_indeterminate_when_region_unreadable_no_lines() -> None:
    v = _verifier([])
    verdict = v.read_back("anything")
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.should_halt


def test_indeterminate_when_confidence_below_readable() -> None:
    v = _verifier([_Line("blurry", conf=0.05)])
    verdict = v.read_back("Saved note")
    assert verdict.verdict is Verdict.INDETERMINATE


def test_reason_marks_same_surface() -> None:
    v = _verifier([_Line("Saved note")])
    verdict = v.read_back("Saved note")
    assert "SAME-SURFACE" in verdict.reason


def test_capture_pre_state_baseline() -> None:
    v = _verifier([_Line("Ready")])
    state = v.capture_pre_state()
    assert state.substrate == "onscreen"
    assert state.detail.get("same_surface") is True
    assert state.records and state.records[0]["text"] == "ready"


def test_verify_reads_expected_from_effect_value() -> None:
    v = _verifier([_Line("chest pain, follow up in 2 weeks")])
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        field="note",
        value="chest pain, follow up in 2 weeks",  # coerced to ValueExpr(literal=)
    )
    before = v.capture_pre_state()
    verdict = v.verify(effect, before)
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.kind is EffectKind.FIELD_EQUALS


def test_verify_indeterminate_when_no_expected_derivable() -> None:
    v = _verifier([_Line("something")])
    effect = Effect(kind=EffectKind.RECORD_WRITTEN)  # no value, no match
    verdict = v.verify(effect, v.capture_pre_state())
    assert verdict.verdict is Verdict.INDETERMINATE


def test_verify_resolves_param_from_context() -> None:
    v = _verifier([_Line("note text ABC123")])
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        field="note",
        value={"param": "note_text"},
    )
    verdict = v.verify(effect, v.capture_pre_state(), context={"note_text": "ABC123"})
    assert verdict.verdict is Verdict.CONFIRMED


def test_multichar_difference_refuted() -> None:
    """A materially-different readable region HALTs (REFUTED)."""
    v = _verifier([_Line("No note saved for this patient")], min_ratio=0.9)
    verdict = v.read_back("chest pain, follow up in two weeks")
    assert verdict.should_halt


def test_single_glyph_difference_not_discriminated_same_surface_limit() -> None:
    """DOCUMENTED HONEST LIMIT: fuzzy same-surface read-back cannot tell a
    single trailing-glyph difference apart (patient "3" vs "8") — the same
    glyph-collapse that defeats OCR identity. This is precisely why the pixel
    -only safety guarantee rests on the IDENTITY GATE (right record) and
    halt-on-ambiguity, NOT on this read-back. Asserting the real behavior keeps
    the proof honest rather than claiming a discrimination it does not have."""
    v = _verifier([_Line("Saved note for patient 8")], min_ratio=0.9)
    verdict = v.read_back("Saved note for patient 3")
    # It CONFIRMS the near-identical string — a coarse consistency signal only.
    assert verdict.verdict is Verdict.CONFIRMED

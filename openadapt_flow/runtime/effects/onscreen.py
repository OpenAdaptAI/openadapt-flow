"""On-screen OCR read-back verifier — the ONLY no-API effect substrate.

Every other :class:`~openadapt_flow.runtime.effects.effect.EffectVerifier` in
this package (``RestRecordVerifier``, ``FhirEffectVerifier``,
``DocumentHashVerifier``) certifies a write by reading an **independent system of
record** (a JSON API, a FHIR endpoint, a filesystem document). A **Citrix-only /
Accuro-over-Citrix** substrate exposes none of those to the local box: there is no
DB, no reachable FHIR, no local file the write lands in — only the **pixels of the
remote-display client window**. This verifier is the honest fallback for that
case: after a write, it re-OCRs the saved-state region and compares it to the
expected value.

HONESTY — this is SAME-SURFACE verification, not independent confirmation.
It reads the *same screen* the action drove, so it CANNOT see the transactional
faults an independent SoR read catches (``docs/LIMITS.md`` "5 of 7 write faults
silent": partial save, phantom optimistic-UI success, duplicate submission, lost
update, double-delivered click). A rendered "Saved" that the record never
received still reads as "Saved". Therefore the pixel-only safety guarantee does
**not** rest on this read-back — it rests on the **identity gate** (right record)
and **halt-on-ambiguity** (never guess a target). This read-back is a
best-effort *presence/consistency* signal on top of those, and it is fail-safe:
it HALTs (INDETERMINATE) when the region is unreadable and HALTs (REFUTED) when
the region shows a readable value that is NOT the expected one — it never guesses
CONFIRMED.

It implements the :class:`EffectVerifier` protocol so it composes with the same
runtime plumbing, with ``substrate="onscreen"``.
"""

from __future__ import annotations

import difflib
from typing import Any, Optional

from openadapt_flow.ir import Region
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
)

# Fuzzy-match floor for accepting an OCR read-back as the expected value.
# Deliberately strict (mirrors resolver.OCR_MIN_RATIO): a near-miss on a
# consequential read-back must HALT, not silently confirm.
READBACK_MIN_RATIO = 0.9

# Minimum OCR confidence for a line to count as "readable text present". Below
# this the region is treated as unreadable -> INDETERMINATE (halt), never a
# fabricated REFUTED/CONFIRMED.
READABLE_CONF = 0.3


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


class OnScreenReadbackVerifier:
    """Verify a write by OCR-reading the saved-state region of the SAME screen.

    Args:
        backend: Anything exposing ``screenshot() -> PNG bytes`` (the
            remote-display backend under test).
        region: The ``(x, y, w, h)`` saved-state region to OCR (e.g. the note
            field or the status label the app updates on save), in the same
            pixel space as the screenshot.
        min_ratio: Fuzzy-match floor to accept the expected value.
        vision: Namespace exposing ``ocr(png, region=...)`` (injectable for
            tests); defaults to :mod:`openadapt_flow.vision`.
    """

    substrate = "onscreen"

    def __init__(
        self,
        backend: Any,
        region: Region,
        *,
        min_ratio: float = READBACK_MIN_RATIO,
        vision: Any = None,
    ) -> None:
        self._backend = backend
        self._region = region
        self._min_ratio = min_ratio
        if vision is None:
            from openadapt_flow import vision as _vision

            vision = _vision
        self._vision = vision

    # -- region read ---------------------------------------------------------

    def _read_region(self) -> tuple[str, float]:
        """OCR the region; return (joined normalized text, max line confidence)."""
        png = self._backend.screenshot()
        lines = self._vision.ocr(png, region=self._region)
        if not lines:
            return "", 0.0
        text = _normalize(" ".join(line.text for line in lines))
        conf = max((float(line.confidence) for line in lines), default=0.0)
        return text, conf

    def _match(self, haystack: str, expected: str) -> bool:
        """Whether ``expected`` is present in ``haystack`` at ``min_ratio``."""
        want = _normalize(expected)
        if not want:
            return False
        if want in haystack:
            return True
        # Longest contiguous run (tolerant of the value embedded in surrounding
        # label text, e.g. "saved note for patient 3" containing the value).
        squashed_want = "".join(want.split())
        squashed_hay = "".join(haystack.split())
        matcher = difflib.SequenceMatcher(
            None, squashed_want, squashed_hay, autojunk=False
        )
        longest = max((b.size for b in matcher.get_matching_blocks()), default=0)
        return longest >= self._min_ratio * len(squashed_want)

    # -- direct API ----------------------------------------------------------

    def read_back(self, expected_text: str) -> EffectVerdict:
        """Rule on whether ``expected_text`` is on the saved-state region NOW.

        - CONFIRMED   — the expected value reads back (fuzzy) in the region.
        - INDETERMINATE — the region has no readable text -> HALT (cannot certify).
        - REFUTED     — the region has readable text that is NOT the expected
          value -> HALT (affirmative on-screen contradiction).
        """
        text, conf = self._read_region()
        if not text or conf < READABLE_CONF:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=EffectKind.FIELD_EQUALS,
                substrate=self.substrate,
                reason=(
                    "saved-state region unreadable (no OCR text above "
                    f"confidence {READABLE_CONF}); cannot certify write "
                    "(SAME-SURFACE read-back)"
                ),
                observed_value=text or None,
                expected_value=expected_text,
            )
        if self._match(text, expected_text):
            return EffectVerdict(
                verdict=Verdict.CONFIRMED,
                kind=EffectKind.FIELD_EQUALS,
                substrate=self.substrate,
                reason="expected value read back on-screen (SAME-SURFACE)",
                observed_value=text,
                expected_value=expected_text,
            )
        return EffectVerdict(
            verdict=Verdict.REFUTED,
            kind=EffectKind.FIELD_EQUALS,
            substrate=self.substrate,
            reason=(
                "saved-state region shows readable text that is NOT the "
                "expected value (SAME-SURFACE contradiction)"
            ),
            observed_value=text,
            expected_value=expected_text,
        )

    # -- EffectVerifier protocol --------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        """Snapshot the region's text BEFORE the write (baseline for audit)."""
        text, conf = self._read_region()
        return EffectState(
            substrate=self.substrate,
            reachable=bool(text) and conf >= READABLE_CONF,
            records=[{"text": text}] if text else [],
            detail={"region": list(self._region), "same_surface": True},
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        """Verify ``expected`` (a ``field_equals``/``record_written`` Effect).

        The expected string is taken from ``expected.value`` (field_equals) or,
        failing that, any single ``match`` selector value — resolved against a
        ``context`` mapping of run params when present.
        """
        params = context if isinstance(context, dict) else {}
        expected_text = self._expected_text(expected, params)
        if expected_text is None:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason="no expected value derivable from the effect contract",
            )
        verdict = self.read_back(expected_text)
        return verdict.model_copy(update={"kind": expected.kind})

    @staticmethod
    def _expected_text(expected: Effect, params: dict) -> Optional[str]:
        if expected.value is not None:
            resolved = expected.value.resolve(params)
            if resolved:
                return resolved
        for v in expected.match.values():
            resolved = v.resolve(params)
            if resolved:
                return resolved
        return None

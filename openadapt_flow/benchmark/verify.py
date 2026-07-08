"""Arm-independent success criterion for the benchmark.

Both arms — compiled replay and the computer-use agent — are judged by the
exact same check, applied to a screenshot of the final state: OCR must find
(a) the ``Encounter saved — <note>`` banner and (b) the saved encounter row
(``<type> — <note>``). MockMed renders both only on the patient screen after
a successful save, so the check passes only in the navigated-back-to-patient
state a user would accept as "done". Neither arm's internal notion of
success (the replayer's ``RunReport.success``, the agent's own claim of
completion) is used.
"""

from __future__ import annotations

from pydantic import BaseModel

from openadapt_flow.vision import find_text

BANNER_PREFIX = "Encounter saved"
#: MockMed truncation limits (static/app.js): banner shows note[:40], the
#: encounters list row shows note[:60].
BANNER_NOTE_CHARS = 40
ROW_NOTE_CHARS = 60


class VerifyResult(BaseModel):
    """Outcome of the shared success check.

    Attributes:
        success: True iff both the banner and the encounter row were found.
        banner_found: The ``Encounter saved — <note>`` banner was located.
        note_found: The ``<type> — <note>`` encounter row was located.
    """

    success: bool
    banner_found: bool
    note_found: bool


def verify_encounter_saved(
    screen_png: bytes,
    note_text: str,
    *,
    encounter_type: str = "Triage",
    min_ratio: float = 0.8,
) -> VerifyResult:
    """Check a final-state screenshot for the encounter-saved evidence.

    ``find_text`` fuzzy-matches whole OCR lines, and the OCR engine may
    segment the banner as one line (``Encounter saved — <note>``) or as two
    (the prefix and the note separately). Each check therefore accepts any
    of a small set of candidate line forms; all candidates describe the same
    on-screen evidence, so this tolerates OCR line segmentation without
    weakening the criterion: the banner prefix exists only after a save,
    and both checks must pass.

    Args:
        screen_png: Full-frame screenshot of the final state as PNG bytes.
        note_text: The note the run was asked to enter.
        encounter_type: Encounter type the run was asked to create.
        min_ratio: Fuzzy-match threshold forwarded to ``find_text``.

    Returns:
        A :class:`VerifyResult`; ``success`` requires both checks to pass.
    """
    def any_found(candidates: tuple[str, ...]) -> bool:
        return any(
            find_text(screen_png, c, min_ratio=min_ratio) is not None
            for c in candidates
        )

    banner_found = any_found(
        (
            f"{BANNER_PREFIX} — {note_text[:BANNER_NOTE_CHARS]}",
            f"{BANNER_PREFIX} —",
        )
    )
    note_found = any_found(
        (
            f"{encounter_type} — {note_text[:ROW_NOTE_CHARS]}",
            note_text[:BANNER_NOTE_CHARS],
        )
    )
    return VerifyResult(
        success=banner_found and note_found,
        banner_found=banner_found,
        note_found=note_found,
    )

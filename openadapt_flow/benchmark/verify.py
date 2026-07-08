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

    Args:
        screen_png: Full-frame screenshot of the final state as PNG bytes.
        note_text: The note the run was asked to enter.
        encounter_type: Encounter type the run was asked to create.
        min_ratio: Fuzzy-match threshold forwarded to ``find_text``.

    Returns:
        A :class:`VerifyResult`; ``success`` requires both checks to pass.
    """
    banner = find_text(
        screen_png,
        f"{BANNER_PREFIX} — {note_text[:BANNER_NOTE_CHARS]}",
        min_ratio=min_ratio,
    )
    row = find_text(
        screen_png,
        f"{encounter_type} — {note_text[:ROW_NOTE_CHARS]}",
        min_ratio=min_ratio,
    )
    return VerifyResult(
        success=banner is not None and row is not None,
        banner_found=banner is not None,
        note_found=row is not None,
    )

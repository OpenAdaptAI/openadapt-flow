"""Write API eligibility results into the governed results artifact set.

The dental fulfillment's v1 system of record is a practice-local artifact
folder (a results CSV the front desk reads, plus the supporting documents).
The API tier lands its answers in the SAME set: one appended CSV row per
check and the raw 271 response, written byte-exact, alongside it.

**Effect verification is source-agnostic** -- that is the competitive story.
Whether a portal replay, a clearinghouse API, or a human produced the
artifact, the same
:class:`~openadapt_flow.runtime.effects.document_hash.DocumentHashVerifier`
(``docs/EFFECT_KIT.md``, the ``document-hash`` substrate) certifies that
exactly one raw-271 document landed and that its bytes match the digest the
client computed on the wire (:attr:`EligibilityResult.raw_271_sha256`). A
truncated write, a duplicate export, or a missing store is REFUTED /
INDETERMINATE -> the check HALTs into the queue instead of a wrong row
silently becoming the practice's answer.

PHI note: the artifact set intentionally carries member-identifying fields
(member ID, the raw 271) -- it IS the practice's local system of record, on
the practice's machine. Nothing here logs row contents; failures reference
file names and digests only.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel, Field

from openadapt_flow.eligibility.client import EligibilityResult
from openadapt_flow.runtime.effects.document_hash import DocumentHashVerifier
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectVerdict,
    ValueExpr,
    Verdict,
)

#: The results CSV inside the artifact directory.
RESULTS_CSV = "eligibility_results.csv"

#: Glob for raw-271 documents inside the artifact directory (the document
#: store the hash verifier certifies).
RAW_271_GLOB = "271_*.json"

_CSV_COLUMNS = [
    "checked_at",
    "payer",
    "payer_id",
    "member_id",
    "status",
    "plan_name",
    "copay",
    "coinsurance_percent",
    "deductible",
    "out_of_pocket_maximum",
    "service_type_codes",
    "aaa_codes",
    "source",
    "raw_271_file",
    "raw_271_sha256",
]


class EligibilityArtifact(BaseModel):
    """What one check wrote into the artifact set, plus its effect contracts.

    The two effects are the document-hash substrate's standard pair
    (``docs/EFFECT_KIT.md``): exactly ONE raw-271 document for this check
    (``record_written``), whose bytes hash to the wire digest
    (``field_equals`` on ``sha256``).
    """

    artifact_dir: str
    results_csv: str
    raw_271_file: Optional[str] = None
    raw_271_sha256: Optional[str] = None
    effects: list[Effect] = Field(default_factory=list)


def _raw_271_name(result: EligibilityResult) -> str:
    digest = result.raw_271_sha256 or "unknown"
    return f"271_{digest[:16]}.json"


def write_eligibility_artifacts(
    result: EligibilityResult,
    artifact_dir: Union[str, Path],
    *,
    member_id: Optional[str] = None,
    payer: Optional[str] = None,
) -> EligibilityArtifact:
    """Append the result row to the CSV and write the raw 271 byte-exact.

    Only a result that RETAINED response bytes gets a raw-271 document (a
    transport failure has nothing to retain; its row still lands so the
    check's outcome is on the record). Returns the artifact description with
    the effect contracts to verify.

    Raises:
        FileExistsError: When this check's raw-271 document already exists --
            an at-most-once violation surfaced loudly rather than silently
            overwritten (re-running a check produces new response bytes and
            therefore a new document name).
    """
    root = Path(artifact_dir)
    root.mkdir(parents=True, exist_ok=True)

    raw_name: Optional[str] = None
    effects: list[Effect] = []
    if result.raw_271_bytes is not None and result.raw_271_sha256 is not None:
        raw_name = _raw_271_name(result)
        raw_path = root / raw_name
        if raw_path.exists():
            raise FileExistsError(
                f"raw-271 document {raw_name} already exists in "
                f"{root} -- refusing to overwrite the system of record"
            )
        raw_path.write_bytes(result.raw_271_bytes)
        effects = [
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={"name": ValueExpr(literal=raw_name)},
                expected_count=1,
                probe=(
                    "exactly one raw-271 document for this check in the "
                    "results artifact set"
                ),
            ),
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match={"name": ValueExpr(literal=raw_name)},
                field="sha256",
                value=ValueExpr(literal=result.raw_271_sha256),
                probe="raw-271 bytes match the wire digest",
            ),
        ]

    csv_path = root / RESULTS_CSV
    is_new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(
            {
                "checked_at": result.checked_at,
                "payer": payer or result.payer_name or "",
                "payer_id": result.payer_id,
                "member_id": member_id or "",
                "status": result.status.value,
                "plan_name": result.plan_name or "",
                "copay": result.copay or "",
                "coinsurance_percent": result.coinsurance_percent or "",
                "deductible": result.deductible or "",
                "out_of_pocket_maximum": result.out_of_pocket_maximum or "",
                "service_type_codes": " ".join(result.service_type_codes),
                "aaa_codes": " ".join(result.aaa_codes),
                "source": result.source,
                "raw_271_file": raw_name or "",
                "raw_271_sha256": result.raw_271_sha256 or "",
            }
        )

    return EligibilityArtifact(
        artifact_dir=str(root),
        results_csv=str(csv_path),
        raw_271_file=raw_name,
        raw_271_sha256=result.raw_271_sha256,
        effects=effects,
    )


def write_and_verify(
    result: EligibilityResult,
    artifact_dir: Union[str, Path],
    *,
    member_id: Optional[str] = None,
    payer: Optional[str] = None,
) -> tuple[EligibilityArtifact, list[EffectVerdict]]:
    """Snapshot -> write -> verify, the kit's standard bracket.

    Returns the artifact plus one verdict per effect contract. Callers HALT
    the check into the queue unless EVERY verdict is CONFIRMED -- the same
    rule a portal replay's writes live under.
    """
    verifier = DocumentHashVerifier(Path(artifact_dir), glob=RAW_271_GLOB)
    before = verifier.capture_pre_state()
    artifact = write_eligibility_artifacts(
        result, artifact_dir, member_id=member_id, payer=payer
    )
    verdicts = [verifier.verify(effect, before) for effect in artifact.effects]
    return artifact, verdicts


def all_confirmed(verdicts: list[EffectVerdict]) -> bool:
    """Whether every verdict CONFIRMED (the only outcome that records the
    check as done; anything else halts it into the queue)."""
    return bool(verdicts) and all(v.verdict is Verdict.CONFIRMED for v in verdicts)

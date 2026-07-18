"""Artifact write + document-hash verification roundtrip for API results.

The API result is written into the same practice-local results artifact set
the portal replay uses (results CSV + raw-271 document) and certified by the
kit's document-hash verifier -- effect verification is source-agnostic.
"""

from __future__ import annotations

import csv
import json

import pytest

from openadapt_flow.eligibility.artifact import (
    RESULTS_CSV,
    all_confirmed,
    write_and_verify,
    write_eligibility_artifacts,
)
from openadapt_flow.eligibility.client import (
    EligibilityResult,
    EligibilityStatus,
    parse_271,
)
from openadapt_flow.runtime.effects.document_hash import DocumentHashVerifier
from openadapt_flow.runtime.effects.effect import Verdict

ACTIVE_271 = {
    "payer": {"name": "Cigna"},
    "planInformation": {"groupDescription": "DENTAL PPO"},
    "benefitsInformation": [
        {"code": "1", "name": "Active Coverage", "serviceTypeCodes": ["35"]},
        {"code": "C", "name": "Deductible", "benefitAmount": "50"},
        {"code": "B", "name": "Co-Payment", "benefitAmount": "20"},
    ],
    "x12": "ISA*...~ST*271*0001~...",
}


def active_result() -> EligibilityResult:
    return parse_271(
        "62308", json.dumps(ACTIVE_271).encode(), service_type_codes=["35"]
    )


def test_write_and_verify_roundtrip_confirms(tmp_path):
    artifact, verdicts = write_and_verify(
        active_result(), tmp_path, member_id="U3141592653", payer="Cigna Dental"
    )
    assert len(verdicts) == 2  # record_written + sha256 field_equals
    assert all_confirmed(verdicts), [v.reason for v in verdicts]
    assert artifact.raw_271_file is not None
    assert artifact.raw_271_sha256 is not None
    # The raw-271 document is byte-exact: its digest IS the wire digest.
    raw = tmp_path / artifact.raw_271_file
    import hashlib

    assert hashlib.sha256(raw.read_bytes()).hexdigest() == artifact.raw_271_sha256


def test_results_csv_row_carries_normalized_fields_and_digest(tmp_path):
    artifact, _ = write_and_verify(
        active_result(), tmp_path, member_id="U3141592653", payer="Cigna Dental"
    )
    with (tmp_path / RESULTS_CSV).open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == EligibilityStatus.ACTIVE.value
    assert row["payer"] == "Cigna Dental"
    assert row["member_id"] == "U3141592653"
    assert row["plan_name"] == "DENTAL PPO"
    assert row["deductible"] == "50"
    assert row["copay"] == "20"
    assert row["raw_271_file"] == artifact.raw_271_file
    assert row["raw_271_sha256"] == artifact.raw_271_sha256  # audit chain


def test_tampered_raw_271_is_refuted(tmp_path):
    artifact, verdicts = write_and_verify(active_result(), tmp_path)
    assert all_confirmed(verdicts)
    raw = tmp_path / artifact.raw_271_file
    raw.write_bytes(raw.read_bytes() + b" tampered")
    verifier = DocumentHashVerifier(tmp_path, glob="271_*.json")
    before = verifier.capture_pre_state()
    # Re-check the field_equals (sha256) contract against the tampered store.
    field_effect = artifact.effects[1]
    verdict = verifier.verify(field_effect, before)
    assert verdict.verdict is Verdict.REFUTED


def test_duplicate_write_refuses_loudly(tmp_path):
    result = active_result()
    write_eligibility_artifacts(result, tmp_path)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_eligibility_artifacts(result, tmp_path)


def test_no_answer_result_writes_row_but_no_document(tmp_path):
    down = parse_271(
        "62308",
        json.dumps({"errors": [{"code": "42", "description": "down"}]}).encode(),
    )
    # AAA responses DO retain bytes; simulate a transport failure (no bytes).
    down = down.model_copy(update={"raw_271_bytes": None, "raw_271_sha256": None})
    artifact, verdicts = write_and_verify(down, tmp_path)
    assert artifact.raw_271_file is None
    assert artifact.effects == []
    assert verdicts == []
    assert not all_confirmed(verdicts)  # nothing verified -> never "done"
    with (tmp_path / RESULTS_CSV).open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["status"] == EligibilityStatus.PAYER_UNAVAILABLE.value
    assert rows[0]["raw_271_file"] == ""


def test_two_checks_append_two_rows_and_two_documents(tmp_path):
    first = active_result()
    second = parse_271(
        "62308", json.dumps({**ACTIVE_271, "controlNumber": "2"}).encode()
    )
    a1, v1 = write_and_verify(first, tmp_path)
    a2, v2 = write_and_verify(second, tmp_path)
    assert all_confirmed(v1) and all_confirmed(v2)
    assert a1.raw_271_file != a2.raw_271_file
    with (tmp_path / RESULTS_CSV).open() as fh:
        assert len(list(csv.DictReader(fh))) == 2

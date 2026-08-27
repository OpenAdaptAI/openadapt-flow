#!/usr/bin/env python3
"""Regenerate the deterministic non-success terminal-v2 test vectors."""

from __future__ import annotations

import argparse
import hashlib
import json
from base64 import b64encode
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from openadapt_flow.qualification_admission_v2 import canonical_json
from openadapt_flow.terminal_verification_v2 import (
    SIGNATURE_DOMAIN,
    evidence_runner_key_id,
    evidence_runner_signer_sha256,
    sign_production_terminal_verification,
)
from tests.test_terminal_verification_v2 import (
    _halted_payload,
    _private_key,
    _reconciliation_payload,
)

DEFAULT_OUTPUT = Path("tests/fixtures/terminal_verification_v2_terminal_vectors.json")


def _vector(name: str, *, uncertain_delivery: bool) -> dict[str, object]:
    payload = (
        _halted_payload()
        if name == "halted-before-effect-zero-permit"
        else _reconciliation_payload()
    )
    envelope = sign_production_terminal_verification(payload, _private_key())
    raw = canonical_json(envelope)
    effect_state = payload.evidence_manifests.effect.records[0]
    return {
        "callback": {
            "artifact_bytes_source": "envelope_canonical_base64",
            "outcome": payload.run_receipt.transaction_outcome,
            "report_sha256": payload.run_report_sha256,
            "run_id": payload.run_id,
            "schema_version": "openadapt.hosted-runner-terminal/v1",
            "started": True,
            "uncertain_delivery": uncertain_delivery,
        },
        "effect_state": effect_state.model_dump(mode="json"),
        "envelope_canonical_base64": b64encode(raw).decode("ascii"),
        "name": name,
        "payload_canonical_sha256": hashlib.sha256(
            payload.canonical_bytes()
        ).hexdigest(),
        "signature": envelope.signature,
        "terminal_verification_artifact_sha256": envelope.artifact_sha256(),
    }


def build_fixture() -> dict[str, object]:
    private_key = _private_key()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "key_id": evidence_runner_key_id(public_bytes),
        "private_key_base64": b64encode(private_bytes).decode("ascii"),
        "public_key_base64": b64encode(public_bytes).decode("ascii"),
        "schema_version": "openadapt.production-terminal-cross-language-vectors/v2",
        "signature_domain_base64": b64encode(SIGNATURE_DOMAIN).decode("ascii"),
        "signer_sha256": evidence_runner_signer_sha256(public_bytes),
        "vectors": [
            _vector(
                "halted-before-effect-zero-permit",
                uncertain_delivery=False,
            ),
            _vector(
                "reconciliation-required-pending-permit",
                uncertain_delivery=True,
            ),
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(build_fixture(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

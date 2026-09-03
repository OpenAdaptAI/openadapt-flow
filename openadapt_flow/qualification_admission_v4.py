"""Verify ``openadapt.qualification-admission/v4`` as issued on ``.github``.

Standard and Regulated actuation consume this object. Demo does not. The
verifier pins one published inner key and the published signer-registry
identity. It does not trust any other key. ``expires_at`` null means
until-revoked. A non-null ``revoked_at`` on the pinned key, or a revocation
entry for the admission, fails closed.

The issued synthetic admission binds one exact ``bundle_sha256``. A live run
must reproduce that digest. Do not attach this object to a different bundle.
"""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64decode, urlsafe_b64decode
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, StrictInt

SCHEMA: Final[Literal["openadapt.qualification-admission/v4"]] = (
    "openadapt.qualification-admission/v4"
)
SIGNER_REGISTRY_SCHEMA: Final[Literal["openadapt.qualification-signer-registry/v2"]] = (
    "openadapt.qualification-signer-registry/v2"
)
RECEIPT_SCHEMA: Final[
    Literal["openadapt.qualification-evidence-decision-receipt/v2"]
] = "openadapt.qualification-evidence-decision-receipt/v2"
REVOCATION_SCHEMA: Final[
    Literal["openadapt.qualification-revocation-state-receipt/v1"]
] = "openadapt.qualification-revocation-state-receipt/v1"

ADMISSION_DOMAIN: Final[bytes] = b"OpenAdapt qualification admission v4\0"
SIGNER_REGISTRY_IDENTITY_DOMAIN: Final[bytes] = (
    b"OpenAdapt qualification signer registry v2\0"
)
DECISION_RECEIPT_SIGNATURE_DOMAIN: Final[bytes] = (
    b"OpenAdapt qualification evidence decision receipt v2\0"
)
REVOCATION_STATE_SIGNATURE_DOMAIN: Final[bytes] = (
    b"OpenAdapt qualification revocation state receipt v1\0"
)

PINNED_KEY_ID: Final[Literal["qa-ed25519-9cf4bca214c01d79"]] = (
    "qa-ed25519-9cf4bca214c01d79"
)
PINNED_PUBLIC_KEY: Final[str] = "vHPUDLG2WD2BnTLaKYnZd9GxvUKfjpd68gJ9HubIEH8"
PINNED_PUBLIC_KEY_SHA256: Final[str] = (
    "sha256:465646ab5137f05dfba094fb05fc10e3af74a0c2e2d0fcc20814b8f1f8271170"
)
PINNED_REGISTRY_IDENTITY_SHA256: Final[str] = (
    "sha256:e243a23243b24986ed08812e284a1bd8c4993814149f02bcba79cc520e10ca14"
)

PINNED_ADMISSION_ISSUER: Final[dict[str, str]] = {
    "repository": "OpenAdaptAI/.github",
    "repository_id": "858454062",
    "repository_owner_id": "132681217",
    "workflow": ".github/workflows/issue-qualification-admission.yml",
    "ref": "refs/heads/main",
    "environment": "qualification-admission",
}
PINNED_RECEIPT_WORKFLOW: Final[str] = (
    "https://github.com/OpenAdaptAI/.github/.github/workflows/"
    "issue-synthetic-qualification-evidence-decision.yml@refs/heads/main"
)
PINNED_REVOCATION_WORKFLOW: Final[str] = (
    "https://github.com/OpenAdaptAI/openadapt-ops/.github/workflows/"
    "qualification-revocation-state.yml@refs/heads/main"
)

LOCAL_IDENTITY_OPENING: Final[dict[str, Any]] = {
    "schema_version": "openadapt.qualification-local-identity-opening/v1",
    "algorithm": "hmac-sha256",
    "required": True,
    "customer_controlled_secret_required": True,
    "exact_contract_match_required": True,
    "revalidation_before_actuation": True,
    "maximum_age_seconds": 60,
}

ADMISSION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "admission_id_sha256",
        "evidence_class",
        "organization_id_sha256",
        "workflow_id_sha256",
        "workflow_version_id_sha256",
        "bundle_version",
        "bundle_sha256",
        "admitted_runtime_sha256",
        "application_contract_sha256",
        "environment_contract_sha256",
        "input_contract_sha256",
        "action_contract_sha256",
        "identity_contract_sha256",
        "effect_contract_sha256",
        "policy_contract_sha256",
        "evidence_authority_sha256",
        "campaign_artifact_sha256",
        "campaign_permit_sha256",
        "decision_receipt_reference",
        "decision_receipt_bundle_reference",
        "signer_registry_sha256",
        "revocation_state_sha256",
        "entity_class",
        "campaign_summary",
        "local_identity_opening",
        "verdict",
        "issued_at",
        "not_before",
        "expires_at",
        "issuer",
    }
)
ISSUER_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "repository",
        "repository_id",
        "repository_owner_id",
        "workflow",
        "ref",
        "source_commit",
        "environment",
    }
)
CAMPAIGN_CLASSES: Final[tuple[str, ...]] = (
    "healthy",
    "safe_halt",
    "idempotency_replay",
    "uncertain_delivery",
    "declared_attended",
    "governed_repair",
)
CAMPAIGN_COUNT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "task_condition_cell_count",
        "minimum_trials_per_cell",
        "observed_trial_count",
        "silent_incorrect_success_count",
        "over_halt_count",
        "unsafe_effect_count",
        "blind_retry_count",
        "replay_dispatch_count",
        "model_call_count",
        "unplanned_intervention_count",
        "reconciliation_required_count",
        "authenticated_bound_decision_count",
        "live_target_revalidation_count",
        "policy_approved_repair_count",
        "approved_repair_count",
        "retained_repair_evidence_count",
        "unverified_direct_action_count",
    }
)
ADMISSION_RECEIPT_BINDINGS: Final[tuple[tuple[str, str], ...]] = (
    ("evidence_class", "evidence_class"),
    ("organization_id_sha256", "organization_id_sha256"),
    ("workflow_id_sha256", "workflow_id_sha256"),
    ("workflow_version_id_sha256", "workflow_version_id_sha256"),
    ("bundle_version", "bundle_version"),
    ("bundle_sha256", "bundle_sha256"),
    ("admitted_runtime_sha256", "admitted_runtime_sha256"),
    ("application_contract_sha256", "application_contract_sha256"),
    ("environment_contract_sha256", "environment_contract_sha256"),
    ("input_contract_sha256", "input_contract_sha256"),
    ("action_contract_sha256", "action_contract_sha256"),
    ("identity_contract_sha256", "identity_contract_sha256"),
    ("effect_contract_sha256", "effect_contract_sha256"),
    ("policy_contract_sha256", "policy_contract_sha256"),
    ("evidence_authority_sha256", "evidence_authority_contract_sha256"),
    ("campaign_artifact_sha256", "campaign_artifact_sha256"),
    ("campaign_permit_sha256", "campaign_permit_sha256"),
    ("signer_registry_sha256", "signer_registry_sha256"),
    ("revocation_state_sha256", "revocation_state_sha256"),
    ("entity_class", "entity_class"),
)

_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_HEX40_RE = re.compile(r"^[a-f0-9]{40}$")
_BUNDLE_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]{0,9})\.(0|[1-9][0-9]{0,9})\."
    r"(0|[1-9][0-9]{0,9})(-[0-9A-Za-z]+([.-][0-9A-Za-z]+)*)?$"
)
_ENTITY_CLASS_RE = re.compile(r"^[a-z][a-z0-9 -]{0,63}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9+/]{86}==$")


class QualificationAdmissionV4Error(ValueError):
    """The v4 qualification admission is missing, mismatched, or untrusted."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def object_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value) + b"\n").hexdigest()


def raw_object_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value) + b"\n").hexdigest()


def digest_bytes(domain: bytes, value: Any) -> str:
    return "sha256:" + hashlib.sha256(domain + canonical_json(value)).hexdigest()


def signer_registry_identity_sha256(registry: Mapping[str, Any]) -> str:
    return digest_bytes(SIGNER_REGISTRY_IDENTITY_DOMAIN, registry)


def normalize_digest(value: str) -> str:
    if value.startswith("sha256:"):
        return value[7:]
    return value


def _closed(
    value: Any, fields: frozenset[str] | set[str], label: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(fields):
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise QualificationAdmissionV4Error(
            f"{label} must contain exactly {sorted(fields)}; got {actual}"
        )
    return value


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise QualificationAdmissionV4Error(
            f"{label} must be a lowercase sha256 digest"
        )
    return value


def _require_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise QualificationAdmissionV4Error(
            f"{label} must be a canonical UTC timestamp"
        )
    return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)


def _optional_timestamp(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    return _require_timestamp(value, label)


def _require_int(value: Any, label: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise QualificationAdmissionV4Error(
            f"{label} must be an integer of at least {minimum}"
        )
    return value


def _parse_utc_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise QualificationAdmissionV4Error("verification time must be timezone-aware")
    return current.astimezone(timezone.utc)


def _decode_pinned_public_key() -> bytes:
    padded = PINNED_PUBLIC_KEY + "=" * (-len(PINNED_PUBLIC_KEY) % 4)
    try:
        raw = urlsafe_b64decode(padded)
    except (ValueError, TypeError) as exc:
        raise QualificationAdmissionV4Error(
            "pinned qualification public key is invalid"
        ) from exc
    if len(raw) != 32:
        raise QualificationAdmissionV4Error(
            "pinned qualification public key is invalid"
        )
    spki = bytes.fromhex("302a300506032b6570032100") + raw
    digest = "sha256:" + hashlib.sha256(spki).hexdigest()
    key_id = "qa-ed25519-" + hashlib.sha256(raw).hexdigest()[:16]
    if digest != PINNED_PUBLIC_KEY_SHA256 or key_id != PINNED_KEY_ID:
        raise QualificationAdmissionV4Error(
            "pinned qualification public key is invalid"
        )
    return raw


def _workflow_identity(issuer: Mapping[str, Any]) -> str:
    return (
        f"https://github.com/{issuer['repository']}/{issuer['workflow']}"
        f"@{issuer['ref']}"
    )


def _signing_statement(
    value: Mapping[str, Any],
    *,
    object_schema_version: str,
    signature_domain: bytes,
) -> dict[str, Any]:
    unsigned = dict(value)
    unsigned.pop("signature", None)
    unsigned.pop("signing_statement", None)
    unsigned_bytes = canonical_json(unsigned) + b"\n"
    return {
        "schema_version": "openadapt.qualification-evidence-signing-statement/v1",
        "object_schema_version": object_schema_version,
        "signature_domain": signature_domain.decode("utf-8"),
        "unsigned_object_sha256": (
            "sha256:" + hashlib.sha256(unsigned_bytes).hexdigest()
        ),
        "unsigned_size_bytes": len(unsigned_bytes),
        "commitment_scheme": "sha256-canonical-json-lf",
    }


def _verify_embedded_signature(
    value: Mapping[str, Any],
    *,
    object_schema_version: str,
    signature_domain: bytes,
    expected_workflow: str,
) -> None:
    if value.get("algorithm") != "ed25519":
        raise QualificationAdmissionV4Error("signed object algorithm is not ed25519")
    if value.get("issuer_key_id") != PINNED_KEY_ID:
        raise QualificationAdmissionV4Error(
            "signed object key_id is not the pinned qualification key"
        )
    issuer = value.get("issuer")
    if not isinstance(issuer, dict):
        raise QualificationAdmissionV4Error("signed object has no issuer identity")
    if _workflow_identity(issuer) != expected_workflow:
        raise QualificationAdmissionV4Error(
            "signed object issuer workflow is not pinned"
        )
    statement = value.get("signing_statement")
    expected = _signing_statement(
        value,
        object_schema_version=object_schema_version,
        signature_domain=signature_domain,
    )
    if statement != expected:
        raise QualificationAdmissionV4Error(
            "qualification evidence signing statement differs"
        )
    signature = value.get("signature")
    if not isinstance(signature, str) or _SIGNATURE_RE.fullmatch(signature) is None:
        raise QualificationAdmissionV4Error("signed object signature is not canonical")
    try:
        raw_signature = b64decode(signature, validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(_decode_pinned_public_key())
        public_key.verify(raw_signature, canonical_json(expected) + b"\n")
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise QualificationAdmissionV4Error(
            "signed object Ed25519 signature is invalid"
        ) from exc


def verify_pinned_signer_registry(value: Any) -> dict[str, Any]:
    """Accept only the published until-revoked registry and pinned inner key."""

    registry = _closed(
        value,
        {"schema_version", "revision", "generated_at", "expires_at", "signers"},
        "signer registry",
    )
    if registry["schema_version"] != SIGNER_REGISTRY_SCHEMA:
        raise QualificationAdmissionV4Error("signer registry schema is not supported")
    _require_int(registry["revision"], "signer registry revision", minimum=1)
    _require_timestamp(registry["generated_at"], "signer registry generated_at")
    if registry["expires_at"] is not None:
        raise QualificationAdmissionV4Error(
            "qualification signer registry must be until-revoked"
        )
    identity = signer_registry_identity_sha256(registry)
    if identity != PINNED_REGISTRY_IDENTITY_SHA256:
        raise QualificationAdmissionV4Error(
            "qualification signer registry is not the pinned published registry"
        )
    signers = registry["signers"]
    if not isinstance(signers, list):
        raise QualificationAdmissionV4Error("signer registry signers are invalid")
    matches = [
        item
        for item in signers
        if isinstance(item, dict) and item.get("key_id") == PINNED_KEY_ID
    ]
    if len(matches) != 1:
        raise QualificationAdmissionV4Error(
            "pinned qualification key is missing from the signer registry"
        )
    signer = matches[0]
    if (
        signer.get("algorithm") != "ed25519"
        or signer.get("status") != "active"
        or signer.get("revoked_at") is not None
        or signer.get("public_key") != PINNED_PUBLIC_KEY
        or signer.get("public_key_sha256") != PINNED_PUBLIC_KEY_SHA256
    ):
        raise QualificationAdmissionV4Error(
            "pinned qualification key is not an active until-revoked signer"
        )
    usages = signer.get("allowed_usages")
    if not isinstance(
        usages, list
    ) or "qualification-evidence-decision-receipt" not in (usages):
        raise QualificationAdmissionV4Error(
            "pinned qualification key cannot sign a decision receipt"
        )
    if "qualification-revocation-state-receipt" not in usages:
        raise QualificationAdmissionV4Error(
            "pinned qualification key cannot sign a revocation state"
        )
    workflows = signer.get("allowed_workflows")
    if not isinstance(workflows, list) or PINNED_RECEIPT_WORKFLOW not in workflows:
        raise QualificationAdmissionV4Error(
            "pinned qualification key cannot sign the synthetic decision workflow"
        )
    if PINNED_REVOCATION_WORKFLOW not in workflows:
        raise QualificationAdmissionV4Error(
            "pinned qualification key cannot sign the revocation workflow"
        )
    return registry


def _validate_campaign_summary(value: Any) -> dict[str, Any]:
    summary = _closed(value, set(CAMPAIGN_CLASSES), "campaign summary")
    for campaign_class in CAMPAIGN_CLASSES:
        counts = _closed(
            summary[campaign_class], CAMPAIGN_COUNT_FIELDS, f"{campaign_class} counts"
        )
        for key, count in counts.items():
            _require_int(
                count,
                f"{campaign_class} {key}",
                minimum=3 if key == "minimum_trials_per_cell" else 0,
            )
        cells = counts["task_condition_cell_count"]
        trials = counts["observed_trial_count"]
        if cells < 1 or trials < cells * counts["minimum_trials_per_cell"]:
            raise QualificationAdmissionV4Error(
                f"{campaign_class} does not have three trials per cell"
            )
        if (
            counts["unsafe_effect_count"]
            or counts["silent_incorrect_success_count"]
            or counts["blind_retry_count"]
        ):
            raise QualificationAdmissionV4Error(
                f"{campaign_class} contains a forbidden failure"
            )
    for campaign_class in ("healthy", "idempotency_replay"):
        counts = summary[campaign_class]
        if counts["model_call_count"] or counts["unplanned_intervention_count"]:
            raise QualificationAdmissionV4Error(
                f"{campaign_class} is not a zero-model healthy path"
            )
    uncertain = summary["uncertain_delivery"]
    if uncertain["reconciliation_required_count"] != uncertain["observed_trial_count"]:
        raise QualificationAdmissionV4Error(
            "every uncertain-delivery trial must require reconciliation"
        )
    if uncertain["replay_dispatch_count"]:
        raise QualificationAdmissionV4Error("uncertain delivery cannot replay dispatch")
    return summary


def _validate_window(
    value: Mapping[str, Any], *, now: datetime, issued_field: str = "issued_at"
) -> None:
    issued = _require_timestamp(value[issued_field], issued_field)
    not_before = _require_timestamp(value["not_before"], "not_before")
    expires = _optional_timestamp(value["expires_at"], "expires_at")
    if not_before > issued:
        raise QualificationAdmissionV4Error("validity window is invalid")
    if expires is not None and not issued < expires:
        raise QualificationAdmissionV4Error("validity window is invalid")
    if now + timedelta(minutes=5) < issued:
        raise QualificationAdmissionV4Error("object is future-issued")
    if now < not_before or (expires is not None and now >= expires):
        raise QualificationAdmissionV4Error("object is not active")


def validate_qualification_admission_v4(
    value: Any, *, now: datetime | None = None
) -> dict[str, Any]:
    admission = _closed(value, ADMISSION_FIELDS, "qualification admission")
    if (
        admission["schema_version"] != SCHEMA
        or admission["verdict"] != "accepted"
        or admission["evidence_class"]
        not in {"private-customer", "remote-safe-synthetic"}
    ):
        raise QualificationAdmissionV4Error(
            "qualification admission schema or verdict is invalid"
        )
    if admission["expires_at"] is not None:
        raise QualificationAdmissionV4Error(
            "qualification admission must be until-revoked"
        )
    for field in ADMISSION_FIELDS:
        if field.endswith("_sha256"):
            _require_digest(admission[field], field)
    if (
        not isinstance(admission["bundle_version"], str)
        or _BUNDLE_VERSION_RE.fullmatch(admission["bundle_version"]) is None
    ):
        raise QualificationAdmissionV4Error(
            "qualification admission bundle version is not canonical"
        )
    if (
        not isinstance(admission["entity_class"], str)
        or _ENTITY_CLASS_RE.fullmatch(admission["entity_class"]) is None
    ):
        raise QualificationAdmissionV4Error(
            "qualification admission entity class is not remote-safe"
        )
    if admission["local_identity_opening"] != LOCAL_IDENTITY_OPENING:
        raise QualificationAdmissionV4Error(
            "local customer-bound identity opening contract differs"
        )
    _validate_campaign_summary(admission["campaign_summary"])
    issuer = _closed(
        admission["issuer"], ISSUER_FIELDS, "qualification admission issuer"
    )
    for key, expected in PINNED_ADMISSION_ISSUER.items():
        if issuer[key] != expected:
            raise QualificationAdmissionV4Error(
                "qualification admission issuer is not the pinned .github issuer"
            )
    if (
        not isinstance(issuer["source_commit"], str)
        or _HEX40_RE.fullmatch(issuer["source_commit"]) is None
    ):
        raise QualificationAdmissionV4Error(
            "qualification admission issuer source commit is invalid"
        )
    if admission["signer_registry_sha256"] != PINNED_REGISTRY_IDENTITY_SHA256:
        raise QualificationAdmissionV4Error(
            "qualification admission signer registry is not the pinned registry"
        )
    current = _parse_utc_now(now)
    _validate_window(admission, now=current)
    projection = dict(admission)
    admission_id = projection.pop("admission_id_sha256")
    if admission_id != digest_bytes(ADMISSION_DOMAIN, projection):
        raise QualificationAdmissionV4Error("qualification admission id is invalid")
    return admission


def _validate_decision_receipt(
    value: Any,
    *,
    admission: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QualificationAdmissionV4Error("decision receipt is invalid")
    if value.get("schema_version") != RECEIPT_SCHEMA:
        raise QualificationAdmissionV4Error("decision receipt schema is not supported")
    if value.get("expires_at") is not None:
        raise QualificationAdmissionV4Error("decision receipt must be until-revoked")
    if value.get("signer_registry_sha256") != PINNED_REGISTRY_IDENTITY_SHA256:
        raise QualificationAdmissionV4Error(
            "decision receipt signer registry is not the pinned registry"
        )
    _validate_window(value, now=now)
    _verify_embedded_signature(
        value,
        object_schema_version=RECEIPT_SCHEMA,
        signature_domain=DECISION_RECEIPT_SIGNATURE_DOMAIN,
        expected_workflow=PINNED_RECEIPT_WORKFLOW,
    )
    for admission_field, receipt_field in ADMISSION_RECEIPT_BINDINGS:
        if admission[admission_field] != value.get(receipt_field):
            raise QualificationAdmissionV4Error(
                f"qualification admission {admission_field} differs from the receipt"
            )
    if admission["campaign_summary"] != value.get("campaign_summary", {}).get(
        "classes"
    ):
        raise QualificationAdmissionV4Error(
            "qualification admission campaign summary differs from the receipt"
        )
    return value


def _validate_revocation_state(
    value: Any,
    *,
    admission: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QualificationAdmissionV4Error("revocation state is invalid")
    if value.get("schema_version") != REVOCATION_SCHEMA:
        raise QualificationAdmissionV4Error("revocation state schema is not supported")
    if value.get("expires_at") is not None:
        raise QualificationAdmissionV4Error("revocation state must be until-revoked")
    if value.get("status") != "current":
        raise QualificationAdmissionV4Error("revocation state is not current")
    if value.get("signer_registry_sha256") != PINNED_REGISTRY_IDENTITY_SHA256:
        raise QualificationAdmissionV4Error(
            "revocation state signer registry is not the pinned registry"
        )
    _require_digest(value.get("revocation_state_sha256"), "revocation_state_sha256")
    if admission["revocation_state_sha256"] != value["revocation_state_sha256"]:
        raise QualificationAdmissionV4Error(
            "qualification admission revocation state differs"
        )
    _validate_window(value, now=now, issued_field="observed_at")
    _verify_embedded_signature(
        value,
        object_schema_version=REVOCATION_SCHEMA,
        signature_domain=REVOCATION_STATE_SIGNATURE_DOMAIN,
        expected_workflow=PINNED_REVOCATION_WORKFLOW,
    )
    revocations = value.get("revocations")
    if not isinstance(revocations, list):
        raise QualificationAdmissionV4Error("revocation list is invalid")
    admission_identity = object_sha256(admission)
    for item in revocations:
        if not isinstance(item, dict):
            raise QualificationAdmissionV4Error("revocation entry is invalid")
        if item.get("revoked_at") is None:
            raise QualificationAdmissionV4Error("revocation entry lacks revoked_at")
        subject = (item.get("subject_kind"), item.get("subject_id"))
        if subject == ("qualification-admission", admission_identity) or subject == (
            "qualification-admission",
            admission["admission_id_sha256"],
        ):
            raise QualificationAdmissionV4Error("qualification admission is revoked")
        if subject == ("qualification-signer-key", PINNED_PUBLIC_KEY_SHA256):
            raise QualificationAdmissionV4Error("pinned qualification key is revoked")
    return value


def _validate_receipt_reference(
    reference: Any, *, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(reference, dict):
        raise QualificationAdmissionV4Error("decision receipt reference is invalid")
    if reference.get("kind") != "qualification-evidence-decision-receipt":
        raise QualificationAdmissionV4Error("decision receipt reference kind differs")
    object_digest = _require_digest(
        reference.get("object_sha256"), "decision receipt object_sha256"
    )
    if object_digest != object_sha256(receipt):
        raise QualificationAdmissionV4Error(
            "decision receipt reference does not match the signed receipt"
        )
    return reference


class VerifiedQualificationAdmissionV4(BaseModel):
    """v4 authority returned only after signature, pin, and digest checks pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    admission_artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    admission_id_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    evidence_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    registry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    registry_revision: StrictInt = Field(ge=1)
    registry_expires_at: None = None
    issuer_key_id: Literal["qa-ed25519-9cf4bca214c01d79"] = PINNED_KEY_ID
    bundle_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    admitted_runtime_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    evidence_class: Literal["private-customer", "remote-safe-synthetic"]
    revocation_state_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


def verify_qualification_admission_v4(
    admission: Mapping[str, Any] | Any,
    *,
    registry: Mapping[str, Any] | Any,
    decision_receipt: Mapping[str, Any] | Any,
    revocation_state: Mapping[str, Any] | Any,
    expected_bundle_sha256: str | None = None,
    now: datetime | None = None,
) -> VerifiedQualificationAdmissionV4:
    """Verify one issued v4 admission against the pinned published registry."""

    current = _parse_utc_now(now)
    trusted_registry = verify_pinned_signer_registry(registry)
    trusted_admission = validate_qualification_admission_v4(admission, now=current)
    trusted_receipt = _validate_decision_receipt(
        decision_receipt, admission=trusted_admission, now=current
    )
    _validate_receipt_reference(
        trusted_admission["decision_receipt_reference"],
        receipt=trusted_receipt,
    )
    _validate_revocation_state(
        revocation_state, admission=trusted_admission, now=current
    )
    if expected_bundle_sha256 is not None:
        live = expected_bundle_sha256
        if not live.startswith("sha256:"):
            live = "sha256:" + live
        if live != trusted_admission["bundle_sha256"]:
            raise QualificationAdmissionV4Error(
                "qualification admission does not bind the sealed workflow bundle"
            )
    return VerifiedQualificationAdmissionV4(
        admission_artifact_sha256=raw_object_sha256(trusted_admission),
        admission_id_sha256=trusted_admission["admission_id_sha256"],
        evidence_identity_sha256=raw_object_sha256(trusted_receipt),
        registry_sha256=normalize_digest(PINNED_REGISTRY_IDENTITY_SHA256),
        registry_revision=trusted_registry["revision"],
        issuer_key_id=PINNED_KEY_ID,
        bundle_sha256=trusted_admission["bundle_sha256"],
        admitted_runtime_sha256=trusted_admission["admitted_runtime_sha256"],
        evidence_class=trusted_admission["evidence_class"],
        revocation_state_sha256=trusted_admission["revocation_state_sha256"],
    )


__all__ = [
    "PINNED_KEY_ID",
    "PINNED_PUBLIC_KEY",
    "PINNED_REGISTRY_IDENTITY_SHA256",
    "SCHEMA",
    "QualificationAdmissionV4Error",
    "VerifiedQualificationAdmissionV4",
    "canonical_json",
    "normalize_digest",
    "object_sha256",
    "raw_object_sha256",
    "validate_qualification_admission_v4",
    "verify_pinned_signer_registry",
    "verify_qualification_admission_v4",
]

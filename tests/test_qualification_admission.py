from __future__ import annotations

from base64 import b64encode
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.qualification_admission import (
    QualificationAdmissionEnvelope,
    QualificationAdmissionError,
    QualificationAdmissionExpected,
    QualificationAdmissionPayload,
    QualificationCampaignBinding,
    QualificationCondition,
    QualificationIssuer,
    QualificationSignerTrust,
    contract_sha256,
    qualification_signer_key_id,
    sign_qualification_admission,
    verify_admission_for_actuation,
    verify_qualification_admission,
)

NOW = datetime(2026, 8, 18, 18, 0, tzinfo=timezone.utc)
UUIDS = {
    "admission_id": "11111111-1111-4111-8111-111111111111",
    "tenant_id": "22222222-2222-4222-8222-222222222222",
    "workflow_id": "33333333-3333-4333-8333-333333333333",
    "workflow_version_id": "44444444-4444-4444-8444-444444444444",
    "bundle_version_id": "55555555-5555-4555-8555-555555555555",
    "runtime_validation_id": "66666666-6666-4666-8666-666666666666",
}
DIGEST_FIELDS = (
    "bundle_artifact_sha256",
    "bundle_content_digest",
    "governed_authorization_template_sha256",
    "application_contract_sha256",
    "substrate_contract_sha256",
    "environment_contract_sha256",
    "runtime_environment_sha256",
    "runtime_contract_sha256",
    "input_policy_sha256",
    "action_policy_sha256",
    "network_policy_sha256",
    "identity_contract_sha256",
    "effect_contract_sha256",
    "operator_contract_sha256",
)


def _utc(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))


def _key_id(key: Ed25519PrivateKey | None = None) -> str:
    public = (
        (key or _private_key())
        .public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    return qualification_signer_key_id(public)


def _payload(**updates: object) -> QualificationAdmissionPayload:
    values: dict[str, object] = {
        **UUIDS,
        **{
            field: format(index + 1, "x") * 64
            for index, field in enumerate(DIGEST_FIELDS)
        },
        "campaign": QualificationCampaignBinding(
            artifact_sha256="b" * 64,
            contract_sha256="c" * 64,
            outcomes_sha256="d" * 64,
            oracle_id="qualification-oracle-v1",
            oracle_contract_sha256="e" * 64,
            tasks=(
                QualificationCondition(
                    task="create-record",
                    condition="healthy",
                    required_trials=3,
                    observed_trials=3,
                ),
                QualificationCondition(
                    task="create-record",
                    condition="wrong-identity",
                    required_trials=3,
                    observed_trials=3,
                ),
            ),
            failure_taxonomy=(
                "duplicate_effect",
                "over_halt",
                "silent_incorrect_success",
                "wrong_identity",
            ),
        ),
        "issuer": QualificationIssuer(
            key_id=_key_id(),
            workflow=(
                "OpenAdaptAI/openadapt-internal/.github/workflows/"
                "production-qualification-admission.yml"
            ),
            ref="refs/heads/main@0123456789abcdef0123456789abcdef01234567",
        ),
        "issued_at": _utc(NOW - timedelta(minutes=1)),
        "not_before": _utc(NOW - timedelta(minutes=1)),
        "expires_at": _utc(NOW + timedelta(days=29)),
    }
    values.update(updates)
    return QualificationAdmissionPayload.model_validate(values)


def _expected(payload: QualificationAdmissionPayload) -> QualificationAdmissionExpected:
    return QualificationAdmissionExpected.model_validate(
        {
            field: getattr(payload, field)
            for field in QualificationAdmissionExpected.model_fields
        }
    )


def _trust(key: Ed25519PrivateKey | None = None) -> dict[str, QualificationSignerTrust]:
    public = (
        (key or _private_key())
        .public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    return {
        _key_id(key): QualificationSignerTrust(
            public_key=b64encode(public).decode("ascii"),
            allowed_workflows=(
                "OpenAdaptAI/openadapt-internal/.github/workflows/"
                "production-qualification-admission.yml",
            ),
            allowed_ref_prefixes=("refs/heads/main@",),
        )
    }


def _interop_payload() -> QualificationAdmissionPayload:
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    key_id = qualification_signer_key_id(
        key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    return QualificationAdmissionPayload.model_validate(
        {
            "admission_id": "77777777-7777-4777-8777-777777777777",
            "tenant_id": "11111111-1111-4111-8111-111111111111",
            "workflow_id": "22222222-2222-4222-8222-222222222222",
            "workflow_version_id": "33333333-3333-4333-8333-333333333333",
            "bundle_version_id": "33333333-3333-4333-8333-333333333333",
            "runtime_validation_id": "66666666-6666-4666-8666-666666666666",
            "bundle_artifact_sha256": "1" * 64,
            "bundle_content_digest": "2" * 64,
            "governed_authorization_template_sha256": "3" * 64,
            "application_contract_sha256": "4" * 64,
            "substrate_contract_sha256": "5" * 64,
            "environment_contract_sha256": "6" * 64,
            "runtime_environment_sha256": "7" * 64,
            "runtime_contract_sha256": "8" * 64,
            "input_policy_sha256": "9" * 64,
            "action_policy_sha256": "a" * 64,
            "network_policy_sha256": "b" * 64,
            "identity_contract_sha256": "c" * 64,
            "effect_contract_sha256": "d" * 64,
            "operator_contract_sha256": "e" * 64,
            "campaign": {
                "artifact_sha256": "f" * 64,
                "contract_sha256": "0" * 64,
                "outcomes_sha256": "1" * 64,
                "oracle_id": "postcondition-oracle-v1",
                "oracle_contract_sha256": "2" * 64,
                "tasks": [
                    {
                        "task": "reference-write",
                        "condition": "standard",
                        "required_trials": 3,
                        "observed_trials": 3,
                    }
                ],
                "failure_taxonomy": ["over_halt", "silent_incorrect_success"],
                "decision": "admitted",
            },
            "issuer": {
                "key_id": key_id,
                "workflow": (
                    "OpenAdaptAI/openadapt-internal/.github/workflows/"
                    "production-qualification-admission.yml"
                ),
                "ref": "refs/heads/main@0123456789abcdef0123456789abcdef01234567",
            },
            "issued_at": "2030-01-01T00:00:00Z",
            "not_before": "2030-01-01T00:00:00Z",
            "expires_at": "2030-01-31T00:00:00Z",
        }
    )


def test_signed_admission_accepts_only_the_exact_live_contract() -> None:
    payload = _payload()
    envelope = sign_qualification_admission(payload, _private_key())

    digest = verify_qualification_admission(
        envelope,
        trusted_signers=_trust(),
        expected=_expected(payload),
        now=NOW,
    )

    assert digest == envelope.artifact_sha256()
    assert len(digest) == 64


def test_cloud_and_flow_share_one_canonical_admission_vector() -> None:
    envelope = sign_qualification_admission(
        _interop_payload(), Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    )
    assert envelope.signature == (
        "pBd6QoXnyUxqiHuanbnuJmen9/+2SjtQLCm7b3RK15tByYvgLLjKT9x1Ogr/"
        "c4dI1y+tYlDk/GiBTzq68B9kCg=="
    )
    assert envelope.artifact_sha256() == (
        "2e2bce1a8b63053c9a2d9d8c22cf6616fe091b59b693f6a9fe45cc91916e3aff"
    )


@pytest.mark.parametrize("field", QualificationAdmissionExpected.model_fields)
def test_every_changed_live_binding_refuses_before_actuation(field: str) -> None:
    payload = _payload()
    envelope = sign_qualification_admission(payload, _private_key())
    raw = _expected(payload).model_dump(mode="json")
    raw[field] = (
        "77777777-7777-4777-8777-777777777777" if field.endswith("_id") else "f" * 64
    )

    with pytest.raises(QualificationAdmissionError, match=field):
        verify_qualification_admission(
            envelope,
            trusted_signers=_trust(),
            expected=QualificationAdmissionExpected.model_validate(raw),
            now=NOW,
        )


def test_unknown_signer_invalid_signature_expiry_and_revocation_refuse() -> None:
    payload = _payload()
    envelope = sign_qualification_admission(payload, _private_key())
    expected = _expected(payload)

    with pytest.raises(QualificationAdmissionError, match="not trusted"):
        verify_qualification_admission(
            envelope, trusted_signers={}, expected=expected, now=NOW
        )

    envelope_with_wrong_signature = sign_qualification_admission(
        payload, Ed25519PrivateKey.generate()
    )
    with pytest.raises(QualificationAdmissionError, match="signature"):
        verify_qualification_admission(
            envelope_with_wrong_signature,
            trusted_signers=_trust(),
            expected=expected,
            now=NOW,
        )

    with pytest.raises(QualificationAdmissionError, match="expired"):
        verify_qualification_admission(
            envelope,
            trusted_signers=_trust(),
            expected=expected,
            now=NOW + timedelta(days=31),
        )

    with pytest.raises(QualificationAdmissionError, match="revoked"):
        verify_qualification_admission(
            envelope,
            trusted_signers=_trust(),
            expected=expected,
            revoked_admission_ids={payload.admission_id},
            now=NOW,
        )


def test_signature_cannot_move_to_a_repaired_bundle_or_new_workflow_version() -> None:
    original = _payload()
    signed = sign_qualification_admission(original, _private_key())
    repaired = original.model_copy(
        update={
            "workflow_version_id": "88888888-8888-4888-8888-888888888888",
            "bundle_version_id": "99999999-9999-4999-8999-999999999999",
            "bundle_content_digest": "a" * 64,
        }
    )

    with pytest.raises(QualificationAdmissionError, match="workflow_version_id"):
        verify_qualification_admission(
            signed,
            trusted_signers=_trust(),
            expected=_expected(repaired),
            now=NOW,
        )


def test_campaign_requires_three_trials_and_mandatory_failure_metrics() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 3"):
        QualificationCondition(
            task="task", condition="condition", required_trials=2, observed_trials=2
        )
    with pytest.raises(ValueError, match="silent incorrect success"):
        QualificationCampaignBinding(
            artifact_sha256="a" * 64,
            contract_sha256="b" * 64,
            outcomes_sha256="c" * 64,
            oracle_id="oracle",
            oracle_contract_sha256="d" * 64,
            tasks=(
                QualificationCondition(
                    task="task",
                    condition="condition",
                    required_trials=3,
                    observed_trials=3,
                ),
            ),
            failure_taxonomy=("duplicate_effect", "over_halt"),
        )


def test_unknown_fields_and_noncanonical_signature_refuse() -> None:
    payload = _payload()
    signed = sign_qualification_admission(payload, _private_key())
    raw = signed.model_dump(mode="json")
    raw["payload"]["label"] = "production ready"
    with pytest.raises(ValueError, match="extra"):
        QualificationAdmissionEnvelope.model_validate(raw)

    raw = signed.model_dump(mode="json")
    raw["signature"] = raw["signature"][:-2] + "A="
    with pytest.raises(ValueError, match="signature"):
        QualificationAdmissionEnvelope.model_validate(raw)


def test_validity_and_signer_identity_are_bounded() -> None:
    with pytest.raises(ValueError, match="independent from runtime validation"):
        _payload(admission_id=UUIDS["runtime_validation_id"])

    raw = _payload().model_dump(mode="json")
    raw["expires_at"] = _utc(NOW + timedelta(days=31))
    with pytest.raises(ValueError, match="30 days"):
        QualificationAdmissionPayload.model_validate(raw)

    raw = _payload().model_dump(mode="json")
    raw["issued_at"] = "2026-08-18T17:59:00.100Z"
    with pytest.raises(ValueError, match="canonical UTC"):
        QualificationAdmissionPayload.model_validate(raw)

    assert _key_id() == "qa-ed25519-65b60673d6ed884b"


# ---------------------------------------------------------------------------
# The actuation gate: every real Standard or Regulated action, managed OR local
# ---------------------------------------------------------------------------


def _governed_template(payload: QualificationAdmissionPayload):
    """A template stub carrying exactly the digests Flow recomputes locally."""

    return SimpleNamespace(
        template_sha256=payload.governed_authorization_template_sha256,
        qualification_environment_contract_sha256=payload.environment_contract_sha256,
        parameter_contract_sha256=payload.input_policy_sha256,
        qualification_project_contract_sha256=payload.action_policy_sha256,
        identity_contract_sha256=payload.identity_contract_sha256,
        qualified_effect_requirements=(),
    )


def _workflow_for(payload: QualificationAdmissionPayload, **overrides):
    template = _governed_template(payload)
    for name, value in overrides.items():
        setattr(template, name, value)
    return SimpleNamespace(
        manifest=SimpleNamespace(
            content_digest=payload.bundle_content_digest,
            provenance=SimpleNamespace(governed_authorization_template=template),
        )
    )


def _admission_bound_to_workflow():
    """A signed admission whose effect contract matches an empty requirement set."""

    payload = _payload(effect_contract_sha256=contract_sha256([]))
    return sign_qualification_admission(payload, _private_key()), payload


def test_actuation_accepts_an_admission_bound_to_this_exact_workflow() -> None:
    envelope, payload = _admission_bound_to_workflow()
    digest = verify_admission_for_actuation(
        envelope,
        _workflow_for(payload),
        trusted_signers=_trust(),
        now=NOW,
    )
    assert digest == envelope.artifact_sha256()


def test_actuation_refuses_an_admission_for_another_bundle_version() -> None:
    envelope, payload = _admission_bound_to_workflow()
    workflow = _workflow_for(payload)
    workflow.manifest.content_digest = "9" * 64
    with pytest.raises(QualificationAdmissionError, match="another bundle version"):
        verify_admission_for_actuation(
            envelope, workflow, trusted_signers=_trust(), now=NOW
        )


@pytest.mark.parametrize(
    "field",
    [
        "template_sha256",
        "qualification_environment_contract_sha256",
        "parameter_contract_sha256",
        "qualification_project_contract_sha256",
        "identity_contract_sha256",
    ],
)
def test_actuation_refuses_a_changed_governed_contract(field: str) -> None:
    envelope, payload = _admission_bound_to_workflow()
    # "f" * 64 is outside the fixture's digest range, so every parametrized
    # field really changes value.
    workflow = _workflow_for(payload, **{field: "f" * 64})
    with pytest.raises(
        QualificationAdmissionError, match="does not bind the current governed"
    ):
        verify_admission_for_actuation(
            envelope, workflow, trusted_signers=_trust(), now=NOW
        )


def test_actuation_refuses_a_changed_effect_contract() -> None:
    envelope, payload = _admission_bound_to_workflow()
    requirement = SimpleNamespace(model_dump=lambda mode="json": {"step_id": "s0"})
    workflow = _workflow_for(payload, qualified_effect_requirements=(requirement,))
    with pytest.raises(
        QualificationAdmissionError, match="does not bind the current governed"
    ):
        verify_admission_for_actuation(
            envelope, workflow, trusted_signers=_trust(), now=NOW
        )


def test_actuation_refuses_an_unsealed_bundle_or_ungoverned_template() -> None:
    envelope, payload = _admission_bound_to_workflow()
    unsealed = SimpleNamespace(manifest=None)
    with pytest.raises(QualificationAdmissionError, match="sealed manifest"):
        verify_admission_for_actuation(
            envelope, unsealed, trusted_signers=_trust(), now=NOW
        )
    ungoverned = _workflow_for(payload)
    ungoverned.manifest.provenance.governed_authorization_template = None
    with pytest.raises(QualificationAdmissionError, match="governed template"):
        verify_admission_for_actuation(
            envelope, ungoverned, trusted_signers=_trust(), now=NOW
        )


def test_actuation_refuses_an_expired_or_untrusted_admission() -> None:
    envelope, payload = _admission_bound_to_workflow()
    workflow = _workflow_for(payload)
    with pytest.raises(QualificationAdmissionError, match="has expired"):
        verify_admission_for_actuation(
            envelope,
            workflow,
            trusted_signers=_trust(),
            now=NOW + timedelta(days=30),
        )
    other_key = Ed25519PrivateKey.from_private_bytes(bytes(range(2, 34)))
    with pytest.raises(QualificationAdmissionError, match="signer is not trusted"):
        verify_admission_for_actuation(
            envelope,
            workflow,
            trusted_signers=_trust(other_key),
            now=NOW,
        )
    with pytest.raises(QualificationAdmissionError, match="is revoked"):
        verify_admission_for_actuation(
            envelope,
            workflow,
            trusted_signers=_trust(),
            revoked_admission_ids={payload.admission_id},
            now=NOW,
        )


# ---------------------------------------------------------------------------
# The CLI gate: a local Standard/Regulated run needs no control plane, but it
# does need a signed admission. Demo is the only unsigned actuating profile.
# ---------------------------------------------------------------------------


def _write_private(path, text: str):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_local_run_without_any_admission_is_refused(tmp_path, monkeypatch) -> None:
    import openadapt_flow.__main__ as cli

    monkeypatch.delenv(cli.QUALIFICATION_ADMISSION_ENV, raising=False)
    monkeypatch.delenv(cli.QUALIFICATION_SIGNERS_ENV, raising=False)
    monkeypatch.delenv(cli.QUALIFICATION_SIGNERS_JSON_ENV, raising=False)
    _envelope, payload = _admission_bound_to_workflow()
    refusal = cli._refuse_unqualified_actuation(_workflow_for(payload))
    assert refusal is not None
    assert "no signed qualification admission" in refusal
    assert "Nothing was executed." in refusal


def test_local_run_without_a_signer_registry_is_refused(tmp_path, monkeypatch) -> None:
    import openadapt_flow.__main__ as cli

    envelope, payload = _admission_bound_to_workflow()
    admission_file = _write_private(
        tmp_path / "admission.json", envelope.model_dump_json()
    )
    monkeypatch.setenv(cli.QUALIFICATION_ADMISSION_ENV, str(admission_file))
    monkeypatch.delenv(cli.QUALIFICATION_SIGNERS_ENV, raising=False)
    monkeypatch.delenv(cli.QUALIFICATION_SIGNERS_JSON_ENV, raising=False)
    refusal = cli._refuse_unqualified_actuation(_workflow_for(payload))
    assert refusal is not None
    assert "signer registry" in refusal


def test_local_run_with_an_offline_signer_registry_is_admitted(
    tmp_path, monkeypatch
) -> None:
    """An offline customer verifies locally and needs no control plane."""

    import json

    import openadapt_flow.__main__ as cli

    envelope, payload = _admission_bound_to_workflow()
    admission_file = _write_private(
        tmp_path / "admission.json", envelope.model_dump_json()
    )
    registry_file = _write_private(
        tmp_path / "signers.json",
        json.dumps(
            {key: value.model_dump(mode="json") for key, value in _trust().items()}
        ),
    )
    monkeypatch.setenv(cli.QUALIFICATION_ADMISSION_ENV, str(admission_file))
    monkeypatch.setenv(cli.QUALIFICATION_SIGNERS_ENV, str(registry_file))
    assert cli._refuse_unqualified_actuation(_workflow_for(payload)) is None


def test_local_run_refuses_an_admission_for_another_workflow(
    tmp_path, monkeypatch
) -> None:
    import json

    import openadapt_flow.__main__ as cli

    envelope, payload = _admission_bound_to_workflow()
    admission_file = _write_private(
        tmp_path / "admission.json", envelope.model_dump_json()
    )
    registry_file = _write_private(
        tmp_path / "signers.json",
        json.dumps(
            {key: value.model_dump(mode="json") for key, value in _trust().items()}
        ),
    )
    monkeypatch.setenv(cli.QUALIFICATION_ADMISSION_ENV, str(admission_file))
    monkeypatch.setenv(cli.QUALIFICATION_SIGNERS_ENV, str(registry_file))
    workflow = _workflow_for(payload)
    workflow.manifest.content_digest = "9" * 64
    refusal = cli._refuse_unqualified_actuation(workflow)
    assert refusal is not None
    assert "another bundle version" in refusal


def test_unreadable_admission_never_becomes_a_simulated_success(
    tmp_path, monkeypatch
) -> None:
    import openadapt_flow.__main__ as cli

    _envelope, payload = _admission_bound_to_workflow()
    broken = _write_private(tmp_path / "admission.json", "{not json")
    monkeypatch.setenv(cli.QUALIFICATION_ADMISSION_ENV, str(broken))
    monkeypatch.setenv(cli.QUALIFICATION_SIGNERS_JSON_ENV, "{}")
    refusal = cli._refuse_unqualified_actuation(_workflow_for(payload))
    assert refusal is not None
    assert "could not be read safely" in refusal
    # The refusal never echoes the rejected document.
    assert "not json" not in refusal


def test_missing_admission_file_is_refused(tmp_path, monkeypatch) -> None:
    import openadapt_flow.__main__ as cli

    _envelope, payload = _admission_bound_to_workflow()
    monkeypatch.setenv(cli.QUALIFICATION_ADMISSION_ENV, str(tmp_path / "absent.json"))
    monkeypatch.setenv(cli.QUALIFICATION_SIGNERS_JSON_ENV, "{}")
    refusal = cli._refuse_unqualified_actuation(_workflow_for(payload))
    assert refusal is not None
    assert "could not be read safely" in refusal


def test_only_a_production_profile_needs_the_admission() -> None:
    """Demo actuates unsigned; the report-only verbs never reach the gate."""

    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        requires_signed_qualification_admission,
    )

    assert requires_signed_qualification_admission(
        ExecutionProfile.STANDARD, will_actuate=True
    )
    assert requires_signed_qualification_admission(
        ExecutionProfile.REGULATED, will_actuate=True
    )
    assert not requires_signed_qualification_admission(
        ExecutionProfile.DEMO, will_actuate=True
    )
    assert not requires_signed_qualification_admission(
        ExecutionProfile.STANDARD, will_actuate=False
    )

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.production_qualification import (
    ProductionQualificationAuthorityError,
    ProductionQualificationAuthorityV4,
    ProductionQualificationGuard,
    _workflow_binding_refusal,
    load_production_qualification_authority,
)
from openadapt_flow.qualification_admission_v4 import (
    PINNED_KEY_ID,
    PINNED_REGISTRY_IDENTITY_SHA256,
    QualificationAdmissionV4Error,
    raw_object_sha256,
    validate_qualification_admission_v4,
    verify_pinned_signer_registry,
    verify_qualification_admission_v4,
)

FIXTURES = Path(__file__).parent / "fixtures" / "v4-qualification"
NOW = datetime(2026, 9, 2, 18, 30, tzinfo=timezone.utc)
ISSUED_BUNDLE_SHA256 = (
    "sha256:33f8c637f3d5d9eb23b92e0fb7ba74d2713a52f7c8ed11495b30f8bbefb1f312"
)


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def issued_admission() -> dict:
    return _load_fixture("qualification-admission.json")


def issued_registry() -> dict:
    return _load_fixture("signer-registry.json")


def issued_receipt() -> dict:
    return _load_fixture("decision-receipt.json")


def issued_revocation() -> dict:
    return _load_fixture("revocation-state.json")


def v4_authority_payload() -> dict:
    admission = issued_admission()
    registry = issued_registry()
    return {
        "schema_version": "openadapt.production-qualification-authority/v4",
        "qualification_admission": admission,
        "qualification_admission_sha256": raw_object_sha256(admission),
        "qualification_signer_registry": registry,
        "qualification_signer_registry_sha256": PINNED_REGISTRY_IDENTITY_SHA256[7:],
        "decision_receipt": issued_receipt(),
        "revocation_state": issued_revocation(),
    }


def _write_authority(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_issued_v4_admission_verifies_against_pinned_registry() -> None:
    verified = verify_qualification_admission_v4(
        issued_admission(),
        registry=issued_registry(),
        decision_receipt=issued_receipt(),
        revocation_state=issued_revocation(),
        expected_bundle_sha256=ISSUED_BUNDLE_SHA256,
        now=NOW,
    )
    assert verified.issuer_key_id == PINNED_KEY_ID
    assert verified.bundle_sha256 == ISSUED_BUNDLE_SHA256
    assert verified.registry_expires_at is None
    assert verified.evidence_class == "remote-safe-synthetic"


def test_until_revoked_expires_at_null_is_required() -> None:
    admission = issued_admission()
    admission["expires_at"] = "2026-10-01T00:00:00Z"
    with pytest.raises(QualificationAdmissionV4Error, match="until-revoked"):
        validate_qualification_admission_v4(admission, now=NOW)

    registry = issued_registry()
    registry["expires_at"] = "2026-10-01T00:00:00Z"
    with pytest.raises(QualificationAdmissionV4Error, match="until-revoked"):
        verify_pinned_signer_registry(registry)


def test_foreign_key_is_not_trusted() -> None:
    registry = issued_registry()
    registry["signers"] = [
        {**registry["signers"][0], "key_id": "qa-ed25519-0000000000000000"}
    ]
    with pytest.raises(QualificationAdmissionV4Error, match="pinned"):
        verify_pinned_signer_registry(registry)


def test_revoked_pinned_key_fails_closed() -> None:
    registry = issued_registry()
    inner = dict(registry["signers"][0])
    inner["status"] = "revoked"
    inner["revoked_at"] = "2026-09-02T18:24:25Z"
    registry["signers"] = [inner, registry["signers"][1]]
    with pytest.raises(QualificationAdmissionV4Error, match="pinned published registry"):
        verify_pinned_signer_registry(registry)


def test_wrong_bundle_digest_is_not_attached() -> None:
    with pytest.raises(QualificationAdmissionV4Error, match="does not bind"):
        verify_qualification_admission_v4(
            issued_admission(),
            registry=issued_registry(),
            decision_receipt=issued_receipt(),
            revocation_state=issued_revocation(),
            expected_bundle_sha256="sha256:" + ("ab" * 32),
            now=NOW,
        )


def test_tampered_receipt_signature_fails_closed() -> None:
    receipt = issued_receipt()
    receipt["signature"] = "A" * 86 + "=="
    with pytest.raises(QualificationAdmissionV4Error, match="signature"):
        verify_qualification_admission_v4(
            issued_admission(),
            registry=issued_registry(),
            decision_receipt=receipt,
            revocation_state=issued_revocation(),
            now=NOW,
        )


def test_unpinned_issuer_fails_closed() -> None:
    admission = issued_admission()
    admission["issuer"] = {
        **admission["issuer"],
        "workflow": ".github/workflows/other.yml",
    }
    with pytest.raises(QualificationAdmissionV4Error, match="pinned .github issuer"):
        validate_qualification_admission_v4(admission, now=NOW)


def test_v4_authority_binds_only_the_admitted_bundle(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    _write_authority(path, v4_authority_payload())
    authority = load_production_qualification_authority(path)
    assert isinstance(authority, ProductionQualificationAuthorityV4)

    matching = SimpleNamespace(
        manifest=SimpleNamespace(content_digest=ISSUED_BUNDLE_SHA256[7:]),
        qualification=None,
    )
    assert _workflow_binding_refusal(authority, matching) is None

    foreign = SimpleNamespace(
        manifest=SimpleNamespace(content_digest="ab" * 32),
        qualification=None,
    )
    assert _workflow_binding_refusal(authority, foreign) == (
        "Production qualification does not bind the sealed workflow contracts"
    )


def test_v4_guard_authorization_is_until_revoked(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    _write_authority(path, v4_authority_payload())
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=False)
    workflow = SimpleNamespace(
        manifest=SimpleNamespace(content_digest=ISSUED_BUNDLE_SHA256[7:]),
        qualification=None,
    )
    binding = guard.authorization_binding(workflow)
    assert str(binding["production_qualification_admission_id"]).startswith("sha256:")
    assert binding["production_qualification_signer_registry_expires_at"] is None
    assert (
        binding["production_qualification_signer_registry_sha256"]
        == (PINNED_REGISTRY_IDENTITY_SHA256[7:])
    )


def test_v4_guard_refuses_a_foreign_bundle(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    _write_authority(path, v4_authority_payload())
    guard = ProductionQualificationGuard(path, remote_permit_revalidation=True)
    workflow = SimpleNamespace(
        manifest=SimpleNamespace(content_digest="cd" * 32),
        qualification=None,
    )
    with pytest.raises(
        ProductionQualificationAuthorityError,
        match="does not bind the sealed workflow contracts",
    ):
        guard.verify(workflow, for_actuation=True)


def test_demo_does_not_require_signed_admission() -> None:
    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        requires_signed_qualification_admission,
    )

    assert not requires_signed_qualification_admission(
        ExecutionProfile.DEMO, will_actuate=True
    )


@pytest.mark.parametrize("profile", ["standard", "regulated"])
def test_cli_production_run_refuses_without_admission(
    tmp_path: Path, capsys, monkeypatch, profile: str
) -> None:
    from openadapt_flow.__main__ import main
    from openadapt_flow.ir import Workflow

    monkeypatch.chdir(tmp_path)
    bundle = tmp_path / "bundle"
    Workflow(name="unsigned", steps=[]).save(bundle)
    rc = main(
        [
            "run",
            str(bundle),
            "--profile",
            profile,
            "--backend",
            "web",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "signed qualification admission" in out
    assert "Nothing was executed." in out


def test_cli_standard_dry_run_does_not_require_admission(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from openadapt_flow.__main__ import main
    from openadapt_flow.ir import Workflow

    monkeypatch.chdir(tmp_path)
    bundle = tmp_path / "bundle"
    Workflow(name="report-only", steps=[]).save(bundle)
    rc = main(
        [
            "run",
            str(bundle),
            "--profile",
            "standard",
            "--backend",
            "web",
            "--dry-run",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    out = capsys.readouterr().out
    assert "signed qualification admission" not in out
    assert rc in {0, 2}


def test_cli_demo_run_does_not_require_admission(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from openadapt_flow.__main__ import main
    from openadapt_flow.ir import Workflow
    from tests.test_surface_selection import _install_fake_browser

    monkeypatch.chdir(tmp_path)
    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)
    bundle = tmp_path / "bundle"
    Workflow(name="demo", steps=[]).save(bundle)
    rc = main(
        [
            "run",
            str(bundle),
            "--profile",
            "demo",
            "--backend",
            "web",
            "--url",
            "http://app.example/",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    out = capsys.readouterr().out
    assert "signed qualification admission" not in out
    assert rc == 0
    assert "run" in captured


def test_matching_v4_admission_reaches_existing_run_gate(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from openadapt_flow.__main__ import main
    from openadapt_flow.ir import ActionKind, Step, Workflow
    from openadapt_flow.production_qualification import ProductionQualificationGuard

    monkeypatch.chdir(tmp_path)
    bundle = tmp_path / "bundle"
    Workflow(
        name="synthetic",
        steps=[
            Step(
                id="save",
                intent="save",
                action=ActionKind.CLICK,
                risk="irreversible",
            )
        ],
    ).save(bundle)
    authority = tmp_path / "authority.json"
    _write_authority(authority, v4_authority_payload())

    monkeypatch.setattr(
        ProductionQualificationGuard,
        "verify",
        lambda self, workflow, *, for_actuation: object(),
    )
    rc = main(
        [
            "run",
            str(bundle),
            "--profile",
            "standard",
            "--backend",
            "web",
            "--qualification-authority-file",
            str(authority),
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "signed qualification admission" not in out
    assert "Nothing was executed." in out
    assert "the Production qualification authority is invalid" not in out


def test_tutorial_standard_path_does_not_construct_a_production_guard(
    monkeypatch,
) -> None:
    from openadapt_flow.tutorial import TutorialError, run_tutorial_workflow

    constructed: list[object] = []

    class _Boom:
        def __init__(self, *args, **kwargs) -> None:
            constructed.append((args, kwargs))
            raise AssertionError("tutorial must not require a signed admission")

    monkeypatch.setattr(
        "openadapt_flow.production_qualification.ProductionQualificationGuard",
        _Boom,
    )
    monkeypatch.setattr(
        "openadapt_flow.tutorial._http_json", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        "openadapt_flow.run_gate.evaluate_run_gate",
        lambda *args, **kwargs: SimpleNamespace(
            passed=False,
            render=lambda: "refused in test",
        ),
    )
    with pytest.raises(TutorialError, match="REFUSED the tutorial bundle"):
        run_tutorial_workflow(
            base_url="http://127.0.0.1:9",
            workflow=SimpleNamespace(params={"note": "x"}),
            bundle_dir=Path("/tmp"),
            run_dir=Path("/tmp"),
        )
    assert constructed == []

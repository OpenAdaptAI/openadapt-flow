from __future__ import annotations

import hashlib
import json
import os
from base64 import b64encode, urlsafe_b64encode
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import openadapt_flow.runner.hosted_adapter as hosted
from openadapt_flow.ir import ParamKind, ParamSpec
from openadapt_flow.runner.hosted_adapter import (
    AdmissionArtifactBytes,
    DeliveryAuthority,
    HostedDispatch,
    HostedRunnerAdapter,
    ManagedExecution,
    RegisterCapabilities,
)
from openadapt_flow.runner.product_release import (
    DOMAIN,
    TARGETS,
    ProductReleaseAdmissionArtifact,
    ProductReleaseAdmissionError,
    ProductReleaseAdmissionPayload,
    ProductReleaseSignerTrust,
    verify_product_release_admission,
)
from openadapt_flow.runner.protocol import (
    DispatchParamsRef,
    RunnerDispatchPayload,
    dispatch_binding_sha256,
)
from openadapt_flow.runner.verify import VerifiedDispatch
from openadapt_flow.runtime.durable.authority import REMOTE_DISPATCH_SESSION_ID_ENV
from openadapt_flow.transaction import TransactionOutcome
from tests.test_runner_client_lib import dispatch_payload

pytest_plugins = ("tests.test_runner_client_lib",)


def _release_payload() -> dict[str, object]:
    targets = []
    for index, target in enumerate(TARGETS, start=1):
        targets.append(
            {
                "target": target,
                "admission_id": f"00000000-0000-4000-8000-{index:012d}",
                "admission_sha256": f"{index:x}" * 64,
                "release_id": "1.2.3",
                "release_artifact_sha256": f"{index + 7:x}" * 64,
                "admission_issued_at": "2026-08-25T00:00:00Z",
                "admission_expires_at": "2026-08-28T00:00:00Z",
                "revoked_at": None,
                "artifact_authority_sha256": f"{index + 8:x}" * 64,
                "artifact_authority_state": "active",
                "artifact_authority_checked_at": "2026-08-25T00:00:00Z",
                "artifact_authority_expires_at": "2026-08-28T00:00:00Z",
            }
        )
    return {
        "schema_version": "openadapt.product-release-admission-payload/v1",
        "set_id": "00000000-0000-4000-8000-000000000099",
        "sequence": 7,
        "policy_sha256": "a" * 64,
        "issued_at": "2026-08-25T00:00:00Z",
        "expires_at": "2026-08-28T00:00:00Z",
        "targets": tuple(targets),
    }


def _release_artifact() -> tuple[
    ProductReleaseAdmissionArtifact, ProductReleaseSignerTrust
]:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    payload = ProductReleaseAdmissionPayload.model_validate(_release_payload())
    signature = private_key.sign(DOMAIN + payload.canonical_bytes())
    public_b64 = b64encode(public_key).decode("ascii")
    artifact = ProductReleaseAdmissionArtifact.model_validate(
        {
            "schema_version": "openadapt.product-release-admission-artifact/v1",
            "payload": payload,
            "payload_sha256": payload.payload_sha256_value(),
            "signer": {
                "algorithm": "ed25519",
                "key_id": (
                    "release-admission-ed25519-"
                    + hashlib.sha256(public_key).hexdigest()[:16]
                ),
                "public_key": public_b64,
            },
            "signature": urlsafe_b64encode(signature).decode("ascii").rstrip("="),
        }
    )
    return artifact, ProductReleaseSignerTrust(
        public_key=public_b64,
        status="active",
        revoked_at=None,
    )


@pytest.mark.parametrize("sequence", [True, "7"])
def test_product_release_sequence_refuses_scalar_coercion(sequence: object) -> None:
    raw = _release_payload()
    raw["sequence"] = sequence
    with pytest.raises(ValueError):
        ProductReleaseAdmissionPayload.model_validate(raw)


def test_product_release_refuses_noncanonical_utc() -> None:
    raw = _release_payload()
    raw["issued_at"] = "2026-08-25T00:00:00+00:00"
    with pytest.raises(ValueError, match="canonical UTC"):
        ProductReleaseAdmissionPayload.model_validate(raw)


def test_product_release_refuses_revoked_signer() -> None:
    artifact, trust = _release_artifact()
    revoked = trust.model_copy(
        update={"status": "revoked", "revoked_at": "2026-08-25T00:00:00Z"}
    )
    with pytest.raises(ProductReleaseAdmissionError, match="revoked"):
        verify_product_release_admission(
            artifact,
            trusted_signers={artifact.signer.key_id: revoked},
            newest_sequence=7,
            now=datetime(2026, 8, 26, tzinfo=timezone.utc),
        )


def _hosted_dispatch(workflow) -> HostedDispatch:
    workflow_id = "33333333-3333-4333-8333-333333333333"
    version_id = "44444444-4444-4444-8444-444444444444"
    payload_raw = dispatch_payload(
        workflow,
        workflow_id=workflow_id,
        bundle={
            "version_id": version_id,
            "content_digest": workflow.manifest.content_digest,
            "url": "https://invalid.example/never-fetched",
        },
    )
    payload = RunnerDispatchPayload.model_validate(payload_raw)
    artifact_raw = b"{}"
    artifact = AdmissionArtifactBytes(
        artifact_bytes_base64=b64encode(artifact_raw).decode("ascii"),
        artifact_sha256=hashlib.sha256(artifact_raw).hexdigest(),
    )
    return HostedDispatch(
        schema_version="openadapt.hosted-runner/v1",
        dispatch_id="11111111-1111-4111-8111-111111111111",
        dispatch_session_id="12111111-1111-4111-8111-111111111111",
        tenant_id="22222222-2222-4222-8222-222222222222",
        runner_id="55555555-5555-4555-8555-555555555555",
        runner_session_id="66666666-6666-4666-8666-666666666666",
        run_id=payload.run_id,
        workflow_id=workflow_id,
        workflow_version_id=version_id,
        idempotency_key="hosted-dispatch-0001",
        lease_token="oal_" + "a" * 64,
        lease_expires_at="2099-01-01T00:00:00Z",
        product_release_admission=artifact,
        workflow_admission=artifact,
        managed_delivery_authority_url=(
            "https://cloud.example/api/internal/managed-delivery-permit"
        ),
        delivery_authority_token="b" * 64,
        payload=payload,
    )


def test_hosted_dispatch_accepts_lowercase_v8_run_id(sealed) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    run_id = "018f6c0a-4cce-8f47-8d71-c3d63bf1c001"
    payload = dispatch.payload.model_copy(
        update={
            "run_id": run_id,
            "dispatch_binding_sha256": dispatch_binding_sha256(
                run_id, dispatch.payload.authorization
            ),
        }
    )

    parsed = HostedDispatch.model_validate(
        dispatch.model_dump(mode="python") | {"run_id": run_id, "payload": payload}
    )

    assert parsed.run_id == run_id


def test_registration_refuses_without_protected_runner_origin(
    monkeypatch, tmp_path, config
) -> None:
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    monkeypatch.setattr(hosted, "load_runner_config", lambda *_args, **_kwargs: config)

    with pytest.raises(ValueError, match="protected runner host"):
        adapter.registration_request(
            runner_config=tmp_path / "runner.toml",
            name="runner",
            platform="linux",
            agent_version="1.0.0",
            engine_version="1.33.0",
            mode="service",
            capabilities=RegisterCapabilities(
                backends=("linux",),
                attended=False,
                effects_substrates=("linux",),
            ),
        )


def _prepared_adapter(monkeypatch, tmp_path, config, workflow, runner):
    config = replace(config, host="https://cloud.example")
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    dispatch = _hosted_dispatch(workflow)
    verified = VerifiedDispatch(
        payload=dispatch.payload,
        bundle=config.bundles[workflow.manifest.content_digest],
        profile_path=config.profiles["default"],
        params={"visit_date": "2026-07-01"},
        workflow=workflow,
        consequential_steps=1,
        effect_covered_consequential_steps=1,
    )
    monkeypatch.setattr(hosted, "load_runner_config", lambda _, **__: config)
    monkeypatch.setattr(adapter, "_verify_product_release", lambda *_: None)
    monkeypatch.setattr(adapter, "_load_evidence_private_key", lambda *_: object())
    monkeypatch.setattr(
        adapter,
        "_verify_workflow_admission",
        lambda *_, **__: ({}, b"runtime:\n  durable: false\n"),
    )
    monkeypatch.setattr(adapter, "_resolve_params", lambda *_: verified.params)
    monkeypatch.setattr(
        hosted,
        "verify_dispatch",
        lambda _payload, staged_config, **_kwargs: replace(
            verified,
            profile_path=staged_config.profiles[dispatch.payload.deployment_profile_id],
        ),
    )

    class Guard:
        def __init__(self, *_args, **_kwargs):
            pass

        def authorization_binding(self, _workflow):
            return {}

    monkeypatch.setattr(hosted, "ProductionQualificationGuard", Guard)
    return adapter, dispatch


@pytest.mark.parametrize(
    ("fault", "execution"),
    [
        (
            "backend_response_lost",
            RuntimeError("backend response lost after possible actuation"),
        ),
        (
            "delivery_acknowledgment_response_lost",
            RuntimeError("delivery acknowledgment response lost after backend call"),
        ),
        (
            "receipt_unavailable",
            ManagedExecution(returncode=1, report_bytes=None),
        ),
        (
            "malformed_terminal_report",
            ManagedExecution(returncode=0, report_bytes=b"not-json"),
        ),
    ],
    ids=(
        "backend-response-lost",
        "delivery-acknowledgment-response-lost",
        "receipt-unavailable",
        "malformed-terminal-report",
    ),
)
def test_hosted_uncertain_delivery_fault_never_replays(
    monkeypatch, tmp_path, config, sealed, fault, execution
) -> None:
    workflow, _ = sealed
    calls = 0
    seen_child_env = None
    seen_argv = None

    def runner(argv, _run_dir, child_env):
        nonlocal calls
        nonlocal seen_child_env
        nonlocal seen_argv
        calls += 1
        seen_child_env = child_env
        seen_argv = argv
        if isinstance(execution, Exception):
            raise execution
        return execution

    adapter, dispatch = _prepared_adapter(
        monkeypatch, tmp_path, config, workflow, runner
    )
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )
    first = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run",
        authority=authority,
    )
    second = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run-again",
        authority=authority,
    )

    assert first.outcome is TransactionOutcome.RECONCILIATION_REQUIRED
    assert first.started is True
    assert first.uncertain_delivery is True
    assert second.outcome is TransactionOutcome.RECONCILIATION_REQUIRED
    assert second.started is True
    assert second.uncertain_delivery is True
    assert calls == 1
    assert seen_child_env[REMOTE_DISPATCH_SESSION_ID_ENV] == (
        dispatch.dispatch_session_id
    )
    profile_path = Path(seen_argv[seen_argv.index("--config") + 1])
    assert profile_path == tmp_path / "run" / "deployment.yaml"
    assert profile_path.read_bytes() == b"runtime:\n  durable: false\n"
    if os.name != "nt":
        assert profile_path.stat().st_mode & 0o777 == 0o600
    assert fault in {
        "backend_response_lost",
        "delivery_acknowledgment_response_lost",
        "receipt_unavailable",
        "malformed_terminal_report",
    }


def test_params_reference_resolves_only_from_protected_local_root(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed
    workflow.param_specs = {
        "visit_date": ParamSpec(
            name="visit_date",
            type=ParamKind.DATE,
            required=True,
        )
    }
    dispatch = _hosted_dispatch(workflow)
    expected_digest = dispatch.payload.authorization.runtime_inputs_digest
    dispatch = dispatch.model_copy(
        update={
            "payload": dispatch.payload.model_copy(
                update={
                    "params": DispatchParamsRef(
                        ref="records/run.json",
                        expected_digest=expected_digest,
                    )
                }
            )
        }
    )
    root = tmp_path / "params"
    nested = root / "records"
    nested.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    nested.chmod(0o700)
    ref_file = nested / "run.json"
    ref_file.write_text(json.dumps({"visit_date": "2026-07-01"}), encoding="utf-8")
    ref_file.chmod(0o600)
    local_config = replace(
        config,
        host="https://cloud.example",
        params_ref_root=root,
    )
    monkeypatch.setattr(hosted.Workflow, "load", lambda *_: workflow)

    adapter = HostedRunnerAdapter(tmp_path / "resolver-ledger.sqlite")
    assert adapter._resolve_params(dispatch, local_config) == {
        "visit_date": "2026-07-01"
    }

    traversing = dispatch.model_copy(
        update={
            "payload": dispatch.payload.model_copy(
                update={
                    "params": DispatchParamsRef(
                        ref="../outside.json",
                        expected_digest=expected_digest,
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="safe local path"):
        adapter._resolve_params(traversing, local_config)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink contract")
def test_params_reference_refuses_symlink(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed
    workflow.param_specs = {
        "visit_date": ParamSpec(name="visit_date", type=ParamKind.DATE)
    }
    dispatch = _hosted_dispatch(workflow)
    expected_digest = dispatch.payload.authorization.runtime_inputs_digest
    root = tmp_path / "params"
    root.mkdir(mode=0o700)
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"visit_date": "2026-07-01"}), encoding="utf-8")
    target.chmod(0o600)
    (root / "run.json").symlink_to(target)
    dispatch = dispatch.model_copy(
        update={
            "payload": dispatch.payload.model_copy(
                update={
                    "params": DispatchParamsRef(
                        ref="run.json",
                        expected_digest=expected_digest,
                    )
                }
            )
        }
    )
    monkeypatch.setattr(hosted.Workflow, "load", lambda *_: workflow)
    adapter = HostedRunnerAdapter(tmp_path / "resolver-ledger.sqlite")

    with pytest.raises(ValueError, match="private regular file"):
        adapter._resolve_params(dispatch, replace(config, params_ref_root=root))


def test_params_reference_digest_mismatch_refuses_before_managed_runner(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed
    workflow.param_specs = {
        "visit_date": ParamSpec(
            name="visit_date",
            type=ParamKind.DATE,
            required=True,
        )
    }
    dispatch = _hosted_dispatch(workflow)
    root = tmp_path / "params"
    root.mkdir(mode=0o700)
    ref_file = root / "run.json"
    ref_file.write_text(json.dumps({"visit_date": "2026-07-01"}), encoding="utf-8")
    ref_file.chmod(0o600)
    dispatch = dispatch.model_copy(
        update={
            "payload": dispatch.payload.model_copy(
                update={
                    "params": DispatchParamsRef(
                        ref="run.json",
                        expected_digest="f" * 64,
                    )
                }
            )
        }
    )
    local_config = replace(
        config,
        host="https://cloud.example",
        params_ref_root=root,
    )
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("managed runner must not start")

    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    monkeypatch.setattr(
        hosted, "load_runner_config", lambda *_args, **_kwargs: local_config
    )
    monkeypatch.setattr(adapter, "_verify_product_release", lambda *_: None)
    monkeypatch.setattr(adapter, "_load_evidence_private_key", lambda *_: object())
    monkeypatch.setattr(
        adapter,
        "_verify_workflow_admission",
        lambda *_, **__: ({}, b"runtime:\n  durable: false\n"),
    )
    monkeypatch.setattr(hosted.Workflow, "load", lambda *_: workflow)
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    result = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run",
        authority=authority,
    )

    assert result.outcome == "REJECTED_POLICY"
    assert result.code == "runtime_inputs_mismatch"
    assert result.started is False
    assert result.uncertain_delivery is False
    assert calls == 0


@pytest.mark.parametrize("manifest_kind", ["missing", "malformed", "public", "symlink"])
def test_untrusted_runner_manifest_refuses_before_managed_runner(
    tmp_path, sealed, manifest_kind
) -> None:
    if manifest_kind == "symlink" and os.name == "nt":
        pytest.skip("POSIX symlink contract")
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    manifest = tmp_path / "runner.toml"
    if manifest_kind == "malformed":
        manifest.write_text("[runner\n", encoding="utf-8")
        manifest.chmod(0o600)
    elif manifest_kind == "public":
        manifest.write_text("[runner]\nname = 'runner'\n", encoding="utf-8")
        manifest.chmod(0o644)
    elif manifest_kind == "symlink":
        target = tmp_path / "target.toml"
        target.write_text("[runner]\nname = 'runner'\n", encoding="utf-8")
        target.chmod(0o600)
        manifest.symlink_to(target)
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("managed runner must not start")

    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    result = adapter.execute(
        dispatch,
        runner_config=manifest,
        run_dir=tmp_path / "run",
        authority=authority,
    )

    assert result.outcome == "REJECTED_POLICY"
    assert result.code == "hosted_admission_refused"
    assert result.detail.startswith("prestart_")
    assert result.started is False
    assert result.uncertain_delivery is False
    assert calls == 0


@pytest.mark.parametrize(
    "runner_host",
    [
        None,
        "http://cloud.example",
        "https://cloud.example/",
        "https://different.example",
    ],
)
def test_protected_runner_host_binds_delivery_authority_origin(
    monkeypatch, tmp_path, config, sealed, runner_host
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("managed runner must not start")

    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    local_config = replace(config, host=runner_host)
    monkeypatch.setattr(
        hosted, "load_runner_config", lambda *_args, **_kwargs: local_config
    )
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    result = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run",
        authority=authority,
    )

    assert result.outcome == "REJECTED_POLICY"
    assert result.started is False
    assert result.detail == "prestart_ValueError"
    assert calls == 0


def test_protected_profile_mutation_refuses_before_managed_runner(
    monkeypatch, tmp_path, config, sealed
) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    config = replace(config, host="https://cloud.example")
    profile_path = config.profiles["default"]
    profile_path.chmod(0o600)
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("managed runner must not start")

    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite", runner=runner)
    monkeypatch.setattr(hosted, "load_runner_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(adapter, "_verify_product_release", lambda *_: None)
    monkeypatch.setattr(adapter, "_load_evidence_private_key", lambda *_: object())

    def verify_profile(*_args, **_kwargs):
        raw = adapter._read_private_bytes(
            profile_path,
            maximum_bytes=1024 * 1024,
            label="hosted deployment profile",
        )
        return {}, raw

    monkeypatch.setattr(adapter, "_verify_workflow_admission", verify_profile)
    real_read = os.read
    changed = False

    def mutating_read(descriptor, count):
        nonlocal changed
        chunk = real_read(descriptor, count)
        if not changed:
            changed = True
            profile_path.write_bytes(chunk + b"# changed\n")
            profile_path.chmod(0o600)
        return chunk

    monkeypatch.setattr(hosted.os, "read", mutating_read)
    authority = DeliveryAuthority(
        dispatch.managed_delivery_authority_url,
        dispatch.delivery_authority_token,
    )

    result = adapter.execute(
        dispatch,
        runner_config=tmp_path / "runner.toml",
        run_dir=tmp_path / "run",
        authority=authority,
    )

    assert result.outcome == "REJECTED_POLICY"
    assert result.started is False
    assert result.detail == "prestart_ValueError"
    assert calls == 0


def test_parsed_refusal_callback_contains_closed_terminal(tmp_path, sealed) -> None:
    workflow, _ = sealed
    dispatch = _hosted_dispatch(workflow)
    adapter = HostedRunnerAdapter(tmp_path / "ledger.sqlite")
    refusal = adapter._refusal(dispatch, "hosted_admission_refused", "refused")

    callback = adapter.callback_request(dispatch, refusal)

    terminal = callback.events[-1]
    assert terminal["schema_version"] == "openadapt.hosted-runner-terminal/v1"
    assert terminal["outcome"] == "REJECTED_POLICY"
    assert terminal["started"] is False
    assert terminal["uncertain_delivery"] is False

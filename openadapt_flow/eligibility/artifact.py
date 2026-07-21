"""Atomic practice-local eligibility evidence artifacts.

Each logical check is one atomically promoted transaction directory containing
the exact raw response, the exact practice-facing normalized record, and a
hash manifest.  A CSV is a derived index, never the source of truth.  Repeating
the same ``operation_id`` with identical content is idempotent; reusing it with
different content fails loud.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, Literal, Mapping, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from openadapt_flow.eligibility.client import (
    EligibilityRequest,
    EligibilityResult,
    eligibility_request_sha256,
)
from openadapt_flow.runtime.effects.document_hash import DocumentHashVerifier
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectVerdict,
    ValueExpr,
    Verdict,
)

RESULTS_CSV = "eligibility_results.csv"
TRANSACTIONS_DIR = "transactions"
BOUNDARY_FILE = "boundary.json"
_ENCRYPTED_SUFFIX = ".enc"
_AES_PREFIX = b"OAE1"

_CSV_COLUMNS = [
    "checked_at",
    "payer",
    "payer_id",
    "member_id",
    "date_of_service",
    "network_code",
    "coverage_level_code",
    "time_qualifier_code",
    "procedure_code",
    "status",
    "plan_name",
    "copay",
    "coinsurance_percent",
    "deductible_total",
    "deductible_remaining",
    "out_of_pocket_total",
    "out_of_pocket_remaining",
    "service_type_codes",
    "source",
    "raw_271_sha256",
    "operation_id",
    "request_sha256",
]


class ArtifactEncryption(str, Enum):
    PLATFORM_VOLUME = "platform_volume"
    APPLICATION_AES256_GCM = "application_aes256_gcm"


class PracticeArtifactPolicy(BaseModel):
    """Explicit PHI storage, encryption, retention, and egress boundary."""

    boundary_id: str
    encryption: ArtifactEncryption
    retention_days: int = Field(ge=1, le=3650)
    egress: Literal["none"] = "none"
    phi_storage_allowed: bool = True
    volume_encryption_attested: bool = False
    encryption_key_env: Optional[str] = None

    @field_validator("boundary_id", "encryption_key_env")
    @classmethod
    def _safe_name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and (
            not value
            or len(value) > 128
            or any(
                ch
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
                for ch in value
            )
        ):
            raise ValueError("boundary identifiers must be non-PHI operational names")
        return value

    @model_validator(mode="after")
    def _encryption_contract(self) -> "PracticeArtifactPolicy":
        if not self.phi_storage_allowed:
            raise ValueError(
                "eligibility artifacts require an explicit PHI storage boundary"
            )
        if self.encryption is ArtifactEncryption.PLATFORM_VOLUME:
            if not self.volume_encryption_attested:
                raise ValueError("platform-volume mode requires encryption attestation")
            if self.encryption_key_env is not None:
                raise ValueError("platform-volume mode does not use an application key")
        elif not self.encryption_key_env:
            raise ValueError(
                "application encryption requires an environment key reference"
            )
        return self


class EligibilityArtifact(BaseModel):
    artifact_dir: str
    transaction_dir: str
    results_csv: str
    raw_271_file: str
    normalized_file: str
    raw_271_sha256: str
    normalized_sha256: str
    created: bool
    effects: list[Effect] = Field(default_factory=list)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _ensure_secure_dir(path: Path) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("artifact root must be a regular directory, not a link")
    else:
        path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    if stat.S_IMODE(path.lstat().st_mode) != 0o700:
        raise PermissionError("artifact root is not owner-only")


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short protected artifact write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("artifact is not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _policy_payload(policy: PracticeArtifactPolicy) -> bytes:
    return _canonical(
        {
            "schema_version": 1,
            "boundary_id": policy.boundary_id,
            "encryption": policy.encryption.value,
            "retention_days": policy.retention_days,
            "egress": policy.egress,
            "phi_storage_allowed": policy.phi_storage_allowed,
        }
    )


def _bind_policy(root: Path, policy: PracticeArtifactPolicy) -> None:
    expected = _policy_payload(policy)
    marker = root / BOUNDARY_FILE
    if marker.exists() or marker.is_symlink():
        if _read_regular(marker) != expected:
            raise ValueError("artifact root is bound to a different PHI policy")
        return
    _write_exclusive(marker, expected)
    _fsync_dir(root)


def _key(
    policy: PracticeArtifactPolicy, env: Optional[Mapping[str, str]]
) -> Optional[bytes]:
    if policy.encryption is ArtifactEncryption.PLATFORM_VOLUME:
        return None
    source = os.environ if env is None else env
    encoded = source.get(policy.encryption_key_env or "", "")
    try:
        key = base64.urlsafe_b64decode(encoded.encode())
    except Exception as exc:  # noqa: BLE001 - error is deliberately secret-free
        raise ValueError("application encryption key is not valid base64") from exc
    if len(key) != 32:
        raise ValueError("application encryption key must decode to 32 bytes")
    return key


def _protect(payload: bytes, *, key: Optional[bytes], aad: bytes) -> bytes:
    if key is None:
        return payload
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return _AES_PREFIX + nonce + AESGCM(key).encrypt(nonce, payload, aad)


def _unprotect(payload: bytes, *, key: Optional[bytes], aad: bytes) -> bytes:
    if key is None:
        return payload
    if len(payload) < 16 or payload[:4] != _AES_PREFIX:
        raise ValueError("encrypted artifact envelope is malformed")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key).decrypt(payload[4:16], payload[16:], aad)


@contextmanager
def _artifact_lock(root: Path) -> Iterator[None]:
    lock = root / ".eligibility-write.lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise BlockingIOError(
            "another eligibility artifact writer holds the lock"
        ) from exc
    try:
        yield
    finally:
        lock.rmdir()


def _transaction_id(policy: PracticeArtifactPolicy, operation_id: str) -> str:
    return hashlib.sha256(f"{policy.boundary_id}:{operation_id}".encode()).hexdigest()[
        :24
    ]


def _normalized_payload(
    result: EligibilityResult, *, request: EligibilityRequest
) -> dict[str, object]:
    selection = request.benefit_selection
    return {
        "schema_version": 2,
        "operation_id": result.operation_id,
        "checked_at": result.checked_at,
        "payer": result.payer_name or "",
        "payer_id": result.payer_id,
        "member_id": request.member_id or "",
        "date_of_service": request.date_of_service,
        "network_code": selection.network_code or "",
        "coverage_level_code": selection.coverage_level_code or "",
        "time_qualifier_code": selection.time_qualifier_code or "",
        "procedure_code": selection.procedure_code or "",
        "status": result.status.value,
        "plan_name": result.plan_name or "",
        "plan_begin": result.plan_begin or "",
        "plan_end": result.plan_end or "",
        "coverage_by_service": {
            k: v.value for k, v in result.coverage_by_service.items()
        },
        "benefits": [benefit.model_dump(mode="json") for benefit in result.benefits],
        "copay": result.copay or "",
        "coinsurance_percent": result.coinsurance_percent or "",
        "deductible_total": result.deductible_total or "",
        "deductible_remaining": result.deductible_remaining or "",
        "out_of_pocket_total": result.out_of_pocket_total or "",
        "out_of_pocket_remaining": result.out_of_pocket_remaining or "",
        "service_type_codes": list(result.service_type_codes),
        "source": result.source,
        "raw_271_sha256": result.raw_271_sha256,
        "request_sha256": result.request_sha256,
    }


def _safe_csv(value: object) -> str:
    text = str(value if value is not None else "")
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _csv_payload(rows: list[dict[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=_CSV_COLUMNS)
    writer.writeheader()
    for row in rows:
        csv_row: dict[str, str] = {}
        for column in _CSV_COLUMNS:
            value = row.get(column, "")
            if isinstance(value, list):
                value = " ".join(str(item) for item in value)
            csv_row[column] = _safe_csv(value)
        writer.writerow(csv_row)
    return stream.getvalue().encode()


def _atomic_replace(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError("refusing to replace a symlinked artifact index")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    _write_exclusive(temporary, payload)
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _effect(name: str, digest: str, probe: str) -> list[Effect]:
    match = {"name": ValueExpr(literal=name)}
    return [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match=match,
            expected_count=1,
            probe=f"exactly one {probe}",
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=match,
            field="sha256",
            value=ValueExpr(literal=digest),
            probe=f"{probe} storage bytes match the committed digest",
        ),
    ]


def _artifact_from_manifest(
    root: Path, tx_dir: Path, manifest: dict[str, object], *, created: bool
) -> EligibilityArtifact:
    raw_name = str(manifest["raw_file"])
    normalized_name = str(manifest["normalized_file"])
    effects = _effect(raw_name, str(manifest["raw_storage_sha256"]), "raw 271")
    effects += _effect(
        normalized_name,
        str(manifest["normalized_storage_sha256"]),
        "normalized eligibility record",
    )
    return EligibilityArtifact(
        artifact_dir=str(root),
        transaction_dir=str(tx_dir),
        results_csv=str(root / str(manifest["results_index"])),
        raw_271_file=str(tx_dir / raw_name),
        normalized_file=str(tx_dir / normalized_name),
        raw_271_sha256=str(manifest["raw_plain_sha256"]),
        normalized_sha256=str(manifest["normalized_plain_sha256"]),
        created=created,
        effects=effects,
    )


def _load_manifest(
    tx_dir: Path, policy: Optional[PracticeArtifactPolicy] = None
) -> dict[str, object]:
    raw = json.loads(_read_regular(tx_dir / "manifest.json"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        raise ValueError("eligibility transaction manifest is malformed")
    tx_id = tx_dir.name.removeprefix("tx_")
    suffix = (
        _ENCRYPTED_SUFFIX
        if policy is not None
        and policy.encryption is ArtifactEncryption.APPLICATION_AES256_GCM
        else ""
    )
    expected = {
        "raw_file": f"raw_271_{tx_id}.json{suffix}",
        "normalized_file": f"result_{tx_id}.json{suffix}",
        "results_index": RESULTS_CSV + suffix,
    }
    if not re.fullmatch(r"[0-9a-f]{24}", tx_id):
        raise ValueError("eligibility transaction directory name is malformed")
    if policy is not None and raw.get("boundary_id") != policy.boundary_id:
        raise ValueError("eligibility transaction escaped its PHI boundary")
    for field, value in expected.items():
        if raw.get(field) != value:
            raise ValueError(f"eligibility transaction {field} is malformed")
    for field in (
        "raw_plain_sha256",
        "raw_storage_sha256",
        "normalized_plain_sha256",
        "normalized_storage_sha256",
        "request_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(raw.get(field, ""))):
            raise ValueError(f"eligibility transaction {field} is malformed")
    if raw.get("egress") != "none":
        raise ValueError("eligibility transaction egress policy is malformed")
    return raw


def _rebuild_index(
    root: Path, policy: PracticeArtifactPolicy, key: Optional[bytes]
) -> str:
    rows: list[dict[str, object]] = []
    transactions = root / TRANSACTIONS_DIR
    for tx_dir in sorted(transactions.iterdir()):
        if not tx_dir.is_dir() or tx_dir.is_symlink():
            raise ValueError("transaction store contains a non-directory entry")
        manifest = _load_manifest(tx_dir, policy)
        raw_name = str(manifest["raw_file"])
        raw_stored = _read_regular(tx_dir / raw_name)
        if _sha(raw_stored) != manifest["raw_storage_sha256"]:
            raise ValueError("raw artifact fails its committed storage digest")
        raw_aad = f"{policy.boundary_id}:{tx_dir.name}:{raw_name}".encode()
        raw_plain = _unprotect(raw_stored, key=key, aad=raw_aad)
        if _sha(raw_plain) != manifest["raw_plain_sha256"]:
            raise ValueError("raw artifact fails its committed plaintext digest")
        normalized_name = str(manifest["normalized_file"])
        stored = _read_regular(tx_dir / normalized_name)
        if _sha(stored) != manifest["normalized_storage_sha256"]:
            raise ValueError("normalized artifact fails its committed storage digest")
        aad = f"{policy.boundary_id}:{tx_dir.name}:{normalized_name}".encode()
        plain = _unprotect(stored, key=key, aad=aad)
        if _sha(plain) != manifest["normalized_plain_sha256"]:
            raise ValueError("normalized artifact fails its committed digest")
        parsed = json.loads(plain)
        if not isinstance(parsed, dict):
            raise ValueError("normalized artifact is not an object")
        rows.append(parsed)
    plain_csv = _csv_payload(rows)
    name = RESULTS_CSV + (_ENCRYPTED_SUFFIX if key is not None else "")
    payload = _protect(
        plain_csv,
        key=key,
        aad=f"{policy.boundary_id}:index:{name}".encode(),
    )
    _atomic_replace(root / name, payload)
    return name


def write_eligibility_artifacts(
    result: EligibilityResult,
    artifact_dir: Union[str, Path],
    *,
    request: EligibilityRequest,
    policy: PracticeArtifactPolicy,
    env: Optional[Mapping[str, str]] = None,
) -> EligibilityArtifact:
    """Atomically promote a PHI-bearing raw+normalized transaction."""
    if not result.is_answer:
        raise ValueError(
            "only an exact unambiguous eligibility answer may be promoted as consumable"
        )
    request_sha256 = eligibility_request_sha256(request)
    if (
        result.operation_id != request.operation_id
        or result.payer_id != request.payer_id
        or result.service_type_codes != request.service_type_codes
        or result.request_sha256 != request_sha256
    ):
        raise ValueError("eligibility result is not bound to the supplied request")
    if result.raw_271_bytes is None or result.raw_271_sha256 is None:
        raise ValueError(
            "a raw response is required for a consumable eligibility artifact"
        )
    if _sha(result.raw_271_bytes) != result.raw_271_sha256:
        raise ValueError("raw eligibility bytes do not match the wire digest")
    root = Path(artifact_dir)
    _ensure_secure_dir(root)
    _bind_policy(root, policy)
    key = _key(policy, env)
    transactions = root / TRANSACTIONS_DIR
    _ensure_secure_dir(transactions)
    tx_id = _transaction_id(policy, result.operation_id)
    tx_dir = transactions / f"tx_{tx_id}"
    normalized_plain = _canonical(_normalized_payload(result, request=request))
    raw_plain = result.raw_271_bytes
    suffix = _ENCRYPTED_SUFFIX if key is not None else ""
    raw_name = f"raw_271_{tx_id}.json{suffix}"
    normalized_name = f"result_{tx_id}.json{suffix}"

    with _artifact_lock(root):
        if tx_dir.exists() or tx_dir.is_symlink():
            if tx_dir.is_symlink() or not tx_dir.is_dir():
                raise ValueError(
                    "idempotency target is not a regular transaction directory"
                )
            manifest = _load_manifest(tx_dir, policy)
            if (
                manifest.get("operation_id") != result.operation_id
                or manifest.get("request_sha256") != request_sha256
                or manifest.get("raw_plain_sha256") != _sha(raw_plain)
                or manifest.get("normalized_plain_sha256") != _sha(normalized_plain)
            ):
                raise FileExistsError(
                    "operation_id was already committed with different content"
                )
            _rebuild_index(root, policy, key)
            return _artifact_from_manifest(root, tx_dir, manifest, created=False)

        stage = transactions / f".staging-{uuid4().hex}"
        stage.mkdir(mode=0o700)
        try:
            raw_aad = f"{policy.boundary_id}:{tx_dir.name}:{raw_name}".encode()
            normalized_aad = (
                f"{policy.boundary_id}:{tx_dir.name}:{normalized_name}".encode()
            )
            raw_stored = _protect(raw_plain, key=key, aad=raw_aad)
            normalized_stored = _protect(normalized_plain, key=key, aad=normalized_aad)
            _write_exclusive(stage / raw_name, raw_stored)
            _write_exclusive(stage / normalized_name, normalized_stored)
            expiry = (
                datetime.now(timezone.utc) + timedelta(days=policy.retention_days)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            index_name = RESULTS_CSV + (_ENCRYPTED_SUFFIX if key is not None else "")
            committed_manifest: dict[str, object] = {
                "schema_version": 2,
                "operation_id": result.operation_id,
                "request_sha256": request_sha256,
                "boundary_id": policy.boundary_id,
                "retention_expires_at": expiry,
                "egress": "none",
                "raw_file": raw_name,
                "raw_plain_sha256": _sha(raw_plain),
                "raw_storage_sha256": _sha(raw_stored),
                "normalized_file": normalized_name,
                "normalized_plain_sha256": _sha(normalized_plain),
                "normalized_storage_sha256": _sha(normalized_stored),
                "results_index": index_name,
            }
            _write_exclusive(stage / "manifest.json", _canonical(committed_manifest))
            _fsync_dir(stage)
            os.rename(stage, tx_dir)
            _fsync_dir(transactions)
        except Exception:
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)
            raise
        _rebuild_index(root, policy, key)
        return _artifact_from_manifest(root, tx_dir, committed_manifest, created=True)


def write_and_verify(
    result: EligibilityResult,
    artifact_dir: Union[str, Path],
    *,
    request: EligibilityRequest,
    policy: PracticeArtifactPolicy,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[EligibilityArtifact, list[EffectVerdict]]:
    root = Path(artifact_dir)
    artifact = write_eligibility_artifacts(
        result,
        root,
        request=request,
        policy=policy,
        env=env,
    )
    # Re-open without following links before consulting the generic verifier.
    _read_regular(Path(artifact.raw_271_file))
    _read_regular(Path(artifact.normalized_file))
    verifier = DocumentHashVerifier(root, glob=f"{TRANSACTIONS_DIR}/tx_*/*")
    before = verifier.capture_pre_state()
    verdicts = [verifier.verify(effect, before) for effect in artifact.effects]
    if not all_confirmed(verdicts):
        raise RuntimeError("eligibility artifact effect verification did not confirm")
    return artifact, verdicts


def all_confirmed(verdicts: list[EffectVerdict]) -> bool:
    return bool(verdicts) and all(v.verdict is Verdict.CONFIRMED for v in verdicts)

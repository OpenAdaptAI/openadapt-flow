"""Public qualification-fault driver and signed mutation-receipt contract.

The runtime owns the detector.  A customer or application fixture owns the
fault driver.  The driver changes the *real* observation or verifier input at
one named pre-action gate; it cannot return a detector verdict.  The ordinary
resolver, identity, final-revalidation, or effect gate must then refuse the
changed input.

Only this protocol and its verification helpers are public.  Application-
specific drivers, datasets, and tuned fault recipes belong outside the public
engine (normally in the customer-controlled qualification environment).
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Literal, Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

QualificationFaultKind = Literal[
    "ambiguity",
    "wrong_identity",
    "stale_identity",
    "weak_effect",
    "missing_effect",
]
QualificationFaultGate = Literal[
    "target_resolution",
    "identity_verification",
    "actuation_revalidation",
    "effect_strength",
    "effect_verifier",
]


def expected_fault_detector(
    kind: QualificationFaultKind,
) -> tuple[QualificationFaultGate, str]:
    """Return the ordinary detector stage and stable refusal code for a fault."""

    detectors: dict[QualificationFaultKind, tuple[QualificationFaultGate, str]] = {
        "ambiguity": ("target_resolution", "target_ambiguous"),
        "wrong_identity": ("identity_verification", "identity_conflict"),
        "stale_identity": (
            "actuation_revalidation",
            "actuation_observation_changed",
        ),
        "weak_effect": ("effect_strength", "effect_strength_insufficient"),
        "missing_effect": ("effect_verifier", "effect_verifier_missing"),
    }
    return detectors[kind]


def fault_detector_contract_error(
    report: Any, receipt: "FaultMutationReceipt"
) -> str | None:
    """Require one ordinary detector refusal against the driver's changed input."""

    required_gate, required_code = expected_fault_detector(receipt.fault_kind)
    if receipt.gate != required_gate:
        return "fault_detector_gate_mismatch"
    matching_paths = getattr(report, "governed_qualification_case_action_paths", {})
    matching_step_ids = [
        step_id
        for step_id in matching_paths
        if sha256_bytes(step_id.encode("utf-8")) == receipt.step_id_sha256
    ]
    if (
        len(matching_step_ids) != 1
        or matching_paths.get(matching_step_ids[0]) != receipt.actuation_path
    ):
        return "fault_detector_actuation_path_mismatch"
    detector_refusals = [
        result
        for result in report.results
        if result.safety_refusal_evidence is not None
        and sha256_bytes(result.step_id.encode("utf-8")) == receipt.step_id_sha256
        and result.safety_refusal_evidence.stage == required_gate
        and result.safety_refusal_evidence.code == required_code
        and result.safety_refusal_evidence.detector_input_sha256
        == receipt.after_input_sha256
    ]
    if report.execution_outcome != "HALTED" or len(detector_refusals) != 1:
        return "fault_detector_refusal_not_observed"
    if any(
        not result.safety_halt or result.delivery_attempted is not False
        for result in detector_refusals
    ):
        return "fault_detector_delivery_boundary_crossed"
    refusal_index = report.results.index(detector_refusals[0])
    trailing_results = report.results[refusal_index + 1 :]
    if trailing_results and not (
        len(trailing_results) == 1
        and trailing_results[0].step_id == "<terminal>"
        and not trailing_results[0].ok
        and trailing_results[0].safety_halt
        and getattr(report, "terminal_outcome", None) in {"halt", "escalate"}
    ):
        return "fault_detector_refusal_not_terminal"
    return None


def sha256_bytes(value: bytes) -> str:
    """Return a lowercase SHA-256 digest for one exact input."""

    return hashlib.sha256(value).hexdigest()


def effect_verifier_input_sha256(
    verifier: Any,
    effects: Sequence[Any] = (),
) -> str:
    """Digest the verifier and its exact per-effect evidence-strength inputs."""

    from openadapt_flow.verification import verifier_effect_tier

    if verifier is None:
        payload: dict[str, object] = {"configured": False}
    else:
        effect_inputs = []
        for effect in effects:
            tier = verifier_effect_tier(verifier, effect)
            contract_hash = getattr(effect, "contract_hash", None)
            effect_inputs.append(
                {
                    "contract_sha256": (
                        contract_hash() if callable(contract_hash) else None
                    ),
                    "verification_tier": int(tier) if tier is not None else None,
                }
            )
        payload = {
            "configured": True,
            "implementation": (
                f"{type(verifier).__module__}.{type(verifier).__qualname__}"
            ),
            "effect_inputs": effect_inputs,
        }
    return sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


class FaultMutationReceipt(BaseModel):
    """PHI-free signed proof that a real gate input changed for one case."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.qualification-fault-mutation/v1"] = (
        "openadapt.qualification-fault-mutation/v1"
    )
    project_id: str = Field(min_length=1, max_length=128)
    project_revision: int = Field(ge=1)
    project_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    campaign_id_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_id_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    step_id_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    actuation_path: Literal["gui", "api"]
    fault_kind: QualificationFaultKind
    gate: QualificationFaultGate
    driver_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    driver_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    before_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    after_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mutation_artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attestation_key_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    attestation_signature: str = ""

    @model_validator(mode="after")
    def _input_changed(self) -> "FaultMutationReceipt":
        if self.before_input_sha256 == self.after_input_sha256:
            raise ValueError("fault mutation must change the detector input")
        expected_gate, _code = expected_fault_detector(self.fault_kind)
        if self.gate != expected_gate:
            raise ValueError("fault mutation gate does not match its fault kind")
        return self

    def payload_bytes(self) -> bytes:
        payload = self.model_dump(
            mode="json",
            exclude={"attestation_signature"},
        )
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def receipt_sha256(self) -> str:
        return sha256_bytes(self.artifact_bytes())

    def artifact_bytes(self) -> bytes:
        """Return the canonical exact signed artifact stored with the run."""

        return (
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


@dataclass(frozen=True)
class QualificationFaultContext:
    """Exact case binding and mutable local components available to a driver."""

    project_id: str
    project_revision: int
    project_contract_sha256: str
    campaign_id_sha256: str
    case_id_sha256: str
    case_input_sha256: str
    run_id_sha256: str
    step_id: str
    actuation_path: Literal["gui", "api"]
    fault_kind: QualificationFaultKind
    gate: QualificationFaultGate
    before_input_sha256: str
    backend: Any
    vision: Any
    effect_verifier: Any
    effects: tuple[Any, ...] = ()


@dataclass(frozen=True)
class QualificationFaultMutation:
    """Driver output after it changes the real environment-owned input."""

    receipt: FaultMutationReceipt
    replace_effect_verifier: bool = False
    effect_verifier: Any = None


@runtime_checkable
class QualificationFaultDriver(Protocol):
    """Environment-owned driver used only by an authorized qualification case."""

    @property
    def driver_id(self) -> str: ...

    @property
    def contract_sha256(self) -> str: ...

    @property
    def attestation_key_id(self) -> str: ...

    def mutate(
        self, context: QualificationFaultContext
    ) -> QualificationFaultMutation | None:
        """Change the selected real gate input, or decline this step."""


def sign_fault_mutation_receipt(
    receipt: FaultMutationReceipt,
    *,
    private_key: bytes,
) -> FaultMutationReceipt:
    """Sign one exact driver mutation receipt with Ed25519."""

    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(
        receipt.payload_bytes()
    )
    return receipt.model_copy(
        update={"attestation_signature": base64.b64encode(signature).decode("ascii")}
    )


def verify_fault_mutation_receipt(
    receipt: FaultMutationReceipt,
    *,
    trusted_public_key_base64: str,
) -> bool:
    """Verify one mutation receipt against an exact trusted driver key."""

    try:
        public_key = base64.b64decode(trusted_public_key_base64, validate=True)
        signature = base64.b64decode(receipt.attestation_signature, validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            receipt.payload_bytes(),
        )
    except (ValueError, InvalidSignature):
        return False
    return True


def load_qualification_fault_driver(name: str) -> QualificationFaultDriver:
    """Load one local driver from the public qualification-driver entry point."""

    matches = list(
        entry_points(group="openadapt_flow.qualification_fault_drivers", name=name)
    )
    if len(matches) != 1:
        raise ValueError("qualification fault driver is unavailable or ambiguous")
    loaded = matches[0].load()
    driver = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(driver, QualificationFaultDriver):
        raise ValueError("qualification fault driver does not implement the protocol")
    return driver

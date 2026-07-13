"""Independent effect verification against a system of record (not the screen).

The concrete runtime for the RFC ``Effect`` type
(``docs/design/WORKFLOW_PROGRAM_IR.md`` section 2.2). A screen/vision
postcondition cannot see a partial save, a phantom optimistic-UI success, a
duplicate submission, a lost update, or a double-delivered click -- the
fault-model study found 5 of 7 transactional fault classes silently mishandled
by screen verification. An :class:`EffectVerifier` reads the REAL system of
record (a FHIR/REST API, a document store) and rules CONFIRMED / REFUTED /
INDETERMINATE, halting (never guessing success) whenever it cannot certify the
write.

Public surface:

- Types: :class:`Effect`, :class:`EffectKind`, :class:`EffectState`,
  :class:`EffectVerdict`, :class:`Verdict`, :class:`EffectVerifier`.
- Verifiers: :class:`RestRecordVerifier` (JSON REST SoR, e.g. MockMed
  ``/api/db``), :class:`FhirEffectVerifier` (OpenEMR FHIR R4, primary),
  :class:`DocumentHashVerifier` (filesystem document store).
- Compensation: :func:`reconcile_or_escalate`, :class:`Compensator`,
  :class:`RestCompensator`, :class:`CompensationResult`.
"""

from openadapt_flow.runtime.effects.compensation import (  # noqa: F401
    CompensationAction,
    CompensationOutcome,
    CompensationResult,
    Compensator,
    RestCompensator,
    reconcile_or_escalate,
)
from openadapt_flow.runtime.effects.document_hash import (  # noqa: F401
    DocumentHashVerifier,
    sha256_file,
)
from openadapt_flow.runtime.effects.effect import (  # noqa: F401
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    EffectVerifier,
    Verdict,
    record_matches,
)
from openadapt_flow.runtime.effects.fhir import (  # noqa: F401
    DEFAULT_OBSERVATION_PATHS,
    FhirEffectVerifier,
    extract_path,
)
from openadapt_flow.runtime.effects.rest import (  # noqa: F401
    RestRecordVerifier,
)

__all__ = [
    "Effect",
    "EffectKind",
    "EffectState",
    "EffectVerdict",
    "EffectVerifier",
    "Verdict",
    "record_matches",
    "RestRecordVerifier",
    "FhirEffectVerifier",
    "DEFAULT_OBSERVATION_PATHS",
    "extract_path",
    "DocumentHashVerifier",
    "sha256_file",
    "reconcile_or_escalate",
    "Compensator",
    "RestCompensator",
    "CompensationResult",
    "CompensationAction",
    "CompensationOutcome",
]

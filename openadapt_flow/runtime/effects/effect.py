"""Typed effects and the ``EffectVerifier`` protocol -- verify REAL effects
against a system of record, not the screen.

This is the concrete runtime for the ``Effect`` type proposed in the
Workflow-Program IR RFC (``docs/design/WORKFLOW_PROGRAM_IR.md``, PR #61):
that RFC promotes today's vision-only :class:`openadapt_flow.ir.Postcondition`
into a *typed* ``Effect`` with **system-of-record** kinds
(``record_written`` / ``field_equals``) whose probe is backend-specific (an
API / DB read), closing the transactional gap the fault-model study measured
(``docs/LIMITS.md`` "5 of 7 write faults silent"; ``benchmark/fault_model/``).

Why not the screen? A vision postcondition answers *"do the pixels look like a
save happened?"* -- which a partial save, an optimistic-UI-then-reject, a
duplicate submission, a lost update, or a double-delivered click all satisfy
while the **record** is wrong or missing. An ``EffectVerifier`` answers the
only question a record system may trust: *"is the intended record actually in
the system of record, exactly once, with the right field values?"*

Design posture -- mirror the identity gate (``runtime.identity``): **fail safe
to HALT, refuse rather than guess.** A verdict is one of

- :attr:`Verdict.CONFIRMED`     -- the effect is present and correct -> proceed;
- :attr:`Verdict.REFUTED`       -- the system of record affirmatively
  contradicts the effect (missing / duplicated / wrong value / collateral
  loss) -> HALT (never accept as success);
- :attr:`Verdict.INDETERMINATE` -- the system of record is unreachable or
  unreadable, so the effect *cannot be certified* -> HALT (never assume
  success). Consistent with the identity ladder's ``unreadable`` -> refuse.

Both non-confirmed verdicts set :attr:`EffectVerdict.should_halt`. There is no
"probably fine": an unverifiable consequential write halts, exactly as an
unreadable identity band does.

Everything here is import-light (pydantic only); concrete substrates
(``rest`` / ``fhir`` / ``document_hash``) bring their own I/O deps lazily.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class EffectKind(str, Enum):
    """System-of-record effect kinds (RFC section 2.2).

    These are the NEW kinds the RFC adds beyond the vision-checkable
    postcondition kinds (``text_present`` / ``region_stable`` / ...): their
    probe is a read against the system of record, not the frame.
    """

    #: A record matching :attr:`Effect.match` must exist in the system of
    #: record EXACTLY :attr:`Effect.expected_count` times (default once --
    #: at-most-once for a consequential write; catches missing, phantom,
    #: duplicate, and double-click writes).
    RECORD_WRITTEN = "record_written"
    #: The record matching :attr:`Effect.match` must carry
    #: ``field == value`` (read-back of a specific field; catches partial
    #: saves that persist the row but drop a field).
    FIELD_EQUALS = "field_equals"


class Effect(BaseModel):
    """A typed system-of-record effect (RFC ``Effect``, concrete runtime form).

    An ``Effect`` is a *contract*: what must be true of the system of record
    for a step to have actually succeeded. It is deliberately substrate-neutral
    -- the SAME ``Effect`` is checked by the REST verifier, the FHIR verifier,
    or the filesystem verifier (see :class:`EffectVerifier`). The verifier maps
    :attr:`match` / :attr:`field` onto its substrate's query language.

    Binds to the RFC (``docs/design/WORKFLOW_PROGRAM_IR.md`` section 2.2): the
    RFC's ``Effect(kind="record_written", probe="encounter exists for
    patient")`` is realized here with a machine-checkable :attr:`match`
    selector and an :attr:`expected_count`; the RFC's
    ``Effect(kind="field_equals", field="note", value=params.note)`` maps to
    :attr:`field` / :attr:`value`.
    """

    kind: EffectKind
    #: Field -> value selector identifying the INTENDED record in the system
    #: of record (e.g. ``{"patient_id": "p1", "type": "Triage"}``). A verifier
    #: matches a record when every selector pair is satisfied. Values compare
    #: as strings after ``str()`` so numeric ids match across JSON/DB types.
    match: dict[str, str] = Field(default_factory=dict)
    #: ``field_equals`` only: the record field that must equal :attr:`value`.
    field: Optional[str] = None
    #: ``field_equals`` only: the required value of :attr:`field`.
    value: Optional[str] = None
    #: ``record_written`` only: how many matching records must exist. 1 is the
    #: at-most-once contract for a consequential write; 0 asserts absence.
    expected_count: int = 1
    #: Optional idempotency / at-most-once key. When set, ``record_written``
    #: counts records bearing THIS key (via :attr:`key_field`) and requires
    #: exactly :attr:`expected_count` -- so a duplicate submission that reused
    #: the key collapses to one record and a non-idempotent duplicate (no key,
    #: or a second distinct write) is caught as ``observed_count > expected``.
    idempotency_key: Optional[str] = None
    #: Which record field carries the idempotency key (substrate-specific;
    #: the MockMed system-of-record uses ``"key"``).
    key_field: str = "key"
    #: ``record_written`` only: also REFUTE when a record that existed in the
    #: pre-state (``before``) and does NOT match :attr:`match` has since
    #: vanished -- collateral loss. This is what catches a stale / lost-update
    #: (last-write-wins) fault: our row lands (count 1, looks fine) while a
    #: concurrent actor's row was silently destroyed.
    forbid_collateral_loss: bool = True
    #: Consequential-write flag (mirrors ``Step.risk`` / RFC ``State.risk``).
    #: Compensation (``effects.compensation``) only fires for irreversible
    #: effects; a reversible effect just halts.
    risk: str = "reversible"
    #: Human-readable probe description (mirrors the RFC's illustrative
    #: ``probe="encounter exists for patient"``); for audit/logging only.
    probe: Optional[str] = None
    #: How long a verifier may poll the system of record before ruling
    #: INDETERMINATE (the SoR write may lag the GUI paint).
    timeout_s: float = 5.0
    #: Set by the compiler's effect miner
    #: (``compiler.effect_mining``) on a PLACEHOLDER effect: the step is a
    #: consequential write, but its system-of-record binding (which API /
    #: record / idempotency key) was NOT derivable from the demonstration —
    #: it is "irreducibly app-specific" (RFC ``WORKFLOW_PROGRAM_IR.md`` §7).
    #: The miner refuses to INVENT an endpoint, so :attr:`match` here is a
    #: sentinel, not a real selector. Such an effect must never be silently
    #: trusted: a run treats it as fail-safe (the replayer HALTs rather than
    #: verify a fabricated binding — see ``runtime.replayer._verify_effects``)
    #: until an operator completes the binding and clears this flag.
    needs_operator_confirmation: bool = False


class EffectState(BaseModel):
    """A snapshot of the system of record captured BEFORE the action.

    Two jobs: (1) establish a baseline so ``record_written`` can count only
    records that appeared *because of this action* (delta / at-most-once),
    and (2) let :attr:`Effect.forbid_collateral_loss` detect a pre-existing
    record that the action destroyed (lost update). :attr:`reachable` is
    False when the system of record could not be read at capture time -- the
    verifier factors that into an INDETERMINATE verdict rather than assuming
    an empty baseline.
    """

    substrate: str
    reachable: bool
    #: The system-of-record records present at capture time. Each is a plain
    #: dict in the substrate's native shape (a FaultDB row, a FHIR resource, a
    #: filesystem descriptor). Empty when unreachable.
    records: list[dict[str, Any]] = Field(default_factory=list)
    captured_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    detail: dict[str, Any] = Field(default_factory=dict)


class Verdict(str, Enum):
    """The three-valued effect verdict (fail-safe to HALT)."""

    CONFIRMED = "confirmed"  # effect present and correct -> proceed
    REFUTED = "refuted"  # SoR contradicts the effect -> HALT
    INDETERMINATE = "indeterminate"  # SoR unreachable/unreadable -> HALT


class EffectVerdict(BaseModel):
    """Outcome of verifying one :class:`Effect` against a system of record."""

    verdict: Verdict
    kind: EffectKind
    substrate: str = ""
    reason: str = ""
    observed_count: Optional[int] = None
    expected_count: Optional[int] = None
    observed_value: Optional[str] = None
    expected_value: Optional[str] = None
    #: Records the verifier judged to MATCH :attr:`Effect.match` (native
    #: shape), so a caller / compensator can act on them (e.g. delete the
    #: extras of a duplicate).
    matched_records: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def should_halt(self) -> bool:
        """True unless the effect was CONFIRMED -- a REFUTED or INDETERMINATE
        consequential effect must halt the run (never proceed on an
        unverified or contradicted write)."""
        return self.verdict is not Verdict.CONFIRMED

    @property
    def confirmed(self) -> bool:
        return self.verdict is Verdict.CONFIRMED


@runtime_checkable
class EffectVerifier(Protocol):
    """Independent verification of a typed :class:`Effect` against a REAL
    system of record (an API / DB / filesystem), never the screen.

    A verifier is bound to one substrate (an OpenEMR FHIR endpoint, a REST
    system of record, a document store) and knows how to (1) snapshot the
    system of record before the action and (2) decide whether the intended
    effect actually landed. The same :class:`Effect` contract is honored by
    every substrate -- that substrate-agnosticism is the point (an
    OpenEMR-shaped protocol would just overfit MockMed's replacement).

    Contract for implementers:

    - :meth:`verify` must return :attr:`Verdict.INDETERMINATE` (never raise,
      never guess CONFIRMED) when the system of record is unreachable or its
      response is unusable -- the run then HALTs, consistent with the identity
      gate's refuse-rather-than-guess posture.
    - :meth:`verify` returns :attr:`Verdict.REFUTED` only on AFFIRMATIVE
      contradiction (wrong count, wrong value, collateral loss) -- a positive
      signal the effect is bad, distinct from "could not tell".
    """

    #: Stable substrate name for audit (``"rest"`` / ``"fhir"`` / ``"fs"``).
    substrate: str

    def capture_pre_state(self, context: Any = None) -> EffectState:
        """Snapshot the system of record before the action runs."""
        ...

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        """Decide whether ``expected`` actually landed in the system of
        record, given the pre-action snapshot ``before``."""
        ...


# -- shared matching helpers (used by every substrate) -----------------------


def record_matches(record: dict[str, Any], selector: dict[str, str]) -> bool:
    """Whether ``record`` satisfies every ``field == value`` in ``selector``.

    Comparison is on ``str()`` of both sides so a numeric id in JSON (``1``)
    matches a string selector (``"1"``) without the caller tracking types.
    An empty selector matches every record (used by absence checks).
    """
    for key, want in selector.items():
        if str(record.get(key, None)) != str(want):
            return False
    return True


def stable_id(record: dict[str, Any]) -> Any:
    """A record's identity for pre/post delta accounting.

    Prefers an ``id`` field; falls back to the whole record's sorted-items
    tuple so substrates without an explicit id still support collateral-loss
    detection.
    """
    if "id" in record:
        return ("id", record["id"])
    return tuple(sorted((k, str(v)) for k, v in record.items()))

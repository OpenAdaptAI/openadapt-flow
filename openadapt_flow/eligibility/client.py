"""Stedi real-time 270/271 eligibility client (the waterfall's API tier).

Documented contract this client is built against (Stedi Healthcare API,
fetched 2026-07-18 -- ``docs/ELIGIBILITY_API_WATERFALL.md`` records the
research honestly):

- Endpoint: ``POST https://healthcare.us.stedi.com/2024-04-01/change/medicalnetwork/eligibility/v3``
- Auth: ``Authorization: Key <api_key>`` (a TEST-mode key unlocks Stedi's
  free mock catalog, including a dedicated dental section -- service type
  code ``35`` -- with mock payers Ameritas / Anthem / Cigna / MetLife / UHC
  Dental; no signed contract required for test mode).
- Request: ``tradingPartnerServiceId`` (payer ID), ``provider`` (NPI +
  org or person name), ``subscriber`` (name / DOB ``YYYYMMDD`` / member ID),
  ``encounter.serviceTypeCodes`` (``["35"]`` = Dental Care; defaults to
  ``30`` when omitted).
- Response: ``planStatus`` / ``benefitsInformation[]`` (code ``1`` active,
  ``6`` inactive, ``A`` co-insurance ``benefitPercent``, ``B`` co-payment
  ``benefitAmount``, ``C`` deductible, ``G`` out-of-pocket max),
  ``planInformation``, ``errors[]`` carrying X12 AAA reject codes
  (``42``/``79``/``80`` payer unavailable, ``72``/``73``/``75``/``76``
  subscriber not found / bad identifying info), and the raw 271 X12.

Posture:

- **Fail-closed parsing.** A response that neither affirms active coverage
  nor affirms inactive/error is :attr:`EligibilityStatus.INDETERMINATE` --
  the check is never *guessed* into a benefits row. This mirrors the effect
  verifier's refuse-rather-than-guess verdicts.
- **Secret-isolated auth.** The API key is referenced by environment
  variable name (``STEDI_API_KEY`` by default, or any
  :class:`~openadapt_flow.runtime.effects.auth.AuthRef`), resolved at
  construction, failing LOUD when absent -- the kit-wide convention
  (``docs/EFFECT_KIT.md``). The key never appears in configs, results,
  reason strings, or artifacts.
- **Reads are idempotent.** A 270 inquiry writes nothing, so -- unlike the
  write-path :class:`~openadapt_flow.runtime.actuators.api.ApiActuator`,
  whose sent-but-unacknowledged requests must HALT to avoid double writes --
  a failed or unavailable API check may safely fall through to the portal
  tier. The waterfall (:mod:`.waterfall`) encodes that distinction.
- **PHI stays local.** The request carries member ID / DOB to the
  clearinghouse under the *practice's own* Stedi account and BAA, from the
  practice's machine. Nothing here logs request params; reason strings carry
  payer IDs and status codes only.

Import-light: ``httpx`` (already a core dependency) is imported lazily so
importing this module costs nothing on the replay hot path.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.runtime.effects.auth import AuthRef

#: Stedi's real-time 270/271 JSON endpoint (test and production mode are
#: selected by the API key, not the URL).
STEDI_ELIGIBILITY_URL = (
    "https://healthcare.us.stedi.com/2024-04-01/change/medicalnetwork/eligibility/v3"
)

#: X12 service type code for Dental Care -- the dental offer's default EQ.
SERVICE_TYPE_DENTAL = "35"

#: Default env var holding the practice's Stedi API key (test or production).
STEDI_API_KEY_ENV = "STEDI_API_KEY"

#: AAA reject codes meaning the PAYER could not answer right now (retry /
#: portal fallback is appropriate; the member may be perfectly eligible).
_AAA_PAYER_UNAVAILABLE = {"42", "79", "80"}
#: AAA reject codes meaning the payer answered but could not FIND the member
#: as identified (wrong/missing member ID, name, DOB -- a data problem).
_AAA_NOT_FOUND = {"72", "73", "75", "76"}

#: benefitsInformation codes affirming coverage state.
_CODE_ACTIVE = "1"
_CODES_INACTIVE = {"6", "7", "8"}  # inactive / pending / terminated


class EligibilityStatus(str, Enum):
    """Normalized outcome of one 270/271 eligibility check."""

    #: Payer affirmed active coverage for the requested service type.
    ACTIVE = "active"
    #: Payer affirmed the coverage is inactive / terminated.
    INACTIVE = "inactive"
    #: Payer answered but could not find the member as identified
    #: (AAA 72/73/75/76) -- fix the identifying fields or fall to the portal.
    NOT_FOUND = "not_found"
    #: The payer (or its clearinghouse leg) could not respond right now
    #: (AAA 42/79/80, transport failure, payer not supported) -- the member's
    #: real status is unknown; retry or fall through to the portal tier.
    PAYER_UNAVAILABLE = "payer_unavailable"
    #: The payer rejected the REQUEST itself (e.g. AAA 43 invalid provider
    #: identification) -- an enrollment / request-shape problem to fix, not
    #: a member status.
    REJECTED = "rejected"
    #: The response could not be interpreted (malformed JSON, no affirmative
    #: coverage or error signal). FAIL CLOSED: never recorded as a benefits
    #: answer, never guessed into "active".
    INDETERMINATE = "indeterminate"


class EligibilityRequest(BaseModel):
    """One 270 inquiry: who is asking (provider) about whom (subscriber).

    Field formats follow the documented Stedi request shape; ``date_of_birth``
    is X12 ``D8`` (``YYYYMMDD``).
    """

    #: The payer's ID with the clearinghouse (Stedi ``tradingPartnerServiceId``;
    #: resolve the exact value in Stedi's payer directory at enrollment).
    payer_id: str
    member_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    #: ``YYYYMMDD``.
    date_of_birth: Optional[str] = None
    #: The practice's (rendering/billing) NPI -- the requestor.
    provider_npi: str
    #: Organization name; exactly one of this or the person name pair below.
    provider_organization: Optional[str] = None
    provider_first_name: Optional[str] = None
    provider_last_name: Optional[str] = None
    #: X12 EQ service type codes; ``["35"]`` = Dental Care.
    service_type_codes: list[str] = Field(default_factory=lambda: [SERVICE_TYPE_DENTAL])

    def to_stedi_body(self) -> dict[str, Any]:
        """The documented Stedi JSON request body for this inquiry."""
        provider: dict[str, Any] = {"npi": self.provider_npi}
        if self.provider_organization:
            provider["organizationName"] = self.provider_organization
        if self.provider_first_name:
            provider["firstName"] = self.provider_first_name
        if self.provider_last_name:
            provider["lastName"] = self.provider_last_name
        subscriber: dict[str, Any] = {}
        for key, value in (
            ("firstName", self.first_name),
            ("lastName", self.last_name),
            ("dateOfBirth", self.date_of_birth),
            ("memberId", self.member_id),
        ):
            if value:
                subscriber[key] = value
        return {
            "tradingPartnerServiceId": self.payer_id,
            "provider": provider,
            "subscriber": subscriber,
            "encounter": {"serviceTypeCodes": list(self.service_type_codes)},
        }


class EligibilityResult(BaseModel):
    """Normalized 271 outcome, with the raw response retained for audit.

    The normalized fields are the reliable head of the 271 (active/inactive,
    plan name, copay / co-insurance / deductible / out-of-pocket max where the
    payer returned them); everything else stays available in :attr:`raw_271`.
    :attr:`raw_271_sha256` is the digest of the EXACT response bytes -- the
    same digest the document-hash effect verifier checks after
    :func:`~openadapt_flow.eligibility.artifact.write_eligibility_artifacts`
    writes those bytes into the results artifact set, giving one auditable
    chain from wire to system-of-record document.
    """

    model_config = ConfigDict(protected_namespaces=())

    status: EligibilityStatus
    payer_id: str
    payer_name: Optional[str] = None
    plan_name: Optional[str] = None
    plan_begin: Optional[str] = None
    plan_end: Optional[str] = None
    #: Dollar amounts / percents are kept as the payer's exact strings --
    #: benefits data is transcribed, never arithmetically reinterpreted.
    copay: Optional[str] = None
    coinsurance_percent: Optional[str] = None
    deductible: Optional[str] = None
    out_of_pocket_maximum: Optional[str] = None
    service_type_codes: list[str] = Field(default_factory=list)
    #: X12 AAA reject codes present in the response (empty when none).
    aaa_codes: list[str] = Field(default_factory=list)
    #: Human-readable error descriptions from the payer/clearinghouse.
    errors: list[str] = Field(default_factory=list)
    #: Why the status was assigned (payer IDs / codes only -- no PHI).
    reason: str = ""
    #: The parsed 271 response body (None when the body was not JSON).
    raw_271: Optional[dict[str, Any]] = None
    #: SHA-256 hex digest of the exact response bytes.
    raw_271_sha256: Optional[str] = None
    #: The exact response bytes (written verbatim into the artifact set so
    #: the digest chain holds; excluded from ``model_dump`` serialization).
    raw_271_bytes: Optional[bytes] = Field(default=None, exclude=True, repr=False)
    checked_at: str = ""
    source: str = "stedi"

    @property
    def is_answer(self) -> bool:
        """Whether the payer AFFIRMED a coverage state (active or inactive).

        Every other status means the check produced no benefits answer and
        the waterfall may fall through to the portal tier.
        """
        return self.status in (EligibilityStatus.ACTIVE, EligibilityStatus.INACTIVE)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first(items: list[Any]) -> Optional[dict[str, Any]]:
    for item in items:
        if isinstance(item, dict):
            return item
    return None


def _collect_aaa_codes(body: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """All AAA reject codes + human descriptions in a 271 ``errors`` array."""
    codes: list[str] = []
    descriptions: list[str] = []
    errors = body.get("errors")
    if isinstance(errors, list):
        for err in errors:
            if not isinstance(err, dict):
                continue
            code = err.get("code")
            if code is not None:
                codes.append(str(code))
            description = err.get("description")
            if description:
                descriptions.append(str(description))
    return codes, descriptions


def _classify_aaa(codes: list[str]) -> Optional[EligibilityStatus]:
    if not codes:
        return None
    if any(c in _AAA_NOT_FOUND for c in codes):
        return EligibilityStatus.NOT_FOUND
    if any(c in _AAA_PAYER_UNAVAILABLE for c in codes):
        return EligibilityStatus.PAYER_UNAVAILABLE
    return EligibilityStatus.REJECTED


def _benefit_entries(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = body.get("benefitsInformation")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _coverage_state(body: Mapping[str, Any]) -> Optional[EligibilityStatus]:
    """Affirmative active/inactive signal, or None when neither is present."""
    codes: list[str] = []
    for entry in _benefit_entries(body):
        code = entry.get("code")
        if code is not None:
            codes.append(str(code))
    plan_status = body.get("planStatus")
    if isinstance(plan_status, list):
        for entry in plan_status:
            if isinstance(entry, dict) and entry.get("statusCode") is not None:
                codes.append(str(entry["statusCode"]))
    if _CODE_ACTIVE in codes:
        return EligibilityStatus.ACTIVE
    if any(c in _CODES_INACTIVE for c in codes):
        return EligibilityStatus.INACTIVE
    return None


def _extract_amounts(body: Mapping[str, Any]) -> dict[str, Optional[str]]:
    """First-listed copay / co-insurance / deductible / OOP-max strings.

    The 271 can carry many qualified variants (in/out of network, individual/
    family, remaining vs total); the FIRST entry per code is normalized here
    for the results row and the full set stays in the retained raw 271.
    """
    out: dict[str, Optional[str]] = {
        "copay": None,
        "coinsurance_percent": None,
        "deductible": None,
        "out_of_pocket_maximum": None,
    }
    key_by_code = {
        "B": ("copay", "benefitAmount"),
        "A": ("coinsurance_percent", "benefitPercent"),
        "C": ("deductible", "benefitAmount"),
        "G": ("out_of_pocket_maximum", "benefitAmount"),
    }
    for entry in _benefit_entries(body):
        mapping = key_by_code.get(str(entry.get("code")))
        if mapping is None:
            continue
        field, source_key = mapping
        value = entry.get(source_key)
        if out[field] is None and value is not None:
            out[field] = str(value)
    return out


def parse_271(
    payer_id: str,
    body_bytes: bytes,
    *,
    http_status: int = 200,
    service_type_codes: Optional[list[str]] = None,
) -> EligibilityResult:
    """Normalize one 271 response, failing CLOSED on anything unparseable.

    Pure and transport-free so the fake-proven tests exercise exactly the
    logic the live client runs. ``body_bytes`` is the exact wire payload;
    its SHA-256 becomes the result's audit digest.
    """
    digest = hashlib.sha256(body_bytes).hexdigest()
    base: dict[str, Any] = {
        "payer_id": payer_id,
        "service_type_codes": list(service_type_codes or []),
        "raw_271_sha256": digest,
        "raw_271_bytes": body_bytes,
        "checked_at": _utcnow(),
    }
    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return EligibilityResult(
            status=EligibilityStatus.INDETERMINATE,
            reason=(
                f"payer {payer_id}: HTTP {http_status} response body is not "
                "JSON -- fail closed (no benefits answer recorded)"
            ),
            **base,
        )
    if not isinstance(body, dict):
        return EligibilityResult(
            status=EligibilityStatus.INDETERMINATE,
            reason=(
                f"payer {payer_id}: HTTP {http_status} JSON body is not an "
                "object -- fail closed"
            ),
            **base,
        )

    base["raw_271"] = body
    aaa_codes, error_texts = _collect_aaa_codes(body)
    base["aaa_codes"] = aaa_codes
    base["errors"] = error_texts
    payer = body.get("payer")
    if isinstance(payer, dict) and payer.get("name"):
        base["payer_name"] = str(payer["name"])
    plan_info = body.get("planInformation")
    if isinstance(plan_info, dict):
        name = plan_info.get("planDescription") or plan_info.get("groupDescription")
        if name:
            base["plan_name"] = str(name)
    plan_dates = body.get("planDateInformation")
    if isinstance(plan_dates, dict):
        begin = plan_dates.get("planBegin") or plan_dates.get("eligibilityBegin")
        end = plan_dates.get("planEnd") or plan_dates.get("eligibilityEnd")
        if begin:
            base["plan_begin"] = str(begin)
        if end:
            base["plan_end"] = str(end)
    if base.get("plan_name") is None:
        plan_status = body.get("planStatus")
        if isinstance(plan_status, list):
            entry = _first(plan_status)
            if entry and entry.get("planDetails"):
                base["plan_name"] = str(entry["planDetails"])

    aaa_status = _classify_aaa(aaa_codes)
    if aaa_status is not None:
        return EligibilityResult(
            status=aaa_status,
            reason=(
                f"payer {payer_id}: AAA reject code(s) "
                f"{','.join(aaa_codes)} -> {aaa_status.value}"
            ),
            **base,
        )

    coverage = _coverage_state(body)
    if coverage is not None:
        base.update(_extract_amounts(body))
        return EligibilityResult(
            status=coverage,
            reason=(
                f"payer {payer_id}: benefitsInformation affirms "
                f"{coverage.value} coverage"
            ),
            **base,
        )

    if http_status // 100 != 2:
        return EligibilityResult(
            status=EligibilityStatus.PAYER_UNAVAILABLE,
            reason=(
                f"payer {payer_id}: HTTP {http_status} with no parseable AAA "
                "or coverage signal -- treating as payer/clearinghouse "
                "unavailable (safe to retry or fall through; a 270 is a read)"
            ),
            **base,
        )
    return EligibilityResult(
        status=EligibilityStatus.INDETERMINATE,
        reason=(
            f"payer {payer_id}: 2xx response carries neither an affirmative "
            "coverage state nor an AAA error -- fail closed (never guess "
            "a benefits answer)"
        ),
        **base,
    )


class StediEligibilityClient:
    """Real-time 270/271 checks through Stedi's documented JSON endpoint.

    Args:
        auth: Optional :class:`AuthRef` naming where the credential lives.
            Default: the ``STEDI_API_KEY`` environment variable, sent as
            Stedi's documented ``Authorization: Key <api_key>`` header.
            Construction FAILS LOUD when the referenced variable is absent --
            the client is never wired silently unauthenticated.
        base_url: Override for tests / a faithful local fake.
        timeout_s: Per-request timeout. Stedi's leg to slow payers can take
            tens of seconds; 30 s is a practical default.
        transport: Optional ``httpx`` transport (tests inject
            ``httpx.MockTransport``); ``None`` uses the real network.
        env: Optional environment mapping override (tests).
    """

    def __init__(
        self,
        *,
        auth: Optional[AuthRef] = None,
        base_url: str = STEDI_ELIGIBILITY_URL,
        timeout_s: float = 30.0,
        transport: Any = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.base_url = base_url
        self.timeout_s = timeout_s
        self._transport = transport
        self._headers = self._resolve_headers(auth, env)

    @staticmethod
    def _resolve_headers(
        auth: Optional[AuthRef], env: Optional[Mapping[str, str]]
    ) -> dict[str, str]:
        if auth is not None:
            headers = auth.resolve_headers(env)
        else:
            import os

            source = os.environ if env is None else env
            key = source.get(STEDI_API_KEY_ENV, "")
            if not key:
                raise ValueError(
                    f"eligibility client references environment variable "
                    f"{STEDI_API_KEY_ENV!r}, which is not set (or empty) -- "
                    "refusing to construct an unauthenticated client (create "
                    "a Stedi TEST-mode key for the free mock catalog)"
                )
            value = key if key.startswith("Key ") else f"Key {key}"
            headers = {"Authorization": value}
        headers.setdefault("Content-Type", "application/json")
        return headers

    def check(self, request: EligibilityRequest) -> EligibilityResult:
        """Send one 270 inquiry; normalize the 271 (or its failure) fail-closed.

        Never raises for transport or payer failures -- a 270 is an
        idempotent read, so every failure maps to a status the waterfall can
        act on (``payer_unavailable`` -> retry / portal tier).
        """
        import httpx  # lazy: keep the module import light

        try:
            with httpx.Client(
                transport=self._transport, timeout=self.timeout_s
            ) as session:
                response = session.post(
                    self.base_url, headers=self._headers, json=request.to_stedi_body()
                )
        except httpx.HTTPError as exc:
            return EligibilityResult(
                status=EligibilityStatus.PAYER_UNAVAILABLE,
                payer_id=request.payer_id,
                service_type_codes=list(request.service_type_codes),
                reason=(
                    f"payer {request.payer_id}: transport failure "
                    f"({type(exc).__name__}) -- eligibility API unavailable; "
                    "safe to retry or fall through (a 270 is a read, nothing "
                    "was written)"
                ),
                checked_at=_utcnow(),
            )
        return parse_271(
            request.payer_id,
            response.content,
            http_status=response.status_code,
            service_type_codes=list(request.service_type_codes),
        )

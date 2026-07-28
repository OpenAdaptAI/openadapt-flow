"""REST/JSON :class:`ApiActuator` -- perform a step's write via its API binding.

This is the ``api`` implementation tier of the RFC's transition contract
(``docs/design/WORKFLOW_PROGRAM_IR.md`` section 4: "call the app's API / DB
write; effect probed against the system of record"). Given a step's
:class:`~openadapt_flow.ir.ApiBinding` and the run's typed params, it
substitutes the params into the endpoint / query / body templates and issues a
single deterministic HTTP request -- no pixel matching, no model call, ``$0``.

The hard requirement is **never double-write the same effect**. A naive
"try the API, and on any failure fall back to GUI-clicking the same save"
would, on a request that the server actually PROCESSED (a read-timeout after
commit, a 5xx after a partial write), perform the write TWICE. So the actuator
classifies every attempt into exactly one of three fail-safe outcomes
(:class:`ActuationStatus`), keyed on *whether the request could have reached
the server*:

- :attr:`ActuationStatus.UNAVAILABLE` -- the request was **never sent** (the
  TCP connection was never established: connection refused, DNS failure,
  connect-timeout) or the binding could not even be built (a param the URL/body
  needs was not supplied). Nothing was written, so it is SAFE for the caller to
  fall through to the GUI ladder for this step. This is the "reachable
  ApiBinding" gate: an unreachable endpoint simply is not actuated.
- :attr:`ActuationStatus.ACTUATED` -- the request was sent and the server
  returned success (2xx, or an explicitly-allowed status). The write was
  performed; the caller MUST now confirm it with the EffectVerifier and MUST
  skip the GUI (never re-do the write).
- :attr:`ActuationStatus.HALT` -- the request WAS sent but its outcome is
  unknown or a rejection (read-timeout after the bytes went out, a non-2xx
  response, any post-send transport error). The write MAY have landed, so the
  caller must NEITHER accept it as success NOR GUI-write it again -- it HALTs
  (the same refuse-rather-than-guess posture as the EffectVerifier's
  INDETERMINATE verdict).

The connect-phase / read-phase split is exact in ``requests``:
``ConnectTimeout`` subclasses ``ConnectionError`` (nothing sent -> UNAVAILABLE)
while ``ReadTimeout`` does not (bytes sent -> HALT), so catching
``ConnectionError`` before ``Timeout`` gives the right classification.

Import-light: ``requests`` is imported lazily so importing this module (and the
runtime package) stays cheap and model-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping, Optional, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from openadapt_flow.ir import ApiBinding


class ActuationStatus(str, Enum):
    """The fail-safe outcome of an API actuation attempt (no-double-write)."""

    #: The write was performed and the server acknowledged success -> the
    #: caller confirms it with the EffectVerifier and SKIPS the GUI.
    ACTUATED = "actuated"
    #: The request was never sent (endpoint unreachable, or the binding could
    #: not be built) -> nothing was written; SAFE to fall through to the GUI
    #: ladder for this step.
    UNAVAILABLE = "unavailable"
    #: The request WAS sent but its outcome is unknown or a rejection -> the
    #: write may have landed; HALT (never accept, never GUI-write it again).
    HALT = "halt"


class ApiActuationResult(BaseModel):
    """Outcome of one :meth:`ApiActuator.actuate` call."""

    model_config = ConfigDict(frozen=True)

    status: ActuationStatus
    substrate: str = "rest"
    #: Human-readable reason (audit trail / error surface). Contains the
    #: UNSUBSTITUTED method + endpoint template only -- never the substituted
    #: values -- so it is safe to log without leaking PHI-bearing params.
    reason: str = ""
    #: HTTP status code when a response was received; None otherwise.
    http_status: Optional[int] = None
    #: ``"METHOD url_template"`` (unsubstituted) for the audit line.
    request_summary: str = ""

    @property
    def actuated(self) -> bool:
        return self.status is ActuationStatus.ACTUATED

    @property
    def should_fall_through(self) -> bool:
        """True when the caller may safely fall through to the GUI ladder
        (the request was never sent, so nothing was written)."""
        return self.status is ActuationStatus.UNAVAILABLE

    @property
    def should_halt(self) -> bool:
        return self.status is ActuationStatus.HALT


class _MissingParam(KeyError):
    """A template referenced a param the run did not supply."""


class _StrictMap(dict):
    def __missing__(self, key: str) -> Any:  # noqa: D401
        raise _MissingParam(key)


def _fill(template: str, params: dict[str, str]) -> str:
    """Substitute ``{param}`` placeholders in ``template`` from ``params``.

    Raises :class:`_MissingParam` when the template references a key that is
    not in ``params`` -- the binding cannot be built, so the actuator reports
    UNAVAILABLE (a before-send problem: nothing is written, GUI fallback is
    safe) rather than sending a half-formed request.
    """
    return template.format_map(_StrictMap(params))


def _fill_body(node: Any, params: dict[str, str]) -> Any:
    """Recursively substitute ``{param}`` in every string leaf of a JSON body."""
    if isinstance(node, str):
        return _fill(node, params)
    if isinstance(node, dict):
        return {k: _fill_body(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill_body(v, params) for v in node]
    return node


@dataclass(frozen=True, slots=True)
class ExternalActuationRequest:
    """A fully rendered invocation for a deployment-owned MCP/tool adapter.

    Values can contain sensitive workflow parameters.  Their fields are absent
    from ``repr`` so accidental exception or debug output does not echo them.
    """

    contract_version: Literal[1]
    executor_id: str
    kind: Literal["mcp", "tool"]
    operation: str
    target: str = field(repr=False)
    body: dict[str, Any] = field(repr=False)
    query: dict[str, str] = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    timeout_s: float
    request_summary: str


@runtime_checkable
class ExternalExecutor(Protocol):
    """Deployment-owned dispatcher for one external executor id.

    The adapter must return the shared no-double-write result.  ``UNAVAILABLE``
    means that it proved no delivery occurred.  The runtime still converts that
    result to a refusal for MCP/tool bindings; it never falls through to GUI.
    """

    def actuate(self, request: ExternalActuationRequest) -> ApiActuationResult: ...


class ApiActuator:
    """Perform a step's write via its :class:`~openadapt_flow.ir.ApiBinding`.

    Bound to a deployment's API base URL (and an optional injected session for
    auth headers / tests), mirroring
    :class:`~openadapt_flow.runtime.effects.rest.RestRecordVerifier`. The
    binding's ``url_template`` may be absolute (``http...``) or relative to
    ``base_url``; params from the run substitute into the URL, query, and body
    templates. A single request is issued and classified into the fail-safe
    :class:`ActuationStatus` outcome. Makes ZERO model calls.

    Args:
        base_url: Deployment API base URL (used for relative ``url_template``s;
            trailing slash optional). May be empty when every binding is
            absolute.
        session: Optional ``requests``-style session (auth headers / test
            injection); a module-level default is created lazily when omitted.
        timeout_s: Default per-request timeout when the binding sets none.
        external_executors: Explicit executor-id to MCP/tool adapter map. The
            workflow stores only the typed id and contract version.
    """

    substrate = "rest"

    def __init__(
        self,
        base_url: str = "",
        *,
        session: Any = None,
        timeout_s: float = 5.0,
        external_executors: Optional[Mapping[str, ExternalExecutor]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_timeout_s = timeout_s
        self._session = session
        self._external_executors = dict(external_executors or {})

    def supports(self, binding: ApiBinding) -> bool:
        """Return whether this actuator has an exact dispatcher for ``binding``."""

        if binding.kind in ("rest", "fhir"):
            return True
        contract = binding.external_executor
        if contract is None:
            return False
        executor = self._external_executors.get(contract.executor_id)
        return isinstance(executor, ExternalExecutor)

    def _get_session(self) -> Any:
        if self._session is None:
            import requests  # lazy: keep module import light and model-free

            self._session = requests.Session()
        return self._session

    def _resolve_url(self, url_template: str) -> str:
        if url_template.startswith(("http://", "https://")):
            return url_template
        return f"{self.base_url}{url_template}"

    def actuate(
        self, binding: ApiBinding, params: dict[str, str]
    ) -> ApiActuationResult:
        """Perform ``binding``'s write, substituting ``params``; classify safely.

        Returns an :class:`ApiActuationResult` whose :attr:`status` tells the
        caller exactly one safe next move: confirm-and-skip-GUI (ACTUATED),
        fall-through-to-GUI (UNAVAILABLE, nothing was written), or HALT
        (attempted, outcome unknown -- never double-write). Never raises.
        """
        summary = f"{binding.method} {binding.url_template}"

        if binding.kind in ("mcp", "tool"):
            return self._actuate_external(binding, params, summary)

        # -- build the request (a before-send problem is UNAVAILABLE) ---------
        try:
            url = self._resolve_url(_fill(binding.url_template, params))
            query = {k: _fill(v, params) for k, v in binding.query.items()}
            body = _fill_body(binding.body_template, params)
            headers = {k: _fill(v, params) for k, v in binding.headers.items()}
        except _MissingParam as exc:
            return ApiActuationResult(
                status=ActuationStatus.UNAVAILABLE,
                substrate=binding.kind,
                reason=(
                    f"binding for {summary} references param {exc} not supplied "
                    "by the run -- API tier unavailable, falling through to GUI"
                ),
                request_summary=summary,
            )

        import requests  # lazy; hierarchy: ConnectTimeout is a ConnectionError

        timeout = binding.timeout_s or self.default_timeout_s
        try:
            resp = self._get_session().request(
                binding.method.upper(),
                url,
                params=query or None,
                json=body if body else None,
                headers=headers or None,
                timeout=timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            # Connection never established (refused / DNS / connect-timeout):
            # the request was NEVER sent, so nothing was written -> it is safe
            # to fall through to the GUI ladder for this step.
            return ApiActuationResult(
                status=ActuationStatus.UNAVAILABLE,
                substrate=binding.kind,
                reason=(
                    f"endpoint unreachable ({type(exc).__name__}) -- request "
                    "not sent, API tier unavailable, falling through to GUI"
                ),
                request_summary=summary,
            )
        except requests.exceptions.Timeout as exc:
            # Read-timeout: the bytes WENT OUT and the server may have processed
            # the write. Outcome unknown -> HALT (never GUI-write it again).
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=binding.kind,
                reason=(
                    f"request sent but timed out awaiting the response "
                    f"({type(exc).__name__}) -- the write may have landed; HALT "
                    "(never double-write via the GUI)"
                ),
                request_summary=summary,
            )
        except Exception as exc:  # noqa: BLE001
            # Any other transport error after the request left the client is of
            # unknown effect on the server -> HALT rather than risk a duplicate.
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=binding.kind,
                reason=(
                    f"request failed after being sent ({type(exc).__name__}) -- "
                    "outcome unknown; HALT (never double-write via the GUI)"
                ),
                request_summary=summary,
            )

        allowed = binding.expected_status or list(range(200, 300))
        ok = resp.status_code in allowed or (
            not binding.expected_status and resp.status_code // 100 == 2
        )
        if ok:
            return ApiActuationResult(
                status=ActuationStatus.ACTUATED,
                substrate=binding.kind,
                reason=f"{summary} -> {resp.status_code}",
                http_status=resp.status_code,
                request_summary=summary,
            )
        # Non-success response: the request was PROCESSED by the server. Even a
        # clean rejection is ambiguous about what (if anything) persisted, and
        # re-driving the same write through the GUI risks a duplicate -> HALT.
        return ApiActuationResult(
            status=ActuationStatus.HALT,
            substrate=binding.kind,
            reason=(
                f"{summary} returned {resp.status_code} (not success) -- the "
                "write was attempted; HALT (never double-write via the GUI)"
            ),
            http_status=resp.status_code,
            request_summary=summary,
        )

    def _actuate_external(
        self,
        binding: ApiBinding,
        params: dict[str, str],
        summary: str,
    ) -> ApiActuationResult:
        """Invoke a registered MCP/tool adapter without a GUI fallback."""

        contract = binding.external_executor
        if contract is None:  # Defensive for callers that bypass model validation.
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=binding.kind,
                reason=(
                    f"{binding.kind} binding has no external executor contract; "
                    "refusing dispatch and GUI fallback"
                ),
                request_summary=summary,
            )
        executor = self._external_executors.get(contract.executor_id)
        if executor is None:
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=binding.kind,
                reason=(
                    f"no registered external executor matches "
                    f"{contract.executor_id!r}; refusing dispatch and GUI fallback"
                ),
                request_summary=summary,
            )
        try:
            kind = cast(Literal["mcp", "tool"], binding.kind)
            request = ExternalActuationRequest(
                contract_version=contract.contract_version,
                executor_id=contract.executor_id,
                kind=kind,
                operation=binding.method,
                target=_fill(binding.url_template, params),
                body=_fill_body(binding.body_template, params),
                query={k: _fill(v, params) for k, v in binding.query.items()},
                headers={k: _fill(v, params) for k, v in binding.headers.items()},
                timeout_s=binding.timeout_s or self.default_timeout_s,
                request_summary=summary,
            )
        except _MissingParam as exc:
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=binding.kind,
                reason=(
                    f"external executor binding for {summary} references param "
                    f"{exc} not supplied by the run; refusing dispatch and GUI fallback"
                ),
                request_summary=summary,
            )
        try:
            result = executor.actuate(request)
        except Exception as exc:  # noqa: BLE001 - trusted deployment boundary
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=binding.kind,
                reason=(
                    "external executor raised after delivery may have begun "
                    f"({type(exc).__name__}); outcome requires reconciliation"
                ),
                request_summary=summary,
            )
        if not isinstance(result, ApiActuationResult):
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=binding.kind,
                reason=(
                    "external executor returned an invalid result; outcome requires "
                    "reconciliation"
                ),
                request_summary=summary,
            )
        try:
            # Extension implementations can bypass normal construction with
            # ``model_construct``. Revalidate at the trust boundary so a raw
            # string or unknown status can never reach the GUI-fallback branch.
            result = ApiActuationResult.model_validate(
                {
                    name: getattr(result, name)
                    for name in ApiActuationResult.model_fields
                }
            )
        except (AttributeError, ValidationError):
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=binding.kind,
                reason=(
                    "external executor returned an invalid result; outcome requires "
                    "reconciliation"
                ),
                request_summary=summary,
            )
        if result.status is ActuationStatus.UNAVAILABLE:
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                substrate=binding.kind,
                reason=(
                    "external executor reported no delivery; the qualified external "
                    "path is unavailable, so GUI fallback is refused"
                ),
                request_summary=summary,
            )
        receipt = (
            "acknowledged delivery"
            if result.status is ActuationStatus.ACTUATED
            else "reported an uncertain or refused delivery"
        )
        return result.model_copy(
            update={
                "substrate": binding.kind,
                "reason": (
                    f"external executor {contract.executor_id!r} {receipt}; "
                    "adapter details are excluded from the portable report"
                ),
                "request_summary": summary,
            }
        )

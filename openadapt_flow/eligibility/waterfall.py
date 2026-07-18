"""The eligibility waterfall seam: per-payer API-vs-portal route resolution.

A committed YAML registry (``payer_routes.yaml``, reviewed like any other
governed config) maps each payer to exactly one primary route:

- ``api`` -- real-time 270/271 through the clearinghouse API first;
- ``portal`` -- compiled portal replay with effect verification (the wedge);
- ``excluded`` -- automation of that surface is contractually barred
  (Availity's portal) until a sanctioned enrollment converts it.

:func:`resolve_route` is the seam the fulfillment loop calls per patient:
given the payer name it returns a typed :class:`RouteDecision`, so the
route choice is data, not code. :func:`run_waterfall` composes the decision
with a client call and encodes the fallback rules:

- A 270 inquiry is an idempotent READ, so an ``api`` attempt that yields no
  benefits answer (payer unavailable, member not found, indeterminate parse,
  request rejected) may safely fall through to the portal tier -- the
  opposite of the write-path
  :class:`~openadapt_flow.runtime.actuators.api.ApiActuator`, which must
  HALT on sent-but-unacknowledged writes.
- EXCEPT when the payer's portal is contractually banned
  (``portal_banned: true``): then the fallback is ``excluded`` and the
  check lands in the practice's queue instead of a portal run.

Import-light: PyYAML (a core dependency) loads lazily, and nothing here is
imported on the replay hot path.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel, Field

from openadapt_flow.eligibility.client import (
    EligibilityRequest,
    EligibilityResult,
    StediEligibilityClient,
)

#: The committed registry shipped with the package.
DEFAULT_REGISTRY_PATH = Path(__file__).parent / "payer_routes.yaml"


class EligibilityRoute(str, Enum):
    """Primary route for a payer's eligibility checks."""

    API = "api"
    PORTAL = "portal"
    EXCLUDED = "excluded"


class PayerCapability(BaseModel):
    """One payer's entry in the capability map."""

    key: str
    display_name: str = ""
    route: EligibilityRoute
    #: Stedi ``tradingPartnerServiceId`` when known; ``None`` means "resolve
    #: in Stedi's payer directory at enrollment".
    stedi_payer_id: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    #: Portal automation contractually banned (e.g. Availity) -- the
    #: waterfall must NEVER fall through to this payer's portal.
    portal_banned: bool = False
    #: Whether the entry was checked against a cited source (see YAML notes).
    verified: bool = False
    verified_on: Optional[str] = None
    notes: str = ""


class PayerRegistry(BaseModel):
    """The loaded capability map."""

    version: int = 1
    #: Route for payers not present in the map. Portal replay is the honest
    #: default: an unknown payer has no confirmed API route.
    default_route: EligibilityRoute = EligibilityRoute.PORTAL
    payers: dict[str, PayerCapability] = Field(default_factory=dict)

    def find(self, payer: str) -> Optional[PayerCapability]:
        """Look up a payer by key, display name, or alias (normalized)."""
        wanted = _normalize(payer)
        if not wanted:
            return None
        for cap in self.payers.values():
            names = [cap.key, cap.display_name, *cap.aliases]
            if any(_normalize(name) == wanted for name in names if name):
                return cap
        return None


class RouteDecision(BaseModel):
    """The resolver's typed answer for one payer."""

    route: EligibilityRoute
    payer: str
    capability: Optional[PayerCapability] = None
    reason: str = ""

    @property
    def use_api(self) -> bool:
        return self.route is EligibilityRoute.API


def _normalize(name: str) -> str:
    """Case/punctuation-insensitive payer-name key."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def load_payer_routes(
    path: Union[str, Path, None] = None,
) -> PayerRegistry:
    """Load the capability map (the committed registry by default).

    Fails loud on a missing or malformed file -- a fulfillment loop must
    never run against a silently-empty routing table.
    """
    import yaml  # lazy: core dependency, but keep module import cheap

    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "payers" not in raw:
        raise ValueError(
            f"payer route registry {registry_path} is malformed: expected a "
            "mapping with a 'payers' section"
        )
    payers = {}
    for key, entry in (raw.get("payers") or {}).items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"payer route registry {registry_path}: entry {key!r} is not a mapping"
            )
        payers[key] = PayerCapability(key=key, **entry)
    return PayerRegistry(
        version=int(raw.get("version", 1)),
        default_route=EligibilityRoute(raw.get("default_route", "portal")),
        payers=payers,
    )


def resolve_route(
    payer: str, registry: Optional[PayerRegistry] = None
) -> RouteDecision:
    """Decide the primary route for ``payer`` from the capability map.

    Unknown payers get the registry's ``default_route`` (portal -- the
    honest default for a payer with no confirmed API route).
    """
    reg = registry if registry is not None else load_payer_routes()
    cap = reg.find(payer)
    if cap is None:
        return RouteDecision(
            route=reg.default_route,
            payer=payer,
            reason=(
                f"payer {payer!r} not in the capability map -- "
                f"{reg.default_route.value} route by default (no confirmed "
                "API route)"
            ),
        )
    if cap.route is EligibilityRoute.EXCLUDED:
        return RouteDecision(
            route=EligibilityRoute.EXCLUDED,
            payer=payer,
            capability=cap,
            reason=(
                f"payer {payer!r} ({cap.key}) is excluded from automation: "
                f"{cap.notes or 'contractual exclusion'}"
            ),
        )
    return RouteDecision(
        route=cap.route,
        payer=payer,
        capability=cap,
        reason=f"payer {payer!r} ({cap.key}) routes {cap.route.value}-first",
    )


class WaterfallOutcome(BaseModel):
    """What one waterfall pass did and where the check must go next.

    ``final_route`` is where the check now stands: ``api`` (the API answered
    -- an ``EligibilityResult`` with a real coverage state is attached),
    ``portal`` (run the compiled portal replay for this payer), or
    ``excluded`` (no automated tier may run; the check lands in the
    practice's queue). ``trail`` is the audit line of route decisions.
    """

    final_route: EligibilityRoute
    decision: RouteDecision
    result: Optional[EligibilityResult] = None
    trail: list[str] = Field(default_factory=list)

    @property
    def answered_by_api(self) -> bool:
        return self.result is not None and self.result.is_answer


def run_waterfall(
    request: EligibilityRequest,
    *,
    payer: Optional[str] = None,
    client: Optional[StediEligibilityClient] = None,
    registry: Optional[PayerRegistry] = None,
) -> WaterfallOutcome:
    """Route one eligibility check: API tier first where sanctioned.

    Args:
        request: The 270 inquiry (its ``payer_id`` is used for the API call).
        payer: Payer name for capability lookup; defaults to
            ``request.payer_id``.
        client: The API-tier client. Required only when the resolved route is
            ``api``; construction of the default client fails loud without
            ``STEDI_API_KEY``, so a portal-routed payer never demands a key.
        registry: Capability map override (tests / per-deployment).
    """
    decision = resolve_route(payer or request.payer_id, registry)
    trail = [decision.reason]
    if decision.route is EligibilityRoute.EXCLUDED:
        return WaterfallOutcome(
            final_route=EligibilityRoute.EXCLUDED, decision=decision, trail=trail
        )
    if decision.route is EligibilityRoute.PORTAL:
        return WaterfallOutcome(
            final_route=EligibilityRoute.PORTAL, decision=decision, trail=trail
        )

    api_client = client if client is not None else StediEligibilityClient()
    result = api_client.check(request)
    trail.append(result.reason)
    if result.is_answer:
        return WaterfallOutcome(
            final_route=EligibilityRoute.API,
            decision=decision,
            result=result,
            trail=trail,
        )
    # No benefits answer from the API tier. A 270 is an idempotent read, so
    # falling through is safe -- unless this payer's portal is banned.
    if decision.capability is not None and decision.capability.portal_banned:
        trail.append(
            "portal fallback is contractually banned for this payer -- "
            "check lands in the practice queue (excluded)"
        )
        return WaterfallOutcome(
            final_route=EligibilityRoute.EXCLUDED,
            decision=decision,
            result=result,
            trail=trail,
        )
    trail.append(
        f"API tier gave no benefits answer ({result.status.value}) -- "
        "falling through to portal replay (a 270 is a read; nothing was "
        "written)"
    )
    return WaterfallOutcome(
        final_route=EligibilityRoute.PORTAL,
        decision=decision,
        result=result,
        trail=trail,
    )

"""API-first eligibility (EDI 270/271) -- the API tier of the eligibility
waterfall.

Where a payer exposes a sanctioned real-time 270/271 route through a
clearinghouse API (Stedi is the reference on-ramp), an eligibility check
should hit that route instead of driving a payer portal's GUI: no page to
drift, no CAPTCHA, no session/MFA cascade, ~$0.08-0.30 per check. Portal
replay remains the fallback for API-less payers and API-missing *fields*
(the ADA-documented weak tail), and stays categorically excluded where a
portal contractually bans automation (Availity) until the sanctioned API
enrollment exists.

Three modules, all import-light (httpx and yaml load lazily):

- :mod:`.client` -- the Stedi 270/271 client and the normalized
  :class:`~openadapt_flow.eligibility.client.EligibilityResult` (raw 271
  retained + SHA-256 digest for audit). Secret-isolated auth (env-var
  references, never literals), fail-closed parsing (a malformed response is
  INDETERMINATE, never a guessed "active").
- :mod:`.waterfall` -- the per-payer capability map (committed YAML registry:
  payer -> ``api`` | ``portal`` | ``excluded``) and the route resolver the
  fulfillment loop uses to pick the route automatically.
- :mod:`.artifact` -- writes each result into the practice-local results
  artifact set (results CSV + raw-271 document) and verifies the write with
  the effect-verifier kit's document-hash substrate, so the API result is
  governed by the same halt-instead-of-guess wedge as a portal or human
  write. Effect verification is source-agnostic; that is the point.

NOT on the replay hot path: nothing here is imported by the recorder,
compiler, or replayer. It is an adjacent fulfillment component that shares
the runtime's effect-verification and secret-isolation conventions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.eligibility.artifact import (
        EligibilityArtifact,
        write_and_verify,
        write_eligibility_artifacts,
    )
    from openadapt_flow.eligibility.client import (
        EligibilityRequest,
        EligibilityResult,
        EligibilityStatus,
        StediEligibilityClient,
    )
    from openadapt_flow.eligibility.waterfall import (
        EligibilityRoute,
        PayerCapability,
        RouteDecision,
        load_payer_routes,
        resolve_route,
        run_waterfall,
    )

__all__ = [
    "EligibilityArtifact",
    "EligibilityRequest",
    "EligibilityResult",
    "EligibilityRoute",
    "EligibilityStatus",
    "PayerCapability",
    "RouteDecision",
    "StediEligibilityClient",
    "load_payer_routes",
    "resolve_route",
    "run_waterfall",
    "write_and_verify",
    "write_eligibility_artifacts",
]

_CLIENT = (
    "EligibilityRequest",
    "EligibilityResult",
    "EligibilityStatus",
    "StediEligibilityClient",
)
_WATERFALL = (
    "EligibilityRoute",
    "PayerCapability",
    "RouteDecision",
    "load_payer_routes",
    "resolve_route",
    "run_waterfall",
)
_ARTIFACT = ("EligibilityArtifact", "write_and_verify", "write_eligibility_artifacts")


def __getattr__(name: str) -> object:
    """Lazy re-exports (PEP 562) -- importing the package stays cheap."""
    if name in _CLIENT:
        from openadapt_flow.eligibility import client

        return getattr(client, name)
    if name in _WATERFALL:
        from openadapt_flow.eligibility import waterfall

        return getattr(waterfall, name)
    if name in _ARTIFACT:
        from openadapt_flow.eligibility import artifact

        return getattr(artifact, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

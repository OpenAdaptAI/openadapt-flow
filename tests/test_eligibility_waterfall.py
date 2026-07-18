"""Tests for the per-payer capability map and the waterfall resolver."""

from __future__ import annotations

import httpx
import pytest

from openadapt_flow.eligibility.client import (
    EligibilityRequest,
    EligibilityStatus,
    StediEligibilityClient,
)
from openadapt_flow.eligibility.waterfall import (
    DEFAULT_REGISTRY_PATH,
    EligibilityRoute,
    PayerCapability,
    PayerRegistry,
    load_payer_routes,
    resolve_route,
    run_waterfall,
)

ENV = {"STEDI_API_KEY": "test-key-123"}

ACTIVE_271 = {
    "payer": {"name": "Cigna"},
    "benefitsInformation": [{"code": "1", "name": "Active Coverage"}],
}
PAYER_DOWN_271 = {
    "errors": [{"code": "42", "description": "Unable to Respond at Current Time"}]
}


def fake_client(body: dict, status: int = 200) -> StediEligibilityClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return StediEligibilityClient(transport=httpx.MockTransport(handler), env=ENV)


def request_for(payer_id: str) -> EligibilityRequest:
    return EligibilityRequest(
        payer_id=payer_id,
        provider_npi="1999999984",
        provider_organization="One",
        member_id="123",
    )


# -- the committed registry ---------------------------------------------------


def test_committed_registry_loads_and_ships_with_package():
    assert DEFAULT_REGISTRY_PATH.exists()
    registry = load_payer_routes()
    assert registry.default_route is EligibilityRoute.PORTAL
    assert set(registry.payers) >= {
        "delta_dental",
        "metlife",
        "cigna_dental",
        "guardian",
        "united_concordia",
        "dentaquest",
        "availity",
    }


def test_six_confirmed_dental_payers_route_api_first():
    registry = load_payer_routes()
    for key in (
        "delta_dental",
        "metlife",
        "cigna_dental",
        "guardian",
        "united_concordia",
        "dentaquest",
    ):
        assert registry.payers[key].route is EligibilityRoute.API, key


def test_availity_is_excluded_and_portal_banned():
    registry = load_payer_routes()
    availity = registry.payers["availity"]
    assert availity.route is EligibilityRoute.EXCLUDED
    assert availity.portal_banned
    assert "sanctioned" in availity.notes.lower()  # the API conversion path
    decision = resolve_route("Availity Essentials", registry)
    assert decision.route is EligibilityRoute.EXCLUDED


def test_alias_and_case_insensitive_lookup():
    registry = load_payer_routes()
    for name in ("Cigna", "CIGNA DENTAL", "cigna dental health"):
        decision = resolve_route(name, registry)
        assert decision.use_api, name
        assert decision.capability is not None
        assert decision.capability.stedi_payer_id == "62308"


def test_unknown_payer_defaults_to_portal():
    decision = resolve_route("Mom & Pop Dental Trust", load_payer_routes())
    assert decision.route is EligibilityRoute.PORTAL
    assert not decision.use_api
    assert "not in the capability map" in decision.reason


def test_malformed_registry_fails_loud(tmp_path):
    bad = tmp_path / "routes.yaml"
    bad.write_text("just a string")
    with pytest.raises(ValueError, match="malformed"):
        load_payer_routes(bad)


# -- run_waterfall: the fulfillment seam -------------------------------------


def test_api_answer_finishes_on_api_route():
    outcome = run_waterfall(
        request_for("62308"),
        payer="Cigna Dental",
        client=fake_client(ACTIVE_271),
        registry=load_payer_routes(),
    )
    assert outcome.final_route is EligibilityRoute.API
    assert outcome.answered_by_api
    assert outcome.result is not None
    assert outcome.result.status is EligibilityStatus.ACTIVE


def test_api_unavailable_falls_through_to_portal():
    outcome = run_waterfall(
        request_for("62308"),
        payer="Cigna Dental",
        client=fake_client(PAYER_DOWN_271),
        registry=load_payer_routes(),
    )
    assert outcome.final_route is EligibilityRoute.PORTAL
    assert not outcome.answered_by_api
    assert outcome.result is not None
    assert outcome.result.status is EligibilityStatus.PAYER_UNAVAILABLE
    assert any("falling through to portal" in step for step in outcome.trail)


def test_portal_route_never_needs_an_api_key():
    # No client, no STEDI_API_KEY: a portal-routed payer must not construct
    # the API client at all.
    outcome = run_waterfall(
        request_for("LOCAL1"),
        payer="Mom & Pop Dental Trust",
        registry=load_payer_routes(),
    )
    assert outcome.final_route is EligibilityRoute.PORTAL
    assert outcome.result is None


def test_excluded_payer_never_reaches_api_or_portal():
    outcome = run_waterfall(
        request_for("AVAILITY"),
        payer="Availity",
        registry=load_payer_routes(),
    )
    assert outcome.final_route is EligibilityRoute.EXCLUDED
    assert outcome.result is None


def test_portal_banned_api_payer_lands_excluded_not_portal():
    # An api-sanctioned / portal-banned payer whose API leg is down must land
    # in the queue, never on its banned portal.
    registry = PayerRegistry(
        payers={
            "strict": PayerCapability(
                key="strict",
                route=EligibilityRoute.API,
                portal_banned=True,
                aliases=["strict dental"],
            )
        }
    )
    outcome = run_waterfall(
        request_for("STRICT1"),
        payer="Strict Dental",
        client=fake_client(PAYER_DOWN_271),
        registry=registry,
    )
    assert outcome.final_route is EligibilityRoute.EXCLUDED
    assert any("contractually banned" in step for step in outcome.trail)


def test_indeterminate_parse_is_not_an_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops</html>")

    client = StediEligibilityClient(transport=httpx.MockTransport(handler), env=ENV)
    outcome = run_waterfall(
        request_for("62308"),
        payer="Cigna Dental",
        client=client,
        registry=load_payer_routes(),
    )
    assert not outcome.answered_by_api
    assert outcome.final_route is EligibilityRoute.PORTAL
    assert outcome.result is not None
    assert outcome.result.status is EligibilityStatus.INDETERMINATE


def test_lazy_package_exports():
    import openadapt_flow.eligibility as elig

    assert elig.EligibilityRoute is EligibilityRoute
    assert callable(elig.run_waterfall)
    with pytest.raises(AttributeError):
        elig.nope

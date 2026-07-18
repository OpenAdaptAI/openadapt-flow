"""Contract tests for the Stedi 270/271 eligibility client.

The fakes are FAITHFUL to Stedi's documented request/response shapes
(endpoint, ``Authorization: Key`` header, ``tradingPartnerServiceId`` /
``subscriber`` / ``encounter.serviceTypeCodes`` request, ``planStatus`` /
``benefitsInformation`` / AAA ``errors`` response -- fetched 2026-07-18, see
``docs/ELIGIBILITY_API_WATERFALL.md``). This suite is CONTRACT-proven: it
proves the client's parsing/fail-closed/auth behavior against those
documented shapes, not against Stedi's live service (that is the env-gated
smoke test in ``tests/test_eligibility_live_stedi.py``).
"""

from __future__ import annotations

import json

import httpx
import pytest

from openadapt_flow.eligibility.client import (
    EligibilityRequest,
    EligibilityStatus,
    StediEligibilityClient,
    parse_271,
)

ENV = {"STEDI_API_KEY": "test-key-123"}


def dental_request(**overrides) -> EligibilityRequest:
    """A dental inquiry shaped like Stedi's documented dental mocks."""
    base = dict(
        payer_id="62308",
        member_id="U3141592653",
        first_name="Jaguar",
        last_name="Dent",
        date_of_birth="19960505",
        provider_npi="1999999984",
        provider_organization="One",
        service_type_codes=["35"],
    )
    base.update(overrides)
    return EligibilityRequest(**base)


#: A faithful active-coverage dental 271 (documented benefitsInformation
#: codes: 1 active, C deductible, B copay, A co-insurance, G OOP max).
ACTIVE_271 = {
    "controlNumber": "000000001",
    "tradingPartnerServiceId": "62308",
    "payer": {
        "entityIdentifier": "Payer",
        "name": "Cigna",
        "payorIdentification": "62308",
    },
    "planInformation": {"groupNumber": "1234567", "groupDescription": "DENTAL PPO"},
    "planDateInformation": {"planBegin": "20260101", "eligibilityBegin": "20260101"},
    "planStatus": [
        {
            "statusCode": "1",
            "status": "Active Coverage",
            "planDetails": "Cigna Dental PPO",
            "serviceTypeCodes": ["35"],
        }
    ],
    "benefitsInformation": [
        {"code": "1", "name": "Active Coverage", "serviceTypeCodes": ["35"]},
        {
            "code": "C",
            "name": "Deductible",
            "benefitAmount": "50",
            "coverageLevelCode": "IND",
            "inPlanNetworkIndicatorCode": "Y",
            "serviceTypeCodes": ["35"],
        },
        {"code": "B", "name": "Co-Payment", "benefitAmount": "20"},
        {"code": "A", "name": "Co-Insurance", "benefitPercent": "0.2"},
        {"code": "G", "name": "Out of Pocket (Stop Loss)", "benefitAmount": "1500"},
    ],
    "x12": "ISA*00*...~ST*271*0001*005010X279A1~...~IEA*1*000000001~",
}

INACTIVE_271 = {
    "tradingPartnerServiceId": "87726",
    "payer": {"name": "UnitedHealthcare"},
    "benefitsInformation": [
        {"code": "6", "name": "Inactive", "serviceTypeCodes": ["30"]}
    ],
}

AAA_PAYER_DOWN_271 = {
    "tradingPartnerServiceId": "62308",
    "errors": [
        {
            "code": "42",
            "description": "Unable to Respond at Current Time",
            "followupAction": "Resubmission Allowed",
        }
    ],
}

AAA_NOT_FOUND_271 = {
    "tradingPartnerServiceId": "62308",
    "errors": [
        {
            "code": "75",
            "description": "Subscriber/Insured Not Found",
            "followupAction": "Please Correct and Resubmit",
        }
    ],
}


def make_client(handler) -> StediEligibilityClient:
    return StediEligibilityClient(transport=httpx.MockTransport(handler), env=ENV)


# -- request shape + auth ----------------------------------------------------


def test_request_body_matches_documented_stedi_shape():
    body = dental_request().to_stedi_body()
    assert body == {
        "tradingPartnerServiceId": "62308",
        "provider": {"npi": "1999999984", "organizationName": "One"},
        "subscriber": {
            "firstName": "Jaguar",
            "lastName": "Dent",
            "dateOfBirth": "19960505",
            "memberId": "U3141592653",
        },
        "encounter": {"serviceTypeCodes": ["35"]},
    }


def test_client_sends_key_auth_header_and_json_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ACTIVE_271)

    result = make_client(handler).check(dental_request())
    assert seen["auth"] == "Key test-key-123"  # documented Stedi format
    assert seen["body"]["tradingPartnerServiceId"] == "62308"
    assert result.status is EligibilityStatus.ACTIVE


def test_missing_api_key_fails_loud_at_construction():
    with pytest.raises(ValueError, match="STEDI_API_KEY"):
        StediEligibilityClient(env={})


def test_secret_never_appears_in_result_or_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ACTIVE_271)

    result = make_client(handler).check(dental_request())
    dumped = result.model_dump_json() + result.reason + repr(result)
    assert "test-key-123" not in dumped


# -- 271 normalization (fake-proven against documented shapes) ---------------


def test_active_dental_271_normalizes_benefits():
    result = parse_271(
        "62308", json.dumps(ACTIVE_271).encode(), service_type_codes=["35"]
    )
    assert result.status is EligibilityStatus.ACTIVE
    assert result.is_answer
    assert result.payer_name == "Cigna"
    assert result.plan_name == "DENTAL PPO"
    assert result.plan_begin == "20260101"
    assert result.deductible == "50"
    assert result.copay == "20"
    assert result.coinsurance_percent == "0.2"
    assert result.out_of_pocket_maximum == "1500"
    assert result.raw_271 == ACTIVE_271  # raw 271 retained for audit
    assert result.raw_271_sha256 and len(result.raw_271_sha256) == 64


def test_inactive_271():
    result = parse_271("87726", json.dumps(INACTIVE_271).encode())
    assert result.status is EligibilityStatus.INACTIVE
    assert result.is_answer  # inactive IS a benefits answer


def test_aaa_42_maps_to_payer_unavailable():
    result = parse_271("62308", json.dumps(AAA_PAYER_DOWN_271).encode())
    assert result.status is EligibilityStatus.PAYER_UNAVAILABLE
    assert not result.is_answer
    assert result.aaa_codes == ["42"]


def test_aaa_75_maps_to_not_found():
    result = parse_271("62308", json.dumps(AAA_NOT_FOUND_271).encode())
    assert result.status is EligibilityStatus.NOT_FOUND
    assert not result.is_answer
    assert "Subscriber/Insured Not Found" in result.errors


def test_aaa_43_maps_to_rejected():
    body = {
        "errors": [
            {"code": "43", "description": "Invalid/Missing Provider Identification"}
        ]
    }
    result = parse_271("62308", json.dumps(body).encode())
    assert result.status is EligibilityStatus.REJECTED


def test_payer_not_supported_http_400_is_unavailable_not_an_answer():
    # Stedi-level rejection (e.g. unsupported payer) with no AAA structure.
    body = {"message": "trading partner is not supported"}
    result = parse_271("99999", json.dumps(body).encode(), http_status=400)
    assert result.status is EligibilityStatus.PAYER_UNAVAILABLE
    assert not result.is_answer


def test_malformed_html_body_fails_closed():
    result = parse_271("62308", b"<html>502 Bad Gateway</html>")
    assert result.status is EligibilityStatus.INDETERMINATE
    assert not result.is_answer
    assert result.raw_271 is None
    assert result.raw_271_sha256  # bytes still digested for the audit trail


def test_empty_2xx_json_fails_closed_never_guesses_active():
    result = parse_271("62308", b"{}")
    assert result.status is EligibilityStatus.INDETERMINATE
    assert not result.is_answer


def test_transport_failure_maps_to_payer_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = make_client(handler).check(dental_request())
    assert result.status is EligibilityStatus.PAYER_UNAVAILABLE
    assert "ConnectError" in result.reason
    assert result.raw_271_bytes is None


def test_raw_bytes_digest_matches_wire_payload():
    import hashlib

    payload = json.dumps(ACTIVE_271).encode()
    result = parse_271("62308", payload)
    assert result.raw_271_bytes == payload
    assert result.raw_271_sha256 == hashlib.sha256(payload).hexdigest()

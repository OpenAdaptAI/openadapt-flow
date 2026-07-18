"""Live smoke test against Stedi's TEST-mode eligibility endpoint.

Env-gated: runs only when ``STEDI_API_KEY`` is set. Use a Stedi TEST-mode
key (self-serve account -> API keys -> Mode: Test); test mode accepts ONLY
Stedi's published mock requests and mock checks are free, so this test
costs $0 and touches no real member data. The request below is Stedi's own
documented DENTAL mock (Cigna, service type code 35, subscriber
Jaguar Dent).

When this passes against the real endpoint, the client graduates from
contract-proven (faithful-fake) to live-proven for the mocked path.
"""

from __future__ import annotations

import os

import pytest

from openadapt_flow.eligibility.artifact import all_confirmed, write_and_verify
from openadapt_flow.eligibility.client import (
    EligibilityRequest,
    EligibilityStatus,
    StediEligibilityClient,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("STEDI_API_KEY"),
    reason=(
        "STEDI_API_KEY not set -- live Stedi smoke skipped. Create a free "
        "Stedi account, generate a TEST-mode API key (mock eligibility "
        "checks are free), and export STEDI_API_KEY to run this."
    ),
)


def test_stedi_dental_mock_roundtrip(tmp_path):
    client = StediEligibilityClient()  # STEDI_API_KEY from the environment
    request = EligibilityRequest(
        payer_id="62308",  # Cigna -- Stedi's documented dental mock
        member_id="U3141592653",
        first_name="Jaguar",
        last_name="Dent",
        date_of_birth="19960505",
        provider_npi="1999999984",
        provider_organization="One",
        service_type_codes=["35"],
    )
    result = client.check(request)
    # The mock catalog returns a real 271; the exact benefits payload is
    # Stedi's to change, so assert the invariants, not the prose.
    assert result.is_answer, result.reason
    assert result.status in (EligibilityStatus.ACTIVE, EligibilityStatus.INACTIVE)
    assert result.raw_271 is not None
    assert result.raw_271_sha256 is not None

    artifact, verdicts = write_and_verify(
        result, tmp_path, member_id="U3141592653", payer="Cigna Dental"
    )
    assert all_confirmed(verdicts), [v.reason for v in verdicts]
    assert artifact.raw_271_file is not None

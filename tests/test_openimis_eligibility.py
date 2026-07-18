"""openIMIS coverage / eligibility-check reference workflow — contract tests.

No docker, no network, no PostgreSQL: these tests pin the COMMITTED surface
of the eligibility demo (`benchmark/openimis_claims/deployment.eligibility.yaml`
+ `scripts/openimis_eligibility_demo.py::eligibility_effects` + the fixture's
oracle SQL) so the deployment YAML, the fixture's ground-truth oracle, and the
bundle's effect contracts can never drift apart. The live loop (record ->
compile -> replay -> SQL-verified verdicts) runs against the pinned stack via
the demo driver, exactly like the claims demo.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from openadapt_flow.deployment import build_effect_verifier, load_deployment
from openadapt_flow.runtime.effects import EffectKind, SqlRecordVerifier
from openadapt_flow.runtime.effects.sql import assert_read_only_sql

REPO_ROOT = Path(__file__).resolve().parents[1]
for entry in (REPO_ROOT / "benchmark", REPO_ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from openimis_claims import fixture as oi  # noqa: E402
from openimis_eligibility_demo import eligibility_effects  # noqa: E402

DEPLOYMENT_YAML = (
    REPO_ROOT / "benchmark" / "openimis_claims" / "deployment.eligibility.yaml"
)


# -- the committed deployment YAML -------------------------------------------


def test_deployment_yaml_wires_the_sql_kit() -> None:
    config = load_deployment(DEPLOYMENT_YAML)
    assert config.effects.kind == "sql"
    assert config.effects.sql_driver == "psycopg"
    assert config.effects.sql_password_env == oi.ORACLE_PASSWORD_ENV
    assert config.effects.sql_connect_args["user"] == oi.ORACLE_ROLE
    assert config.effects.sql_connect_args["port"] == oi.DB_PORT
    assert config.effects.sql_connect_args["host"] == "127.0.0.1"
    # Run-parameter binding is explicit: the probe follows the run's insuree.
    assert config.effects.sql_query_params["insurance_no"].param == "insurance_no"


def test_deployment_query_matches_the_fixture_oracle() -> None:
    """One statement, two readers: the YAML and the fixture cannot drift."""
    config = load_deployment(DEPLOYMENT_YAML)
    normalize = lambda sql: " ".join(sql.split())  # noqa: E731
    assert normalize(config.effects.sql_query) == normalize(oi.ELIGIBILITY_ORACLE_SQL)


def test_deployment_query_is_read_only() -> None:
    config = load_deployment(DEPLOYMENT_YAML)
    assert_read_only_sql(config.effects.sql_query)


def test_verifier_requires_the_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kit convention: a missing secret env var fails LOUD at construction."""
    monkeypatch.delenv(oi.ORACLE_PASSWORD_ENV, raising=False)
    config = load_deployment(DEPLOYMENT_YAML)
    with pytest.raises(ValueError, match=oi.ORACLE_PASSWORD_ENV):
        build_effect_verifier(config.effects, {"insurance_no": oi.POLICYHOLDER_CHF})


def test_verifier_builds_with_secret_and_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The YAML constructs a SqlRecordVerifier bound to the run's insuree.

    A stub DB-API module stands in for psycopg: construction must not open a
    connection (the verifier connects per-read), so the stub proves the
    driver is only imported, never dialed, at build time.
    """
    stub = types.ModuleType("psycopg")

    def _connect(**kwargs: object) -> None:  # pragma: no cover - never dialed
        raise AssertionError("construction must not open a connection")

    stub.connect = _connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", stub)
    monkeypatch.setenv(oi.ORACLE_PASSWORD_ENV, "synthetic-test-secret")
    config = load_deployment(DEPLOYMENT_YAML)
    verifier = build_effect_verifier(config.effects, {"insurance_no": oi.LAPSED_CHF})
    assert isinstance(verifier, SqlRecordVerifier)
    assert verifier.query_params == {"insurance_no": oi.LAPSED_CHF}


def test_verifier_refuses_an_unbound_run_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(oi.ORACLE_PASSWORD_ENV, "synthetic-test-secret")
    config = load_deployment(DEPLOYMENT_YAML)
    with pytest.raises(ValueError, match="insurance_no"):
        build_effect_verifier(config.effects, {})


# -- the bundle's effect contracts -------------------------------------------


def test_eligibility_effects_bind_the_run_param() -> None:
    effects = eligibility_effects()
    assert [e.kind for e in effects] == [
        EffectKind.RECORD_WRITTEN,
        EffectKind.FIELD_EQUALS,
    ]
    for effect in effects:
        assert effect.match["chf_id"].param == "insurance_no"
    exactly_one, coverage_active = effects
    assert exactly_one.expected_count == 1
    assert coverage_active.field == "coverage"
    assert str(coverage_active.value) == "Active"


def test_eligibility_effects_resolve_to_the_checked_policyholder() -> None:
    params = {"insurance_no": oi.SECOND_ACTIVE_CHF}
    for effect in eligibility_effects():
        resolved = effect.resolve(params)
        assert str(resolved.match["chf_id"]) == oi.SECOND_ACTIVE_CHF


# -- the fixture's eligibility scenario --------------------------------------


def test_scenario_constants_are_synthetic_and_distinct() -> None:
    chfs = {oi.POLICYHOLDER_CHF, oi.LAPSED_CHF, oi.SECOND_ACTIVE_CHF}
    assert len(chfs) == 3
    for chf in chfs:
        assert chf.startswith("999"), "synthetic 999* insuree-number range"
    assert oi.POLICY_STATUS_ACTIVE == 2
    assert oi.POLICY_STATUS_EXPIRED == 8


def test_policyholder_sql_template_refuses_suspicious_values() -> None:
    fixture = oi.OpenIMISFixture.__new__(oi.OpenIMISFixture)
    with pytest.raises(oi.FixtureError, match="suspicious"):
        fixture._bootstrap_policyholder(
            chf="999000009",
            last="O'Hara",  # embedded quote must refuse, not inject
            other="Test",
            dob="1990-01-01",
            gender="F",
            address="1 Synthetic Lane",
            enroll="2026-01-01",
            expiry="2027-01-01",
            status=oi.POLICY_STATUS_ACTIVE,
        )


def test_coverage_oracle_refuses_suspicious_insuree_numbers() -> None:
    fixture = oi.OpenIMISFixture.__new__(oi.OpenIMISFixture)
    with pytest.raises(oi.FixtureError, match="suspicious"):
        fixture.coverage("1; DROP TABLE x")

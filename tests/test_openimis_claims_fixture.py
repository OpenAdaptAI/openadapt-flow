"""Static contract tests for the openIMIS claims reference environment.

No Docker, no network: these verify the pinning/fail-closed discipline of
``benchmark/openimis_claims`` — every image digest-pinned, the compose file
refusing unpinned images, loopback-only publishing, synthetic-only fixture
values, and the claim-code limit the openIMIS form enforces.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = REPO_ROOT / "benchmark" / "openimis_claims"

sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from openimis_claims import fixture as oc  # noqa: E402


def test_lock_images_are_digest_pinned() -> None:
    lock = json.loads((BENCH_DIR / "environment.lock.json").read_text())
    assert lock["services"], "lock must pin at least one service image"
    for name, image in lock["services"].items():
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), (
            f"{name} image is not digest-pinned: {image}"
        )


def test_fixture_refuses_unpinned_lock(tmp_path: Path, monkeypatch) -> None:
    lock = json.loads((BENCH_DIR / "environment.lock.json").read_text())
    lock["services"]["backend"] = "ghcr.io/openimis/openimis-be:latest"
    bad_dir = tmp_path / "bench"
    bad_dir.mkdir()
    (bad_dir / "environment.lock.json").write_text(json.dumps(lock))
    monkeypatch.setattr(oc, "HERE", bad_dir)
    with pytest.raises(oc.FixtureError, match="not digest-pinned"):
        oc.OpenIMISFixture(state_dir=tmp_path / "state")


def test_compose_requires_pinned_images_and_binds_loopback() -> None:
    compose = (BENCH_DIR / "compose.yml").read_text()
    for var in (
        "OPENIMIS_BE_IMAGE",
        "OPENIMIS_FE_IMAGE",
        "OPENIMIS_PGSQL_IMAGE",
        "OPENIMIS_REDIS_IMAGE",
        "OPENIMIS_RABBITMQ_IMAGE",
    ):
        assert f"${{{var}:?" in compose, f"compose must fail closed on {var}"
    # No literal image tags: every image comes from a required variable.
    for line in compose.splitlines():
        if "image:" in line:
            assert ":?" in line, f"unpinned literal image line: {line.strip()}"
    # Published ports must bind loopback only.
    for line in compose.splitlines():
        if re.search(r"^\s+- \"?\d|^\s+- \"127", line) and ":80" in line:
            assert "127.0.0.1" in line, f"non-loopback port binding: {line.strip()}"


def test_claim_code_limit_matches_openimis_form() -> None:
    # The openIMIS FE claim-code input accepts at most 8 characters; a longer
    # default would silently truncate at record time.
    assert len(oc.DEFAULT_CLAIM_CODE) <= oc.CLAIM_CODE_MAX_LEN
    assert oc.DEFAULT_CLAIM_CODE.isalnum()


def test_bootstrap_sql_targets_only_the_synthetic_policyholder() -> None:
    sql = oc.OpenIMISFixture.BOOTSTRAP_SQL
    assert oc.POLICYHOLDER_CHF in sql
    # Insurees are inserted, never updated/deleted: the bootstrap must not
    # touch existing demo rows.
    assert "DELETE" not in sql.upper()
    lowered = sql.lower()
    assert lowered.count("update ") == 1  # only the head-of-family back-link
    assert '"tblinsuree" i set "familyid"' in lowered


def test_scenario_values_are_synthetic_fixture_values() -> None:
    assert oc.POLICYHOLDER_CHF == "999000001"
    assert oc.POLICYHOLDER_NAME == "Avery Doe"
    assert oc.HEALTH_FACILITY_CODE == "VIHOS001"
    assert oc.ACTOR_USER == "Admin"  # upstream demo-dataset credential

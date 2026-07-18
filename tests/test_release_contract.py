"""Guard the publication path against version and artifact drift."""

import io
import re
import tarfile
import zipfile
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

from scripts.check_release_consistency import (
    FORBIDDEN_SDIST_PATHS,
    FORBIDDEN_SDIST_PREFIXES,
    REQUIRED_SDIST_PATHS,
    release_versions,
    sync_lock_version,
    validate_sdist_license_boundary,
    validate_wheel_license_boundary,
)

ROOT = Path(__file__).resolve().parents[1]


def test_release_versions_are_synchronized() -> None:
    versions = release_versions()
    assert len(set(versions.values())) == 1, versions


def test_semantic_release_stamps_lock_and_validates_artifacts() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    build_command = pyproject["tool"]["semantic_release"]["build_command"]

    assert "uv==0.11.29" in build_command
    assert "check_release_consistency.py --sync" in build_command
    assert "git add uv.lock" in build_command
    assert "uv build --wheel --sdist" in build_command
    assert "check_release_consistency.py --require-dist" in build_command
    assert "uv lock" not in build_command


def test_lock_sync_updates_only_editable_root_version(tmp_path: Path) -> None:
    (tmp_path / "openadapt_flow").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "openadapt-flow"\nversion = "1.7.1"\n'
    )
    (tmp_path / "openadapt_flow/__init__.py").write_text('__version__ = "1.7.1"\n')
    original_lock = (
        'version = 1\n\n[[package]]\nname = "openadapt-flow"\n'
        'version = "1.7.0"\nsource = { editable = "." }\n'
        '\n[[package]]\nname = "dependency"\nversion = "1.2.3"\n'
    )
    (tmp_path / "uv.lock").write_text(original_lock)

    assert sync_lock_version(tmp_path) == "1.7.1"
    updated_lock = (tmp_path / "uv.lock").read_text()
    assert updated_lock == original_lock.replace(
        'name = "openadapt-flow"\nversion = "1.7.0"',
        'name = "openadapt-flow"\nversion = "1.7.1"',
    )


def test_lock_sync_rejects_source_version_drift(tmp_path: Path) -> None:
    (tmp_path / "openadapt_flow").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "openadapt-flow"\nversion = "1.7.1"\n'
    )
    (tmp_path / "openadapt_flow/__init__.py").write_text('__version__ = "1.7.0"\n')
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "openadapt-flow"\nversion = "1.7.0"\n'
        'source = { editable = "." }\n'
    )

    with pytest.raises(ValueError, match="source versions differ"):
        sync_lock_version(tmp_path)


def test_release_workflow_uses_pinned_actions() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    uses = re.findall(r"^\s*uses:\s+\S+@([^\s#]+)", workflow, flags=re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)
    assert "# v10.6.1" in workflow


def test_auto_release_waits_for_exact_head_ci_before_semantic_release() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()

    wait_index = workflow.index("- name: Wait for exact-head CI")
    release_index = workflow.index("- name: Python Semantic Release")

    assert "actions: read # inspect the exact-head CI run before publishing" in workflow
    assert wait_index < release_index
    assert "actions/workflows/ci.yml/runs" in workflow
    assert '--raw-field head_sha="${GITHUB_SHA}"' in workflow
    assert "--raw-field event=push" in workflow
    assert "select(.head_sha == $sha)" in workflow
    assert 'if [ "$conclusion" = "success" ]' in workflow
    assert "Refusing to publish because exact-head CI concluded" in workflow


def _write_sdist(path: Path, members: set[str]) -> None:
    with tarfile.open(path, mode="w:gz") as archive:
        for relative in sorted(members):
            payload = b"fixture"
            info = tarfile.TarInfo(f"openadapt_flow-test/{relative}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def _write_wheel(path: Path, members: set[str]) -> None:
    with zipfile.ZipFile(path, mode="w") as archive:
        for relative in sorted(members):
            archive.writestr(relative, "fixture")


def test_wheel_requires_mit_license_and_excludes_openimis_surface(
    tmp_path: Path,
) -> None:
    license_path = "openadapt_flow-1.0.dist-info/licenses/LICENSE"
    clean = tmp_path / "clean.whl"
    _write_wheel(clean, {license_path, "openadapt_flow/__init__.py"})
    validate_wheel_license_boundary(clean)

    missing_license = tmp_path / "missing-license.whl"
    _write_wheel(missing_license, {"openadapt_flow/__init__.py"})
    with pytest.raises(ValueError, match="missing the MIT LICENSE"):
        validate_wheel_license_boundary(missing_license)

    for index, forbidden in enumerate(
        (
            "benchmark/openimis_claims/compose.yml",
            "licenses/LICENSE-AGPL-3.0.md",
            "THIRD_PARTY_NOTICES.md",
        )
    ):
        mixed = tmp_path / f"mixed-{index}.whl"
        _write_wheel(mixed, {license_path, forbidden})
        with pytest.raises(ValueError, match="outside the MIT package boundary"):
            validate_wheel_license_boundary(mixed)


def test_sdist_requires_mit_license_and_excludes_openimis_surface(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean.tar.gz"
    _write_sdist(clean, set(REQUIRED_SDIST_PATHS))
    validate_sdist_license_boundary(clean)

    missing_license = tmp_path / "missing-license.tar.gz"
    _write_sdist(missing_license, set())
    with pytest.raises(ValueError, match="missing required files.*LICENSE"):
        validate_sdist_license_boundary(missing_license)

    forbidden_members = {
        *FORBIDDEN_SDIST_PATHS,
        f"{FORBIDDEN_SDIST_PREFIXES[0]}compose.yml",
    }
    for index, forbidden in enumerate(sorted(forbidden_members)):
        mixed = tmp_path / f"mixed-{index}.tar.gz"
        _write_sdist(mixed, {*REQUIRED_SDIST_PATHS, forbidden})
        with pytest.raises(ValueError, match="repository-only benchmark"):
            validate_sdist_license_boundary(mixed)

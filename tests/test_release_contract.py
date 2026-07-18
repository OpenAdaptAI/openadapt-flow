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
    AGPL_CONTENT_SIGNATURES,
    FORBIDDEN_SDIST_PATHS,
    FORBIDDEN_SDIST_PREFIXES,
    REQUIRED_SDIST_PATHS,
    release_versions,
    sync_lock_version,
    validate_distribution_directory,
    validate_sdist_license_boundary,
    validate_wheel_license_boundary,
)

ROOT = Path(__file__).resolve().parents[1]
MIT_LICENSE = (ROOT / "LICENSE").read_bytes()


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


def test_required_wheel_job_builds_and_validates_actual_sdist() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    wheel_job = workflow[workflow.index("\n  wheel:") :]

    assert "python -m build\n" in wheel_job
    assert "python -m build --wheel" not in wheel_job
    assert "python scripts/check_release_consistency.py --require-dist" in wheel_job


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


def test_manual_publish_uses_current_guard_and_exact_target_ci() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    manual = workflow[workflow.index("\n  manual-publish:") :]
    validate_index = manual.index("Independently validate publication artifacts")
    publish_index = manual.index("- name: Publish to PyPI")

    assert "github.ref == 'refs/heads/main'" in manual
    assert "actions: read" in manual
    assert "git merge-base --is-ancestor" in manual
    assert 'head_sha="${TARGET_SHA}"' in manual
    assert "--validate-dist-dir target/dist" in manual
    assert "--license-file target/LICENSE" not in manual
    assert "packages-dir: target/dist/" in manual
    assert validate_index < publish_index


def _metadata_bytes(*, version: str = "1.0", license_name: str = "MIT") -> bytes:
    return (
        f"Metadata-Version: 2.4\n"
        f"Name: openadapt-flow\n"
        f"Version: {version}\n"
        f"License: {license_name}\n"
        f"License-File: LICENSE\n"
        f"\n"
    ).encode()


def _write_sdist(
    path: Path,
    members: set[str],
    *,
    license_bytes: bytes = MIT_LICENSE,
    metadata_bytes: bytes | None = None,
    payloads: dict[str, bytes] | None = None,
) -> None:
    archive_root = path.name.removesuffix(".tar.gz")
    with tarfile.open(path, mode="w:gz") as archive:
        for relative in sorted(members):
            if payloads and relative in payloads:
                payload = payloads[relative]
            elif relative == "LICENSE":
                payload = license_bytes
            elif relative == "PKG-INFO":
                payload = metadata_bytes or _metadata_bytes()
            else:
                payload = b"fixture"
            info = tarfile.TarInfo(f"{archive_root}/{relative}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def _write_wheel(
    path: Path,
    members: set[str],
    *,
    license_bytes: bytes = MIT_LICENSE,
    metadata_bytes: bytes | None = None,
    payloads: dict[str, bytes] | None = None,
) -> None:
    with zipfile.ZipFile(path, mode="w") as archive:
        for relative in sorted(members):
            if payloads and relative in payloads:
                payload = payloads[relative]
            elif relative.endswith(".dist-info/licenses/LICENSE"):
                payload = license_bytes
            elif relative.endswith(".dist-info/METADATA"):
                payload = metadata_bytes or _metadata_bytes()
            else:
                payload = b"fixture"
            archive.writestr(relative, payload)


def test_wheel_requires_mit_license_and_excludes_openimis_surface(
    tmp_path: Path,
) -> None:
    license_path = "openadapt_flow-1.0.dist-info/licenses/LICENSE"
    metadata_path = "openadapt_flow-1.0.dist-info/METADATA"
    clean = tmp_path / "clean.whl"
    _write_wheel(
        clean,
        {license_path, metadata_path, "openadapt_flow/__init__.py"},
    )
    validate_wheel_license_boundary(clean)

    missing_license = tmp_path / "missing-license.whl"
    _write_wheel(
        missing_license,
        {metadata_path, "openadapt_flow/__init__.py"},
    )
    with pytest.raises(ValueError, match="missing the MIT LICENSE"):
        validate_wheel_license_boundary(missing_license)

    changed_license = tmp_path / "changed-license.whl"
    _write_wheel(
        changed_license,
        {license_path, metadata_path},
        license_bytes=b"MIT License\n",
    )
    with pytest.raises(ValueError, match="does not match"):
        validate_wheel_license_boundary(changed_license)

    changed_metadata = tmp_path / "changed-metadata.whl"
    _write_wheel(
        changed_metadata,
        {license_path, metadata_path},
        metadata_bytes=_metadata_bytes(license_name="AGPL-3.0-only"),
    )
    with pytest.raises(ValueError, match="License: MIT"):
        validate_wheel_license_boundary(changed_metadata)

    for index, forbidden in enumerate(
        (
            "benchmark/openimis_claims/compose.yml",
            "licenses/LICENSE-AGPL-3.0.md",
            "THIRD_PARTY_NOTICES.md",
            "fixtures/renamed-compose.yml",
        )
    ):
        mixed = tmp_path / f"mixed-{index}.whl"
        _write_wheel(
            mixed,
            {license_path, metadata_path, forbidden},
            payloads=(
                {forbidden: AGPL_CONTENT_SIGNATURES[0]}
                if forbidden == "fixtures/renamed-compose.yml"
                else None
            ),
        )
        with pytest.raises(ValueError, match="outside the MIT package boundary"):
            validate_wheel_license_boundary(mixed)


def test_sdist_requires_mit_license_and_excludes_openimis_surface(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean.tar.gz"
    clean_members = {*REQUIRED_SDIST_PATHS, "PKG-INFO"}
    _write_sdist(clean, clean_members)
    validate_sdist_license_boundary(clean)

    missing_license = tmp_path / "missing-license.tar.gz"
    _write_sdist(missing_license, {"PKG-INFO"})
    with pytest.raises(ValueError, match="missing required files.*LICENSE"):
        validate_sdist_license_boundary(missing_license)

    changed_license = tmp_path / "changed-license.tar.gz"
    _write_sdist(
        changed_license,
        clean_members,
        license_bytes=b"MIT License\n",
    )
    with pytest.raises(ValueError, match="does not match"):
        validate_sdist_license_boundary(changed_license)

    changed_metadata = tmp_path / "changed-metadata.tar.gz"
    _write_sdist(
        changed_metadata,
        clean_members,
        metadata_bytes=_metadata_bytes(license_name="AGPL-3.0-only"),
    )
    with pytest.raises(ValueError, match="License: MIT"):
        validate_sdist_license_boundary(changed_metadata)

    forbidden_members = {
        *FORBIDDEN_SDIST_PATHS,
        f"{FORBIDDEN_SDIST_PREFIXES[0]}compose.yml",
        "fixtures/openimis_claims/conf/nginx.conf",
        "fixtures/renamed-compose.yml",
    }
    for index, forbidden in enumerate(sorted(forbidden_members)):
        mixed = tmp_path / f"mixed-{index}.tar.gz"
        _write_sdist(
            mixed,
            {*clean_members, forbidden},
            payloads=(
                {forbidden: AGPL_CONTENT_SIGNATURES[0]}
                if forbidden == "fixtures/renamed-compose.yml"
                else None
            ),
        )
        with pytest.raises(ValueError, match="repository-only openIMIS"):
            validate_sdist_license_boundary(mixed)


def test_distribution_directory_refuses_extra_or_multiple_artifacts(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / ".gitignore").write_text("*\n")
    wheel_members = {
        "openadapt_flow-1.0.dist-info/licenses/LICENSE",
        "openadapt_flow-1.0.dist-info/METADATA",
        "openadapt_flow/__init__.py",
    }
    _write_wheel(dist / "openadapt_flow-1.0-py3-none-any.whl", wheel_members)
    _write_sdist(
        dist / "openadapt_flow-1.0.tar.gz",
        {*REQUIRED_SDIST_PATHS, "PKG-INFO"},
    )
    validate_distribution_directory(dist, version="1.0")

    _write_wheel(dist / "openadapt_flow-1.0-cp312-any.whl", wheel_members)
    with pytest.raises(ValueError, match="exactly one wheel and one sdist"):
        validate_distribution_directory(dist, version="1.0")


def test_sdist_refuses_other_roots_traversal_and_duplicates(tmp_path: Path) -> None:
    def write_members(path: Path, names: list[str]) -> None:
        with tarfile.open(path, mode="w:gz") as archive:
            for name in names:
                if name.endswith("/LICENSE"):
                    payload = MIT_LICENSE
                elif name.endswith("/PKG-INFO"):
                    payload = _metadata_bytes()
                else:
                    payload = b"fixture"
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

    other_root = tmp_path / "openadapt_flow-1.0.tar.gz"
    write_members(
        other_root,
        [
            "openadapt_flow-1.0/LICENSE",
            "openadapt_flow-1.0/PKG-INFO",
            "other-root/openimis.yml",
        ],
    )
    with pytest.raises(ValueError, match="outside its single"):
        validate_sdist_license_boundary(other_root)

    traversal = tmp_path / "traversal.tar.gz"
    write_members(traversal, ["traversal/../neutral.yml"])
    with pytest.raises(ValueError, match="non-canonical"):
        validate_sdist_license_boundary(traversal)

    duplicate = tmp_path / "duplicate.tar.gz"
    write_members(duplicate, ["duplicate/LICENSE", "duplicate/LICENSE"])
    with pytest.raises(ValueError, match="duplicate member"):
        validate_sdist_license_boundary(duplicate)


def test_wheel_refuses_traversal_and_duplicates(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.whl"
    _write_wheel(traversal, {"../neutral.py"})
    with pytest.raises(ValueError, match="non-canonical"):
        validate_wheel_license_boundary(traversal)

    duplicate = tmp_path / "duplicate.whl"
    with zipfile.ZipFile(duplicate, mode="w") as archive:
        archive.writestr("neutral.py", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("neutral.py", b"second")
    with pytest.raises(ValueError, match="duplicate member"):
        validate_wheel_license_boundary(duplicate)

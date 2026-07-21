#!/usr/bin/env python3
"""Check that release version sources and built distributions agree."""

from __future__ import annotations

import argparse
import os
import re
import stat
import tarfile
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "openadapt-flow"
DIST_NAME = "openadapt_flow"
FORBIDDEN_DISTRIBUTION_PATH_TOKENS = (
    "openimis",
    "agpl",
    "third_party_notices",
)
AGPL_CONTENT_SIGNATURES = (
    b"SPDX-License-Identifier: " + b"AG" + b"PL-",
    b"GNU " + b"AFFERO GENERAL PUBLIC LICENSE",
)
# Private, deployment-derived hardening artifacts -- the GROWN failure corpus
# from real deployments, the TUNED metamorphic-adversary parameters/weights,
# deployment-derived THRESHOLDS, effect-verification oracle RECIPES, and
# customer/deployment-derived real-EMR datasets -- live ONLY in the private
# OpenAdaptAI/openadapt-corpus repo and must never ride inside an MIT wheel or
# sdist. Reproducible synthetic fixtures and fake-patient public-demo evidence
# remain public; they are mechanisms/samples, not customer-derived data. This
# mirrors the AGPL boundary above: both a path-token check and a content-signature
# check, so a rename cannot smuggle a private artifact in.
PRIVATE_DISTRIBUTION_PATH_TOKENS = (
    "openadapt-corpus",
    "adversary_corpus",
    "identity_roc",
    "grown_corpus",
    "tuned_adversary",
    "deployment_corpus",
    "deployment_thresholds",
    "effect_oracle_recipe",
    "held_out_corpus",
    "oracle_recipe",
    "pixel_verify_cert",
    "real_emr",
)
PRIVATE_DISTRIBUTION_PATH_PREFIXES = ("private/",)
PRIVATE_DISTRIBUTION_EXACT_PATHS = frozenset(
    {
        "tests/test_identity_corpus_rates.py",
        "tests/test_identity_out_of_corpus.py",
    }
)

# This public-web study is not customer-derived, but the complete target list,
# per-target workflows, raw rows, and generated report are high-leverage
# evaluation DATA rather than the engine mechanism. Keep the generic harness
# and bounded aggregate public while refusing the detailed recipes/results.
REPOSITORY_ONLY_EVALUATION_PATH_TOKENS = ("reliability_corpus",)
REPOSITORY_ONLY_EVALUATION_PATH_PREFIXES = (
    "benchmark/reliability/",
    "scripts/reliability/",
)
REPOSITORY_ONLY_EVALUATION_EXACT_PATHS = frozenset(
    {
        "tests/test_reliability.py",
    }
)

# Current-tree guard. Public source retains mechanisms, interfaces, conservative
# defaults, and bounded aggregate evidence. Raw/grown data, tuning sweeps,
# target recipes, and per-target rows must be absent from the public checkout,
# not merely excluded from package archives.
PUBLIC_SOURCE_REPOSITORY_ONLY_PREFIXES = (
    "benchmark/reliability/",
    "scripts/reliability/",
)
PUBLIC_SOURCE_RELIABILITY_ALLOWED_PATHS = frozenset(
    {
        "benchmark/reliability/RELIABILITY.md".lower(),
        "benchmark/reliability/summary.json",
    }
)
PUBLIC_SOURCE_REPOSITORY_ONLY_EXACT_PATHS = frozenset(
    {
        "benchmark/reliability/corpus.json",
        "benchmark/reliability/results.json",
        "tests/test_reliability.py",
    }
)
PUBLIC_SOURCE_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
# Assembled from parts so this guard (which ships in the sdist) does not itself
# trip the content scan; every private-corpus artifact carries the full banner.
PRIVATE_CORPUS_CONTENT_SIGNATURES = (b"OPENADAPT-CORPUS" + b"-PRIVATE-DO-NOT-PACKAGE",)


def _private_distribution_hits(members: set[str], signature_hits: set[str]) -> set[str]:
    """Members that are private deployment-derived hardening artifacts."""
    hits = {
        member
        for member in members
        if member.lower() in PRIVATE_DISTRIBUTION_EXACT_PATHS
        or any(
            member.lower().startswith(prefix)
            for prefix in PRIVATE_DISTRIBUTION_PATH_PREFIXES
        )
        or any(token in member.lower() for token in PRIVATE_DISTRIBUTION_PATH_TOKENS)
    }
    hits.update(signature_hits)
    return hits


def _repository_only_evaluation_hits(members: set[str]) -> set[str]:
    """Members that are public-source evaluation data, not package runtime."""
    return {
        member
        for member in members
        if member.lower() in REPOSITORY_ONLY_EVALUATION_EXACT_PATHS
        or any(
            member.lower().startswith(prefix)
            for prefix in REPOSITORY_ONLY_EVALUATION_PATH_PREFIXES
        )
        or any(
            token in member.lower() for token in REPOSITORY_ONLY_EVALUATION_PATH_TOKENS
        )
    }


def validate_public_source_tree(root: Path = ROOT) -> None:
    """Fail if private data/recipes/tuning re-enter the public checkout."""
    members: set[str] = set()
    for directory, directories, filenames in os.walk(root):
        directories[:] = [
            name
            for name in directories
            if name not in PUBLIC_SOURCE_IGNORED_DIRECTORIES
        ]
        directory_path = Path(directory)
        for filename in filenames:
            members.add((directory_path / filename).relative_to(root).as_posix())

    private = _private_distribution_hits(members, set())
    repository_only = {
        member
        for member in members
        if member.lower() in PUBLIC_SOURCE_REPOSITORY_ONLY_EXACT_PATHS
        or any(
            member.lower().startswith(prefix)
            and member.lower() not in PUBLIC_SOURCE_RELIABILITY_ALLOWED_PATHS
            for prefix in PUBLIC_SOURCE_REPOSITORY_ONLY_PREFIXES
        )
        or any(
            token in member.lower() for token in REPOSITORY_ONLY_EVALUATION_PATH_TOKENS
        )
    }
    forbidden = private | repository_only
    if forbidden:
        raise ValueError(
            "public source tree contains private data, recipes, tuning, or raw "
            f"evaluation artifacts: {sorted(forbidden)}"
        )


LOCK_PACKAGE_PATTERN = re.compile(
    r'(?P<prefix>\[\[package\]\]\nname = "openadapt-flow"\nversion = ")'
    r'[^\"]+(?P<suffix>"\nsource = \{ editable = "\." \})',
    flags=re.MULTILINE,
)
REQUIRED_SDIST_PATHS = frozenset(
    {
        "LICENSE",
    }
)
FORBIDDEN_SDIST_PATHS = frozenset(
    {
        "THIRD_PARTY_NOTICES.md",
        "scripts/openimis_claims_demo.py",
        "tests/test_openimis_claims_fixture.py",
    }
)
FORBIDDEN_SDIST_PREFIXES = ("benchmark/openimis_claims/",)


def _expected_license_bytes(license_file: Path | None = None) -> bytes:
    path = license_file or (ROOT / "LICENSE")
    try:
        value = path.read_bytes()
    except OSError as error:
        raise ValueError(
            f"could not read expected MIT license at {path}: {error}"
        ) from error
    if b"MIT License" not in value or b"Permission is hereby granted" not in value:
        raise ValueError(f"expected license is not the reviewed MIT license: {path}")
    return value


def _validate_package_metadata(
    raw: bytes,
    *,
    source: str,
    expected_version: str | None = None,
) -> str:
    metadata = BytesParser(policy=default).parsebytes(raw)
    if metadata.get("Name") != PACKAGE_NAME:
        raise ValueError(f"{source} package name is not {PACKAGE_NAME!r}")
    version = metadata.get("Version")
    if not version:
        raise ValueError(f"{source} is missing Version metadata")
    if expected_version is not None and version != expected_version:
        raise ValueError(
            f"{source} version {version!r} does not match {expected_version!r}"
        )
    if metadata.get("License") != "MIT":
        raise ValueError(f"{source} does not declare License: MIT")
    license_files = metadata.get_all("License-File", [])
    if license_files != ["LICENSE"]:
        raise ValueError(
            f"{source} must declare exactly License-File: LICENSE; "
            f"found {license_files}"
        )
    return version


def _archive_parts(name: str, *, source: str) -> tuple[str, ...]:
    """Return canonical POSIX archive parts or reject an ambiguous path."""
    if not name or "\\" in name:
        raise ValueError(f"{source} contains a non-canonical member path: {name!r}")
    normalized = name[:-1] if name.endswith("/") else name
    parts = tuple(normalized.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{source} contains a non-canonical member path: {name!r}")
    return parts


def _wheel_members(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, zipfile.ZipInfo], set[str]]:
    by_name: dict[str, zipfile.ZipInfo] = {}
    for member in archive.infolist():
        parts = _archive_parts(member.filename, source="wheel")
        normalized = "/".join(parts)
        if normalized in by_name:
            raise ValueError(f"wheel contains duplicate member: {normalized!r}")
        mode = member.external_attr >> 16
        if stat.S_IFMT(mode) == stat.S_IFLNK:
            raise ValueError(f"wheel contains a symlink member: {normalized!r}")
        by_name[normalized] = member
    return by_name, set(by_name)


def _match(pattern: str, text: str, source: str) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        raise ValueError(f"could not read version from {source}")
    return match.group(1)


def release_versions(root: Path = ROOT) -> dict[str, str]:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    package_init = (root / "openadapt_flow/__init__.py").read_text(encoding="utf-8")
    lock = (root / "uv.lock").read_text(encoding="utf-8")

    return {
        "pyproject.toml": _match(
            r'\[project\]\s+name = "openadapt-flow"\s+version = "([^"]+)"',
            pyproject,
            "pyproject.toml",
        ),
        "openadapt_flow/__init__.py": _match(
            r'^__version__ = "([^"]+)"$',
            package_init,
            "openadapt_flow/__init__.py",
        ),
        "uv.lock": _match(
            r'\[\[package\]\]\s+name = "openadapt-flow"\s+version = "([^"]+)"'
            r'\s+source = \{ editable = "\." \}',
            lock,
            "uv.lock",
        ),
    }


def sync_lock_version(root: Path = ROOT) -> str:
    """Stamp only the editable root version, preserving reviewed resolution."""
    versions = release_versions(root)
    source_versions = {
        versions["pyproject.toml"],
        versions["openadapt_flow/__init__.py"],
    }
    if len(source_versions) != 1:
        raise ValueError(f"release source versions differ: {versions}")
    version = source_versions.pop()

    lock_path = root / "uv.lock"
    lock = lock_path.read_text(encoding="utf-8")
    updated, replacements = LOCK_PACKAGE_PATTERN.subn(
        rf"\g<prefix>{version}\g<suffix>", lock
    )
    if replacements != 1:
        raise ValueError(
            f"expected exactly one editable {PACKAGE_NAME} package in uv.lock, "
            f"found {replacements}"
        )
    lock_path.write_text(updated, encoding="utf-8")

    synchronized = release_versions(root)
    if len(set(synchronized.values())) != 1:
        raise ValueError(f"release versions differ after lock sync: {synchronized}")
    return version


def validate_sdist_license_boundary(
    sdist: Path,
    *,
    expected_license: bytes | None = None,
    expected_version: str | None = None,
) -> str:
    """Require an MIT-only sdist with no openIMIS benchmark material."""
    expected_root = sdist.name.removesuffix(".tar.gz")
    members: set[str] = set()
    archived_license: bytes | None = None
    archived_metadata: bytes | None = None
    signature_hits: set[str] = set()
    private_signature_hits: set[str] = set()
    with tarfile.open(sdist, mode="r:gz") as archive:
        for member in archive:
            parts = _archive_parts(member.name, source="source distribution")
            if parts[0] != expected_root:
                raise ValueError(
                    "source distribution contains a member outside its single "
                    f"{expected_root!r} root: {member.name!r}"
                )
            if len(parts) == 1:
                if not member.isdir():
                    raise ValueError(
                        "source distribution root entry must be a directory: "
                        f"{member.name!r}"
                    )
                continue
            if not (member.isfile() or member.isdir()):
                raise ValueError(
                    "source distribution contains a link/device/special member: "
                    f"{member.name!r}"
                )
            relative = "/".join(parts[1:])
            if relative in members:
                raise ValueError(
                    f"source distribution contains duplicate member: {relative!r}"
                )
            members.add(relative)
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(
                    f"source distribution member could not be read: {relative!r}"
                )
            payload = extracted.read()
            if relative == "LICENSE":
                archived_license = payload
            elif relative == "PKG-INFO":
                archived_metadata = payload
            if any(signature in payload for signature in AGPL_CONTENT_SIGNATURES):
                signature_hits.add(relative)
            if any(
                signature in payload for signature in PRIVATE_CORPUS_CONTENT_SIGNATURES
            ):
                private_signature_hits.add(relative)
    private = _private_distribution_hits(members, private_signature_hits)
    if private:
        raise ValueError(
            "source distribution contains private, deployment-derived hardening "
            "material (grown corpus / tuned adversary / thresholds / oracle "
            "recipes / real-EMR datasets) that belongs only in the private "
            f"OpenAdaptAI/openadapt-corpus repo: {sorted(private)}"
        )
    repository_only = _repository_only_evaluation_hits(members)
    if repository_only:
        raise ValueError(
            "source distribution contains repository-only evaluation data or "
            "recipes that are not part of the distributable engine: "
            f"{sorted(repository_only)}"
        )
    missing = REQUIRED_SDIST_PATHS - members
    if missing:
        raise ValueError(
            f"source distribution is missing required files: {sorted(missing)}"
        )
    reviewed_license = expected_license or _expected_license_bytes()
    if archived_license != reviewed_license:
        raise ValueError(
            "source distribution LICENSE does not match the reviewed root MIT LICENSE"
        )
    if archived_metadata is None:
        raise ValueError("source distribution is missing root PKG-INFO")
    version = _validate_package_metadata(
        archived_metadata,
        source="source distribution PKG-INFO",
        expected_version=expected_version,
    )
    forbidden = set(FORBIDDEN_SDIST_PATHS & members)
    forbidden.update(
        member
        for member in members
        if any(member.startswith(prefix) for prefix in FORBIDDEN_SDIST_PREFIXES)
        or any(token in member.lower() for token in FORBIDDEN_DISTRIBUTION_PATH_TOKENS)
    )
    forbidden.update(signature_hits)
    if forbidden:
        raise ValueError(
            "source distribution contains repository-only openIMIS benchmark "
            f"material outside the MIT package boundary: {sorted(forbidden)}"
        )
    return version


def validate_wheel_license_boundary(
    wheel: Path,
    *,
    expected_license: bytes | None = None,
    expected_version: str | None = None,
) -> str:
    """Require the MIT license and no openIMIS material in the wheel."""
    with zipfile.ZipFile(wheel) as archive:
        member_info, members = _wheel_members(archive)
        license_members = sorted(
            member
            for member in members
            if member.endswith(".dist-info/licenses/LICENSE")
        )
        metadata_members = sorted(
            member for member in members if member.endswith(".dist-info/METADATA")
        )
        archived_license: bytes | None = None
        archived_metadata: bytes | None = None
        signature_hits: set[str] = set()
        private_signature_hits: set[str] = set()
        for name, info in member_info.items():
            if info.is_dir():
                continue
            payload = archive.read(info)
            if name in license_members:
                archived_license = payload
            elif name in metadata_members:
                archived_metadata = payload
            if any(signature in payload for signature in AGPL_CONTENT_SIGNATURES):
                signature_hits.add(name)
            if any(
                signature in payload for signature in PRIVATE_CORPUS_CONTENT_SIGNATURES
            ):
                private_signature_hits.add(name)
    if not license_members:
        raise ValueError("wheel is missing the MIT LICENSE")
    if len(license_members) != 1:
        raise ValueError(f"wheel contains multiple LICENSE files: {license_members}")
    reviewed_license = expected_license or _expected_license_bytes()
    if archived_license != reviewed_license:
        raise ValueError("wheel LICENSE does not match the reviewed root MIT LICENSE")
    if len(metadata_members) != 1 or archived_metadata is None:
        raise ValueError(
            f"wheel must contain exactly one METADATA file: {metadata_members}"
        )
    version = _validate_package_metadata(
        archived_metadata,
        source="wheel METADATA",
        expected_version=expected_version,
    )
    private = _private_distribution_hits(members, private_signature_hits)
    if private:
        raise ValueError(
            "wheel contains private, deployment-derived hardening material "
            "(grown corpus / tuned adversary / thresholds / oracle recipes / "
            "real-EMR datasets) that belongs only in the private "
            f"OpenAdaptAI/openadapt-corpus repo: {sorted(private)}"
        )
    repository_only = _repository_only_evaluation_hits(members)
    if repository_only:
        raise ValueError(
            "wheel contains repository-only evaluation data or recipes that "
            "are not part of the distributable engine: "
            f"{sorted(repository_only)}"
        )
    forbidden = {
        member
        for member in members
        if any(token in member.lower() for token in FORBIDDEN_DISTRIBUTION_PATH_TOKENS)
    }
    forbidden.update(signature_hits)
    if forbidden:
        raise ValueError(
            "wheel contains openIMIS/AGPL material outside the MIT package "
            f"boundary: {sorted(forbidden)}"
        )
    return version


def validate_distribution_directory(
    dist_dir: Path,
    *,
    version: str | None = None,
    license_file: Path | None = None,
) -> tuple[Path, Path]:
    """Validate exactly the two files the publisher is allowed to upload."""
    if not dist_dir.is_dir():
        raise ValueError(f"distribution directory does not exist: {dist_dir}")
    files = sorted(path for path in dist_dir.iterdir() if path.is_file())
    # `uv build` creates this sentinel in custom output directories and the
    # repository keeps the same sentinel in `dist/`; PyPI publishers ignore it.
    artifacts = [path for path in files if path.name != ".gitignore"]
    wheels = [path for path in artifacts if path.name.endswith(".whl")]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    if len(artifacts) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            "distribution directory must contain exactly one wheel and one sdist; "
            f"found {[path.name for path in artifacts]}"
        )
    wheel, sdist = wheels[0], sdists[0]
    reviewed_license = _expected_license_bytes(license_file)
    wheel_version = validate_wheel_license_boundary(
        wheel,
        expected_license=reviewed_license,
        expected_version=version,
    )
    sdist_version = validate_sdist_license_boundary(
        sdist,
        expected_license=reviewed_license,
        expected_version=version,
    )
    if wheel_version != sdist_version:
        raise ValueError(
            f"wheel/sdist metadata versions differ: {wheel_version}, {sdist_version}"
        )
    effective_version = version or wheel_version
    expected_prefix = f"{DIST_NAME}-{effective_version}"
    if not wheel.name.startswith(f"{expected_prefix}-") or sdist.name != (
        f"{expected_prefix}.tar.gz"
    ):
        raise ValueError(
            f"distribution filenames do not match version {effective_version}: "
            f"{wheel.name}, {sdist.name}"
        )
    return wheel, sdist


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--require-dist", action="store_true")
    parser.add_argument(
        "--validate-dist-dir",
        type=Path,
        help="validate an external build directory without trusting its source scripts",
    )
    parser.add_argument(
        "--license-file",
        type=Path,
        help="reviewed root MIT LICENSE for --validate-dist-dir",
    )
    args = parser.parse_args()

    if args.validate_dist_dir is not None:
        if args.sync or args.require_dist:
            parser.error(
                "--validate-dist-dir cannot be combined with --sync/--require-dist"
            )
        try:
            wheel, sdist = validate_distribution_directory(
                args.validate_dist_dir,
                license_file=args.license_file,
            )
        except ValueError as error:
            parser.error(str(error))
        print(f"Distribution license boundary passed: {wheel.name}, {sdist.name}")
        return 0
    if args.license_file is not None:
        parser.error("--license-file requires --validate-dist-dir")

    if args.sync:
        sync_lock_version()

    try:
        validate_public_source_tree()
    except ValueError as error:
        parser.error(str(error))

    versions = release_versions()
    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        parser.error(f"release versions differ: {versions}")
    version = unique_versions.pop()

    if args.require_dist:
        try:
            validate_distribution_directory(ROOT / "dist", version=version)
        except ValueError as error:
            parser.error(str(error))

    print(
        f"Release version {version} is synchronized across project, module, and lock."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

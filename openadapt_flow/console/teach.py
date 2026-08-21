"""Opaque local correction discovery for the console Teach action.

An operator records a correction under ``<run>/teach-demonstrations`` with the
existing recorder, or puts a reviewed correction-spec JSON file there.  The
browser receives only content-derived opaque ids.  It never supplies or learns
a filesystem path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.repair.teach import (
    TeachRepairResult,
    correction_digest,
    create_teach_repair_candidate,
)

TEACH_DEMONSTRATIONS_DIR = "teach-demonstrations"
_CANDIDATES_DIR = ".repair-candidates"
_STORE_DIR = ".repair-store"


class TeachDemonstration(BaseModel):
    """A browser-safe reference to one local correction."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-f0-9]{24}$")
    kind: Literal["recording", "correction_spec"]
    label: str = Field(pattern=r"^Correction demonstration [1-9][0-9]*$")


def _kind(path: Path) -> Optional[Literal["recording", "correction_spec"]]:
    if path.is_symlink():
        return None
    if path.is_file() and path.suffix.lower() == ".json":
        return "correction_spec"
    if path.is_dir():
        events = path / "events.jsonl"
        if events.is_file() and not events.is_symlink():
            return "recording"
    return None


def list_teach_demonstrations(run_dir: Path | str) -> list[TeachDemonstration]:
    root = Path(run_dir) / TEACH_DEMONSTRATIONS_DIR
    if root.is_symlink() or not root.is_dir():
        return []
    found: list[tuple[str, Literal["recording", "correction_spec"]]] = []
    seen: set[str] = set()
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    for child in children:
        kind = _kind(child)
        if kind is None:
            continue
        try:
            identifier = correction_digest(child)[:24]
        except (OSError, ValueError):
            continue
        if identifier in seen:
            continue
        seen.add(identifier)
        found.append((identifier, kind))
    return [
        TeachDemonstration(
            id=identifier,
            kind=kind,
            label=f"Correction demonstration {index}",
        )
        for index, (identifier, kind) in enumerate(found, start=1)
    ]


def resolve_teach_demonstration(
    run_dir: Path | str, demonstration_id: str
) -> Optional[Path]:
    if not isinstance(demonstration_id, str) or len(demonstration_id) != 24:
        return None
    root = Path(run_dir) / TEACH_DEMONSTRATIONS_DIR
    if root.is_symlink() or not root.is_dir():
        return None
    for child in root.iterdir():
        if _kind(child) is None:
            continue
        try:
            if correction_digest(child)[:24] == demonstration_id:
                resolved = child.resolve(strict=True)
                resolved.relative_to(root.resolve(strict=True))
                return resolved
        except (OSError, ValueError):
            continue
    return None


def create_candidate_from_demonstration(
    run_dir: Path,
    demonstration: Path,
    prior_bundle: Path,
    *,
    bundles_root: Path,
    policy_name: str,
) -> TeachRepairResult:
    """Run the repair Teach seam with console-owned local store locations."""
    return create_teach_repair_candidate(
        run_dir,
        demonstration,
        prior_bundle,
        candidates_root=run_dir / _CANDIDATES_DIR,
        repair_store=bundles_root / _STORE_DIR,
        policy_name=policy_name,
    )


__all__ = [
    "TEACH_DEMONSTRATIONS_DIR",
    "TeachDemonstration",
    "create_candidate_from_demonstration",
    "list_teach_demonstrations",
    "resolve_teach_demonstration",
]

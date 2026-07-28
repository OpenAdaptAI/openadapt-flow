#!/usr/bin/env python3
"""Classify a CI diff without making an ambiguous change look paper-only."""

from __future__ import annotations

import argparse
import os
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class Classification:
    paper_only: bool
    reason: str

    @property
    def run_runtime(self) -> bool:
        return not self.paper_only


def classify_paths(paths: list[str]) -> Classification:
    """Use the light lane only for a nonempty, normalized ``paper/**`` diff."""

    if not paths:
        return Classification(False, "empty or unavailable diff")

    for raw_path in paths:
        path = PurePosixPath(raw_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.parts) < 2
            or path.parts[0] != "paper"
        ):
            return Classification(False, "diff includes a path outside paper/")

    return Classification(True, f"all {len(paths)} changed paths are under paper/")


def changed_paths(base: str, head: str) -> list[str]:
    """Return the pull-request paths from its merge base, without rename folding."""

    merge_base = subprocess.run(
        ["git", "merge-base", base, head],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if not merge_base:
        raise subprocess.CalledProcessError(1, ["git", "merge-base", base, head])

    completed = subprocess.run(
        [
            "git",
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            merge_base,
            head,
            "--",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [
        path.decode("utf-8", errors="surrogateescape")
        for path in completed.stdout.split(b"\0")
        if path
    ]


def classify_event(event_name: str, base: str, head: str) -> Classification:
    """Classify PR diffs; exact-main, scheduled, and manual runs stay complete."""

    if event_name != "pull_request":
        return Classification(False, f"{event_name} requires full qualification")
    if not base or not head or set(base) == {"0"}:
        return Classification(False, "missing or unusable comparison SHA")

    try:
        return classify_paths(changed_paths(base, head))
    except (OSError, subprocess.CalledProcessError) as exc:
        return Classification(False, f"diff classification failed: {exc}")


def _write_output(path: str, classification: Classification) -> None:
    with open(path, "a", encoding="utf-8") as output:
        output.write(f"paper_only={str(classification.paper_only).lower()}\n")
        output.write(f"run_runtime={str(classification.run_runtime).lower()}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    args = parser.parse_args()

    classification = classify_event(args.event, args.base, args.head)
    print(
        f"paper_only={str(classification.paper_only).lower()} "
        f"run_runtime={str(classification.run_runtime).lower()} "
        f"reason={classification.reason}"
    )
    if args.github_output:
        _write_output(args.github_output, classification)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

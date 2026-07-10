"""Arm-independent ground-truth verdicts for the competitor drift study.

Reads the state-event log written by ``mockmed_study_server.py`` and
classifies the run using the same semantics as
``docs/validation/VALIDATION.md`` / ``tests/e2e/validation_utils.py``:

- The recorded target is patient ``p1`` (Jane Sample) -- the FIRST referral
  row at record time. Every drift mode attacks that selection, and ground
  truth stays ``p1`` (under ``missing`` the target is gone, so ANY write is
  a wrong action).
- ``success``      -- exactly one encounter written, to p1, type Triage,
                      note == this run's note.
- ``wrong_action`` -- anything written to a patient other than p1, or a
                      write to p1 with the wrong/empty note or wrong type.
- ``no_write``     -- nothing written to any patient. Combined with the
                      tool's own claim this is either a safe-halt (tool
                      reported failure) or a false success (tool reported
                      success but wrote nothing).

The tool's self-reported outcome is recorded separately and NEVER trusted
for the final-state verdict. ``silent`` marks a wrong action where the tool
claimed success.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

TARGET_PATIENT = "p1"
TARGET_TYPE = "Triage"


@dataclass
class RunVerdict:
    verdict: str  # success | wrong_action | no_write
    writes: list[dict] = field(default_factory=list)  # distinct writes seen
    detail: str = ""
    hashes: list[str] = field(default_factory=list)  # navigation trace

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "writes": self.writes,
            "detail": self.detail,
            "hashes": self.hashes,
        }


def read_events(state_file: Path) -> list[dict]:
    events = []
    if state_file.exists():
        for line in state_file.read_text().splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def classify(state_file: Path, expected_note: str) -> RunVerdict:
    """Classify a run's final state against the expected note."""
    events = read_events(state_file)
    writes: list[dict] = []
    seen = set()
    hashes: list[str] = []
    for ev in events:
        h = ev.get("hash")
        if h and (not hashes or hashes[-1] != h):
            hashes.append(h)
        for pid, encs in (ev.get("encounters") or {}).items():
            for enc in encs:
                key = (pid, enc.get("type"), enc.get("note"))
                if key not in seen:
                    seen.add(key)
                    writes.append(
                        {"patient": pid, "type": enc.get("type"),
                         "note": enc.get("note")}
                    )

    if not writes:
        return RunVerdict("no_write", [], "no encounter written", hashes)

    wrong = []
    for w in writes:
        if w["patient"] != TARGET_PATIENT:
            wrong.append(f"wrote to WRONG patient {w['patient']!r}")
        elif w["type"] != TARGET_TYPE:
            wrong.append(f"wrong type {w['type']!r} on {w['patient']}")
        elif (w["note"] or "") != expected_note:
            wrong.append(
                f"wrong/empty note {w['note']!r} on {w['patient']} "
                f"(expected {expected_note!r})"
            )
    if wrong:
        return RunVerdict("wrong_action", writes, "; ".join(wrong), hashes)
    if len(writes) > 1:
        return RunVerdict(
            "wrong_action", writes, "duplicate encounters written", hashes
        )
    return RunVerdict("success", writes, "right patient, type, note", hashes)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_file", type=Path)
    parser.add_argument("--note", required=True)
    args = parser.parse_args()
    print(json.dumps(classify(args.state_file, args.note).to_dict(), indent=2))


if __name__ == "__main__":
    main()

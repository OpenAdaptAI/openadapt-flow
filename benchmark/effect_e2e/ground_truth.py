"""Independent ground truth: read the sqlite FILE directly, judge every table.

This is path (c) of the harness -- the privileged judge of what ACTUALLY
persisted. It is deliberately independent of both oracles:

- It bypasses the record service entirely and opens the sqlite file on its OWN
  read-only connection (``mode=ro``). A bug or lie in the service's read
  handler cannot fool it.
- It reads EVERY mutable surface (encounters AND billing), not just the
  encounters surface the ``/api/records`` effect oracle covers -- so it can see
  a collateral write the out-of-band record oracle structurally cannot.
- It classifies the business effect with its OWN before/after row logic; it
  does NOT restate the effect verifier's typed ``Effect`` contract. The
  cross-table delta uses the kit's frozen :func:`audit_table_deltas` (the
  contract the governed Frappe Lending matrix proved), a DIFFERENT code path
  from the REST verifier's ``judge_records``.

The write's HTTP success flag never reaches this judge; it looks only at rows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmark.effect_e2e.record_service import TARGET_PATIENT, TARGET_TYPE
from openadapt_flow.runtime.effects.sql import audit_table_deltas

_AUDITED_TABLES = ("encounters", "billing")


@dataclass(frozen=True)
class GroundTruth:
    """The independent verdict on what a run actually persisted.

    Attributes:
        correct: True iff EXACTLY the intended write landed -- one target
            encounter with the intended note, no pre-existing row destroyed,
            and no collateral change on any other audited surface.
        fault_class: ``"correct"`` or the specific wrong-effect class
            (``absent`` / ``partial`` / ``duplicate`` / ``wrong_record`` /
            ``collateral_loss`` / ``collateral_write``).
        table_deltas: Per-table row-count delta across the write (audit trail).
    """

    correct: bool
    fault_class: str
    detail: str
    table_deltas: dict[str, int] = field(default_factory=dict)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_counts(db_path: Path) -> dict[str, int]:
    conn = _connect_ro(db_path)
    try:
        return {
            name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in _AUDITED_TABLES
        }
    finally:
        conn.close()


def read_encounters(db_path: Path) -> list[dict[str, Any]]:
    """Direct read-only snapshot of the encounters surface (independent path)."""
    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(
            "SELECT id, patient_id, type, note, source, key FROM encounters ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@dataclass(frozen=True)
class Snapshot:
    """A before/after snapshot pair taken directly from the sqlite file."""

    encounters: list[dict[str, Any]]
    counts: dict[str, int]


def capture(db_path: Path) -> Snapshot:
    """Snapshot every audited surface directly from the file (no service)."""
    return Snapshot(encounters=read_encounters(db_path), counts=_table_counts(db_path))


def judge(before: Snapshot, after: Snapshot, *, intended_note: str) -> GroundTruth:
    """Judge the business effect from direct before/after file snapshots.

    The intended effect is EXACTLY one new ``p1`` / ``Triage`` encounter
    carrying ``intended_note``, with no pre-existing row destroyed and no
    change to any other audited surface.
    """
    # Cross-table delta contract: encounters may move by +1 (our write); every
    # other audited surface must move by exactly 0. audit_table_deltas returns
    # the violations plus the full delta map.
    _violations, deltas = audit_table_deltas(
        before.counts, after.counts, {"encounters": 1}
    )

    before_ids = {r["id"] for r in before.encounters}
    after_ids = {r["id"] for r in after.encounters}
    lost = [r for r in before.encounters if r["id"] not in after_ids]
    if lost:
        return GroundTruth(
            False,
            "collateral_loss",
            f"{len(lost)} pre-existing encounter row(s) destroyed (lost update)",
            deltas,
        )

    new_rows = [r for r in after.encounters if r["id"] not in before_ids]
    target_new = [
        r
        for r in new_rows
        if str(r["patient_id"]) == TARGET_PATIENT and str(r["type"]) == TARGET_TYPE
    ]
    stray_new = [r for r in new_rows if r not in target_new]

    if not target_new:
        if stray_new:
            return GroundTruth(
                False,
                "wrong_record",
                "no target encounter; the write landed on the wrong record "
                f"(patient {stray_new[0]['patient_id']!r})",
                deltas,
            )
        return GroundTruth(
            False, "absent", "no target encounter persisted (phantom write)", deltas
        )
    if len(target_new) > 1:
        return GroundTruth(
            False,
            "duplicate",
            f"{len(target_new)} target encounters persisted; exactly one intended",
            deltas,
        )
    if str(target_new[0]["note"]) != intended_note:
        return GroundTruth(
            False,
            "partial",
            "target encounter persisted with a wrong/dropped note field",
            deltas,
        )

    # The target row is exactly correct. Now the surfaces the encounters-scoped
    # oracle cannot see: a stray encounter to another patient, or any nonzero
    # delta on another audited table (e.g. billing) -- a collateral write.
    billing_delta = deltas.get("billing", 0)
    if billing_delta != 0:
        return GroundTruth(
            False,
            "collateral_write",
            "target encounter is correct, but a collateral row was written to "
            f"the unaudited billing surface (billing delta {billing_delta:+d})",
            deltas,
        )
    if stray_new:
        return GroundTruth(
            False,
            "collateral_write",
            "target encounter is correct, but a stray encounter row was also written",
            deltas,
        )
    return GroundTruth(True, "correct", "exactly one correct target encounter", deltas)

"""Independent read-only SQLite ground truth for the MockLoan SWER study.

The product arms read REST and classify through the runtime effect kit. This
module does neither: it opens the persisted SQLite file read-only, discovers
every business table from ``sqlite_master``, computes its own before/after table
deltas, and classifies the trial's rows with benchmark-local logic. Metadata and
SQLite-internal tables are excluded because they are harness bookkeeping, not
business-effect surfaces.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    Verdict,
)

_EXCLUDED_TABLES = frozenset({"metadata"})


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _quote_identifier(name: str) -> str:
    return f'"{name.replace(chr(34), chr(34) * 2)}"'


def audited_tables(database_path: Path) -> tuple[str, ...]:
    """Discover every persisted business table, excluding harness metadata."""
    with _connect_read_only(database_path) as conn:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
    return tuple(name for name in names if name not in _EXCLUDED_TABLES)


def _table_counts(database_path: Path, tables: tuple[str, ...]) -> dict[str, int]:
    with _connect_read_only(database_path) as conn:
        return {
            table: int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()[0]
            )
            for table in tables
        }


def _read_records(database_path: Path) -> list[dict[str, Any]]:
    with _connect_read_only(database_path) as conn:
        rows = conn.execute(
            """
            SELECT id, loan_id, product, amount, memo, source,
                   record_key AS key, surface
            FROM records
            ORDER BY id
            """
        ).fetchall()
    return [dict(row) for row in rows]


@dataclass(frozen=True)
class Snapshot:
    records: list[dict[str, Any]]
    table_counts: dict[str, int]


def capture(database_path: Path) -> Snapshot:
    tables = audited_tables(database_path)
    return Snapshot(
        records=_read_records(database_path),
        table_counts=_table_counts(database_path, tables),
    )


def _table_deltas(before: Snapshot, after: Snapshot) -> dict[str, int]:
    return {
        table: after.table_counts.get(table, 0) - before.table_counts.get(table, 0)
        for table in sorted(set(before.table_counts) | set(after.table_counts))
    }


@dataclass(frozen=True)
class GroundTruth:
    correct: bool
    fault_class: str
    detail: str
    persisted_count: int
    table_deltas: dict[str, int] = field(default_factory=dict)


def judge(
    before: Snapshot,
    after: Snapshot,
    *,
    intended_loan: str,
    intended_product: str,
    intended_amount: str,
    intended_memo: str,
) -> GroundTruth:
    """Independently classify exactly one intended disbursement effect."""
    deltas = _table_deltas(before, after)
    before_ids = {int(row["id"]) for row in before.records}
    after_ids = {int(row["id"]) for row in after.records}
    lost = [row for row in before.records if int(row["id"]) not in after_ids]
    new_rows = [row for row in after.records if int(row["id"]) not in before_ids]

    if lost:
        return GroundTruth(
            False,
            "collateral_loss",
            f"{len(lost)} pre-existing business row(s) were destroyed",
            max(1, len(new_rows)),
            deltas,
        )

    intended_identity = [
        row
        for row in new_rows
        if str(row["loan_id"]) == intended_loan
        and str(row["product"]) == intended_product
        and str(row["surface"]) == "disbursements"
    ]
    if not intended_identity:
        collateral_tables = {
            table: delta
            for table, delta in deltas.items()
            if table != "records" and delta != 0
        }
        if collateral_tables:
            return GroundTruth(
                False,
                "collateral_write",
                "a discovered business table changed without the intended "
                f"disbursement: {collateral_tables}",
                max(1, sum(abs(delta) for delta in collateral_tables.values())),
                deltas,
            )
        if new_rows:
            trial_rows = [row for row in new_rows if str(row["memo"]) == intended_memo]
            wrong = trial_rows or new_rows
            return GroundTruth(
                False,
                "wrong_record",
                "the trial write persisted, but not on the intended loan surface",
                len(wrong),
                deltas,
            )
        return GroundTruth(False, "absent", "no business row persisted", 0, deltas)

    if len(intended_identity) > 1:
        return GroundTruth(
            False,
            "duplicate",
            f"{len(intended_identity)} intended disbursements persisted",
            len(intended_identity),
            deltas,
        )

    intended = intended_identity[0]
    if (
        str(intended["amount"]) != intended_amount
        or str(intended["memo"]) != intended_memo
    ):
        return GroundTruth(
            False,
            "partial",
            "the intended row persisted with a wrong or dropped field",
            1,
            deltas,
        )

    collateral_rows = [row for row in new_rows if int(row["id"]) != int(intended["id"])]
    collateral_tables = {
        table: delta
        for table, delta in deltas.items()
        if table != "records" and delta != 0
    }
    if collateral_rows or collateral_tables:
        return GroundTruth(
            False,
            "collateral_write",
            "the intended row persisted with an additional business effect",
            1 + len(collateral_rows),
            deltas,
        )

    if deltas.get("records") != 1:
        return GroundTruth(
            False,
            "unexpected_delta",
            f"records changed by {deltas.get('records', 0):+d}, expected +1",
            max(1, len(new_rows)),
            deltas,
        )
    return GroundTruth(True, "correct", "exactly one intended row persisted", 1, deltas)


class SQLiteGroundTruthVerifier:
    """EffectVerifier adapter over the benchmark-local independent judge."""

    substrate = "sqlite_ground_truth"

    def __init__(
        self,
        database_path: Path,
        *,
        intended_loan: str,
        intended_product: str,
        intended_amount: str,
        intended_memo: str,
    ) -> None:
        self.database_path = database_path
        self.intended_loan = intended_loan
        self.intended_product = intended_product
        self.intended_amount = intended_amount
        self.intended_memo = intended_memo
        self._before: Optional[Snapshot] = None

    def capture_pre_state(self, context: Any = None) -> EffectState:
        try:
            self._before = capture(self.database_path)
        except (OSError, sqlite3.Error):
            self._before = None
        return EffectState(
            substrate=self.substrate,
            reachable=self._before is not None,
            detail={
                "tables": sorted(self._before.table_counts) if self._before else []
            },
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        if not before.reachable or self._before is None:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason="the independent SQLite pre-state was unreadable",
            )
        try:
            after = capture(self.database_path)
        except (OSError, sqlite3.Error):
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason="the independent SQLite post-state was unreadable",
            )
        truth = judge(
            self._before,
            after,
            intended_loan=self.intended_loan,
            intended_product=self.intended_product,
            intended_amount=self.intended_amount,
            intended_memo=self.intended_memo,
        )
        return EffectVerdict(
            verdict=Verdict.CONFIRMED if truth.correct else Verdict.REFUTED,
            kind=expected.kind,
            substrate=self.substrate,
            reason=(
                f"independent SQLite ground truth: {truth.fault_class}: "
                f"{truth.detail}; table_deltas={truth.table_deltas}"
            ),
            observed_count=truth.persisted_count,
            expected_count=1,
        )

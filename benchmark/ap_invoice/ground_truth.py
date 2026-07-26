"""Independent ground truth for the AP invoice benchmark.

Bypasses BOTH fixture services entirely: opens the ERP SQLite file directly
(read-only, its own connection) and lists/reads the OUTBOX maildir directly
from disk. No HTTP status, banner row, or verifier verdict ever reaches this
module; it re-derives the intended end state from the worklist rows with its
OWN logic and reports INVARIANT VIOLATIONS (wrong business state), plus a
full per-table delta audit so an unexpected write anywhere in the database is
caught even when every targeted check passes.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmark.ap_invoice.fixtures import ADJACENT_AMOUNT, ADJACENT_INVOICE
from benchmark.multiapp_common import read_maildir_messages

#: The app's own echo surface; excluded from the delta audit on purpose (it is
#: the thing a screen-echo oracle reads, not part of the business state).
_ECHO_TABLES = frozenset({"banner"})


@dataclass
class Snapshot:
    tables: dict[str, list[dict[str, Any]]]
    outbox: dict[str, str]


@dataclass
class Verdict:
    violations: list[str] = field(default_factory=list)
    table_deltas: dict[str, int] = field(default_factory=dict)

    @property
    def correct(self) -> bool:
        return not self.violations


def capture(db_path: Path, outbox: Path) -> Snapshot:
    """Direct read-only snapshot of every persisted table + the outbox."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        names = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        tables = {
            name: [dict(r) for r in conn.execute(f"SELECT * FROM {name}").fetchall()]
            for name in names
        }
    finally:
        conn.close()
    outbox_state = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in read_maildir_messages(outbox).items()
    }
    return Snapshot(tables=tables, outbox=outbox_state)


def _rows(snapshot: Snapshot, table: str, **where: str) -> list[dict[str, Any]]:
    return [
        row
        for row in snapshot.tables.get(table, [])
        if all(str(row.get(k)) == v for k, v in where.items())
    ]


def _mail_for(snapshot: Snapshot, outbox_dir: Path, name: str, invoice_id: str) -> bool:
    if name not in snapshot.outbox:
        return False
    for candidate, data in read_maildir_messages(outbox_dir).items():
        if candidate == name:
            return f"X-OpenAdapt-Invoice: {invoice_id}".encode() in data
    return False


def judge(
    scenario: str,
    rows: list[dict[str, str]],
    before: Snapshot,
    after: Snapshot,
    *,
    outbox_dir: Path,
    completed: bool,
) -> Verdict:
    """Classify the persisted end state against the scenario's invariants.

    ``completed`` is whether the arm REPORTED a completed run; it selects
    which completeness invariants apply (a safe halt legitimately leaves the
    flow unfinished; it must never leave the state WRONG).
    """
    verdict = Verdict()
    verdict.table_deltas = {
        name: len(after.tables.get(name, [])) - len(before.tables.get(name, []))
        for name in sorted(set(before.tables) | set(after.tables))
        if name not in _ECHO_TABLES
    }

    # -- invariants that must hold in EVERY scenario and arm -----------------
    adjacent = _rows(after, "invoices", invoice_id=ADJACENT_INVOICE)
    if len(adjacent) != 1:
        verdict.violations.append("adjacent_invoice_missing_or_duplicated")
    elif (
        adjacent[0]["status"] != "draft"
        or str(adjacent[0]["amount"]) != ADJACENT_AMOUNT
        or str(adjacent[0]["amount_payable"]) != ""
    ):
        verdict.violations.append("adjacent_invoice_modified")

    for row in rows:
        payments = _rows(after, "payments", invoice_id=row["invoice_id"])
        if len(payments) > 1:
            verdict.violations.append(f"duplicate_payment:{row['invoice_id']}")
        invoices = _rows(after, "invoices", invoice_id=row["invoice_id"])
        if scenario == "duplicate_invoice":
            if len(invoices) != 1:
                verdict.violations.append(
                    f"ambiguous_duplicate_persisted:{row['invoice_id']}"
                )
        elif len(invoices) > 1:
            verdict.violations.append(f"duplicate_invoice_rows:{row['invoice_id']}")
        if scenario == "missing_po" and invoices:
            verdict.violations.append(
                f"invoice_entered_against_missing_po:{row['invoice_id']}"
            )
        for payment in payments:
            if str(payment["amount"]) != row["amount_payable"]:
                verdict.violations.append(f"payment_amount_wrong:{row['invoice_id']}")
        if row["route"] == "mismatch" and payments:
            verdict.violations.append(f"mismatched_invoice_paid:{row['invoice_id']}")

    # -- completeness invariants: only a COMPLETED run owes the end state ----
    if completed:
        for row in rows:
            invoice_id = row["invoice_id"]
            invoices = _rows(after, "invoices", invoice_id=invoice_id)
            if len(invoices) != 1:
                verdict.violations.append(f"invoice_not_entered_once:{invoice_id}")
                continue
            invoice = invoices[0]
            if str(invoice["doc_sha256"]) != row["doc_sha256"]:
                verdict.violations.append(f"document_digest_wrong:{invoice_id}")
            if row["route"] == "ok":
                if invoice["status"] != "approved":
                    verdict.violations.append(f"invoice_not_approved:{invoice_id}")
                if str(invoice["amount_payable"]) != row["amount_payable"]:
                    verdict.violations.append(f"payable_amount_wrong:{invoice_id}")
                if len(_rows(after, "payments", invoice_id=invoice_id)) != 1:
                    verdict.violations.append(f"payment_missing:{invoice_id}")
            else:
                if invoice["status"] != "held":
                    verdict.violations.append(f"mismatch_not_held:{invoice_id}")
                if len(_rows(after, "ap_exceptions", invoice_id=invoice_id)) != 1:
                    verdict.violations.append(f"exception_entry_missing:{invoice_id}")
            if not _mail_for(after, outbox_dir, row["mail_name"], invoice_id):
                verdict.violations.append(f"vendor_email_missing:{invoice_id}")
        batches = after.tables.get("batches", [])
        if len(batches) != 1 or str(batches[0].get("processed")) != str(len(rows)):
            verdict.violations.append("batch_completion_wrong")

    return verdict

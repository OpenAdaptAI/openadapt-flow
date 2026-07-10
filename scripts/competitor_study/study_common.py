"""Shared constants and helpers for the competitor drift study.

The canonical task (identical to the openadapt-flow validation benchmark,
``docs/validation/VALIDATION.md``): sign in to MockMed as
``nurse.demo`` / ``mockmed-demo-pass``, open the FIRST referral task in the
list (Jane Sample / ``p1`` at record time -- the row every drift mode
attacks), create a New Encounter of type Triage, enter a distinct note, and
Save Encounter.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

STUDY_PORT = 8765
STUDY_URL = f"http://127.0.0.1:{STUDY_PORT}/"
USERNAME = "nurse.demo"
PASSWORD = "mockmed-demo-pass"

# Drift modes for the main-task matrix. ``sort`` reorder drift exists only on
# the MockMed widgets page (?presort=desc, Track C) and is out of scope for
# the main referral task -- documented in COMPETITOR_STUDY.md.
DRIFT_MODES = ["", "lookalike", "missing", "grow", "theme", "rename", "move"]

# The user-goal handed to LLM-based workflow builders, verbatim. This is the
# canonical task statement from the benchmark. Fairness caveat (see
# COMPETITOR_STUDY.md methodology): the phrase "first referral task" is how
# the benchmark defines the task; ground truth for every drift replay remains
# the RECORDED patient (p1), because data drift arriving between runs must
# not silently redirect a recorded clinical workflow to a different patient.
USER_GOAL = (
    "Sign in to MockMed with username nurse.demo and password "
    "mockmed-demo-pass, open the first referral task in the Referral Tasks "
    "list, click New Encounter, select encounter type Triage, type the "
    "encounter note (the note text is the workflow's input parameter), and "
    "click Save Encounter."
)

# claude-sonnet-5 list prices (USD per token), per Anthropic pricing.
PRICE_IN = 3.00 / 1_000_000
PRICE_OUT = 15.00 / 1_000_000
# Conservative fallback when a tool reports no usage: 8K input + 500 output
# per call, screenshots ~1.5K tokens each (study budget protocol).
FALLBACK_CALL_IN_TOKENS = 8_000
FALLBACK_CALL_OUT_TOKENS = 500

BUDGET_ABORT_USD = 8.00
BUDGET_CAP_USD = 10.00


def run_note(tag: str) -> str:
    """A note string unique to one run (ground truth checks exact match)."""
    return f"competitor-study {tag} {time.strftime('%Y%m%dT%H%M%S')}"


class SpendLedger:
    """Append-only LLM spend log with a running total (list prices)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def total(self) -> float:
        total = 0.0
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    total += json.loads(line)["usd"]
        return total

    def add(
        self,
        phase: str,
        calls: int,
        in_tokens: int | None,
        out_tokens: int | None,
        note: str = "",
    ) -> float:
        if in_tokens is None or out_tokens is None:
            in_tokens = calls * FALLBACK_CALL_IN_TOKENS
            out_tokens = calls * FALLBACK_CALL_OUT_TOKENS
            note = (note + " [estimated]").strip()
        usd = in_tokens * PRICE_IN + out_tokens * PRICE_OUT
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "phase": phase,
            "calls": calls,
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
            "usd": round(usd, 6),
            "note": note,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        total = self.total()
        print(f"[spend] +${usd:.4f} ({phase}) running total ${total:.4f}")
        if total >= BUDGET_ABORT_USD:
            raise RuntimeError(
                f"BUDGET ABORT: estimated spend ${total:.4f} >= "
                f"${BUDGET_ABORT_USD:.2f} soft cap (hard cap "
                f"${BUDGET_CAP_USD:.2f}). Stop all LLM-involving work."
            )
        return total


def anthropic_key() -> str:
    return (Path.home() / ".anthropic" / "api_key").read_text().strip()


async def perform_canonical_task(page, note_text: str) -> None:
    """Drive the canonical MockMed task on a Playwright async page.

    Used as the scripted demonstrator during third-party RECORDING sessions
    (typing is paced so event-capture extensions see human-like input).
    """
    await page.goto(STUDY_URL)
    await page.wait_for_selector("#username")
    await page.click("#username")
    await page.keyboard.type(USERNAME, delay=60)
    await page.click("#password")
    await page.keyboard.type(PASSWORD, delay=60)
    await page.wait_for_timeout(400)
    await page.click("#signin")
    await page.wait_for_selector("#tasks-table")
    await page.wait_for_timeout(600)
    # FIRST referral row's Open button (Jane Sample / p1 at record time).
    await page.click("#open-p1")
    await page.wait_for_selector("#new-encounter")
    await page.wait_for_timeout(600)
    await page.click("#new-encounter")
    await page.wait_for_selector("#type-triage")
    await page.wait_for_timeout(400)
    await page.click("#type-triage")
    await page.wait_for_timeout(400)
    await page.click("#note")
    await page.keyboard.type(note_text, delay=40)
    await page.wait_for_timeout(400)
    await page.click("#save-encounter")
    await page.wait_for_selector("#saved-banner")
    await page.wait_for_timeout(1500)

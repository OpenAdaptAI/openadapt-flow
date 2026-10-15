"""Spend ledger for the paid agent runs (../spend.json).

Founder-approved limits: hard cap $50 in total, per-run cap $2.50, and no new
run starts once the total reaches $45. Every cost here is computed from the
API's own `usage` numbers at list price (5-minute cache writes at 1.25x input,
cache reads at the published cache-read price). The ledger is rewritten after
every model call, so a crash can never lose spend that already happened.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

LEDGER = Path("/Users/abrichr/oa/scratch/benchmark/spend.json")
HARD_CAP_USD = 15.0
STOP_STARTING_AT_USD = 14.0
PER_RUN_CAP_USD = 0.75

# USD per million tokens, list price, checked 2026-10-14 against the claude-api
# skill's model table (cached 2026-10-06) and the computer-use tool page.
PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write_5m": 2.50,
                          "cache_write_1h": 4.00, "cache_read": 0.20},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_write_5m": 5.00,
                        "cache_write_1h": 8.00, "cache_read": 0.20},
}


def usage_cost(model: str, usage: dict[str, Any]) -> dict[str, Any]:
    """List-price cost of one response's usage block (all buckets)."""
    p = PRICES[model]
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cw = int(usage.get("cache_creation_input_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    detail = usage.get("cache_creation") or {}
    cw1h = int(detail.get("ephemeral_1h_input_tokens") or 0)
    cw5 = cw - cw1h
    cost = (inp * p["input"] + out * p["output"] + cw5 * p["cache_write_5m"]
            + cw1h * p["cache_write_1h"] + cr * p["cache_read"]) / 1_000_000
    return {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": cr, "cost_usd": cost}


def load() -> dict[str, Any]:
    d = json.loads(LEDGER.read_text())
    d.setdefault("runs", [])
    return d


def _save(d: dict[str, Any]) -> None:
    d["total_list_price_usd"] = round(sum(r.get("cost_usd", 0.0) for r in d["runs"]), 6)
    d["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tmp = LEDGER.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, LEDGER)


def total() -> float:
    d = load()
    return sum(r.get("cost_usd", 0.0) for r in d["runs"])


def may_start_run() -> tuple[bool, str]:
    t = total()
    if t >= STOP_STARTING_AT_USD:
        return False, f"total ${t:.4f} reached the ${STOP_STARTING_AT_USD:.2f} stop line"
    if t + PER_RUN_CAP_USD > HARD_CAP_USD:
        return False, f"total ${t:.4f} + one run's cap would pass the ${HARD_CAP_USD:.2f} hard cap"
    return True, f"total ${t:.4f}"


def open_run(run_id: str, model: str, meta: dict[str, Any]) -> None:
    d = load()
    if any(r["run_id"] == run_id for r in d["runs"]):
        raise SystemExit(f"run already in ledger: {run_id}")
    d["status"] = "running"
    d["runs"].append({"run_id": run_id, "model": model, "status": "running",
                      "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "api_calls": 0,
                      "input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0, "cost_usd": 0.0, **meta})
    _save(d)


def record_call(run_id: str, call: dict[str, Any]) -> float:
    """Add one response's priced usage to the run; returns the new grand total."""
    d = load()
    r = next(x for x in d["runs"] if x["run_id"] == run_id)
    r["api_calls"] += 1
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens"):
        r[k] += call[k]
    r["cost_usd"] = round(r["cost_usd"] + call["cost_usd"], 8)
    _save(d)
    return d["total_list_price_usd"]


def close_run(run_id: str, status: str, extra: dict[str, Any] | None = None) -> None:
    d = load()
    r = next(x for x in d["runs"] if x["run_id"] == run_id)
    r["status"] = status
    r["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if extra:
        r.update(extra)
    d["status"] = "idle between runs"
    _save(d)

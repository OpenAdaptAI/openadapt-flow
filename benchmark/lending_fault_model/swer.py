"""Lending (MockLoan) Silent Wrong-Effect Rate: screen-only vs effect-verified.

The effect-verified companion to ``benchmark/lending_fault_model/run.py``. Where
``run.py`` replays the compiled bundle through the REAL ``Replayer`` under the
screen-only postcondition contract (and measures how often that silently
mishandles a transactional fault), THIS module measures the same faults through
the shared EffectBench scoring contract
(:func:`openadapt_flow.benchmark.effectbench.score_episode`) under a THREE-arm
ladder that mirrors the clinical ``effect_e2e`` study (screen / single-surface /
complete-read-path), so the two domains are directly comparable:

- ``screen_only`` - the deceptive witness: the agent believes the rendered
  "Disbursement authorized" banner.
- ``effect_verify_single`` - the agent consults its OWN independent
  :class:`~openadapt_flow.runtime.effects.RestRecordVerifier` reading a SINGLE
  surface, the disbursements ledger at ``GET /api/disbursements`` (a path the SPA
  never calls), and halts unless the disbursement is CONFIRMED. This is the
  lending analog of the clinical single-surface REST oracle (encounters only): it
  catches every same-surface fault but is BLIND to a ``collateral`` write that
  lands on the separate fees / general-ledger surface, so it leaves a residual
  silent-wrong-effect on exactly that one class - the honest 9/90-style residual.
- ``effect_verify_full`` - the same agent reading the COMPLETE read path over
  every mutable surface at ``GET /api/db`` (disbursements + fees). It sees the
  collateral row and drives the residual to 0.

Only ``reported_success`` (and the arm's own read path) differs between the arms;
the independent benchmark oracle handed to ``score_episode`` is identical - the
COMPLETE read path over ``/api/db`` - so it is the SAME ground truth judging all
three arms. An injected fault classifies as ``silent_wrong_effect`` under
``screen_only``; every non-collateral fault is caught under BOTH effect arms; the
``collateral`` fault is a residual silent-wrong-effect under
``effect_verify_single`` (invisible to a single-surface oracle) and caught under
``effect_verify_full`` - the headline this second domain now shows with the SAME
honest residual the clinical study reports.

Oracle independence (confirming the sibling ``effect_e2e`` design, unchanged):
the ground-truth oracle reads the true effect over the COMPLETE read path
(``/api/db``, both surfaces) - pre-state captured BEFORE the write, post-state
read AFTER - and never trusts the agent's self-report or the screen. Every trial
binds a TRIAL-UNIQUE memo (and idempotency key), so the oracle checks THIS run's
exact write and cross-trial contamination is detectable. The C6 wrong-record task
exercises the identity gate on the consequential step: a same-name decoy loan is
seeded, a blind write funds the wrong loan, and the intended loan stays empty
behind a green screen.

Both MockMed and MockLoan are SYNTHETIC apps built by the same team, so a
matching residual across the two is SUGGESTIVE of generalizability, not proof.

No model calls, no browser, localhost only - runs in CI. Every fault is injected
deterministically at the boundary and the writes are fixed, so run-to-run
variance is ~0: results are reported as a coverage matrix over scenarios, not a
sampled rate.

Usage::

    python -m benchmark.lending_fault_model.swer            # write results + md
    python -m benchmark.lending_fault_model.swer --print    # print, don't write
    python -m benchmark.lending_fault_model.swer --trials 5
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import platform
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

import requests

from openadapt_flow.benchmark.effectbench import (
    AgentReport,
    DivergenceCategory,
    Effect,
    EffectKind,
    EpisodeRecord,
    RestRecordVerifier,
    Substrate,
    ValueExpr,
    score_episode,
    summarize,
)
from openadapt_flow.mockloan.fault_server import LedgerDB, serve

HERE = Path(__file__).resolve().parent

TARGET_LOAN = "L1001"
TARGET_PRODUCT = "Personal"
TARGET_AMOUNT = "18500"
# A same-name decoy loan for the homonym / wrong-record identity task.
DECOY_LOAN = "L1009"
DECOY_AMOUNT = "40000"

ARMS = ("screen_only", "effect_verify_single", "effect_verify_full")
# The COMPLETE read path (every mutable surface) vs the SINGLE-surface read path
# (the disbursements ledger only). The ground-truth oracle always reads the
# complete path; the effect arms differ only in which their OWN verifier reads.
FULL_RECORDS_PATH = "/api/db"
SINGLE_RECORDS_PATH = "/api/disbursements"
_ARM_READ_PATH = {
    "effect_verify_single": SINGLE_RECORDS_PATH,
    "effect_verify_full": FULL_RECORDS_PATH,
}
_DOUBLE_POST = {"duplicate", "double", "idempotent"}
_HTTP_TIMEOUT_S = 5.0
MOCKLOAN_TIMEOUT_S = 0.2


# -- effect authoring (built directly on the public EffectBench surface) -------


def _expr(v: object) -> ValueExpr:
    if isinstance(v, ValueExpr):
        return v
    if isinstance(v, dict) and "param" in v:
        return ValueExpr(param=str(v["param"]))
    return ValueExpr(literal=str(v))


def _record_effect(match: dict[str, object], *, idem: bool = False) -> Effect:
    """At-most-once, trial-unique consequential-write contract."""
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={k: _expr(v) for k, v in match.items()},
        expected_count=1,
        count_new_only=True,
        forbid_collateral_loss=True,
        idempotency_key=ValueExpr(param="record_key") if idem else None,
        key_field="key",
        risk="irreversible",
        probe="exactly one new disbursement on the target loan with this memo",
        timeout_s=MOCKLOAN_TIMEOUT_S,
    )


def _memo_effect(match: dict[str, object]) -> Effect:
    """Field read-back of the consequential memo (catches a partial save)."""
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={k: _expr(v) for k, v in match.items()},
        field="memo",
        value=ValueExpr(param="memo"),
        risk="irreversible",
        probe="the persisted disbursement memo equals the authorized memo",
        timeout_s=MOCKLOAN_TIMEOUT_S,
    )


# -- the lending task family ---------------------------------------------------


@dataclass(frozen=True)
class LendingTask:
    """One MockLoan consequential-write task + its live drive recipe."""

    task_id: str
    category: DivergenceCategory
    fault: str
    effect: Effect
    correct_action_available: bool
    # The disbursement the agent actually posts (loan_id/product/amount). None
    # models a silent no-op (the click never reaches the boundary).
    write: Optional[dict[str, str]] = field(
        default_factory=lambda: {
            "loan_id": TARGET_LOAN,
            "product": TARGET_PRODUCT,
            "amount": TARGET_AMOUNT,
        }
    )
    seed_concurrent: bool = False
    decoys: tuple[dict[str, str], ...] = ()
    screen_success: bool = True


def _target_match(*, memo_is_target: bool = True) -> dict[str, object]:
    m: dict[str, object] = {"loan_id": TARGET_LOAN, "product": TARGET_PRODUCT}
    if memo_is_target:
        m["memo"] = {"param": "memo"}
    return m


TASKS: tuple[LendingTask, ...] = (
    LendingTask(
        "lending_ctl_clean",
        DivergenceCategory.CONTROL,
        "ok",
        _record_effect(_target_match()),
        correct_action_available=True,
    ),
    LendingTask(
        "lending_c1_partial_memo_dropped",
        DivergenceCategory.C1_PARTIAL_SAVE,
        "partial",
        _memo_effect({"loan_id": TARGET_LOAN, "product": TARGET_PRODUCT}),
        correct_action_available=False,
    ),
    LendingTask(
        "lending_c2_duplicate_submit",
        DivergenceCategory.C2_DUPLICATE_SUBMISSION,
        "duplicate",
        _record_effect(_target_match()),
        correct_action_available=False,
    ),
    LendingTask(
        "lending_c3_optimistic_reject",
        DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
        "optimistic",
        _record_effect(_target_match()),
        correct_action_available=False,
    ),
    LendingTask(
        "lending_c3_timeout_false_abort",
        DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
        "timeout",
        _record_effect(_target_match()),
        correct_action_available=True,
        screen_success=False,
    ),
    LendingTask(
        "lending_c3_session_expired",
        DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
        "session",
        _record_effect(_target_match()),
        correct_action_available=False,
        screen_success=False,
    ),
    LendingTask(
        "lending_c4_stale_lost_update",
        DivergenceCategory.C4_STALE_OVERWRITE,
        "stale",
        _record_effect(_target_match()),
        correct_action_available=False,
        seed_concurrent=True,
    ),
    LendingTask(
        "lending_c5_double_delivered",
        DivergenceCategory.C5_DOUBLE_DELIVERED_INPUT,
        "double",
        _record_effect(_target_match()),
        correct_action_available=False,
    ),
    LendingTask(
        "lending_c6_homonym_wrong_loan",
        DivergenceCategory.C6_WRONG_RECORD_HOMONYM,
        "",
        _record_effect(_target_match()),
        correct_action_available=True,
        write={
            "loan_id": DECOY_LOAN,
            "product": TARGET_PRODUCT,
            "amount": DECOY_AMOUNT,
        },
        decoys=(
            {"loan_id": DECOY_LOAN, "product": TARGET_PRODUCT, "amount": DECOY_AMOUNT},
        ),
    ),
    LendingTask(
        "lending_c7_silent_noop",
        DivergenceCategory.C7_SILENT_NOOP_WRONG_TARGET,
        "",
        _record_effect(_target_match()),
        correct_action_available=True,
        write=None,
    ),
    LendingTask(
        "lending_c8_collateral_unaudited",
        DivergenceCategory.C8_COLLATERAL_UNAUDITED,
        "collateral",
        # The CORRECT disbursement to the target loan IS booked; the effect
        # contract is exactly the clean-write contract. A disbursements-only
        # oracle therefore certifies it. The fault is a SPURIOUS money-movement
        # ALSO written to the separate fees / general-ledger surface (same loan +
        # funding memo), which the COMPLETE read path counts as a second matching
        # money-movement row for one authorization (at-most-once violated).
        _record_effect(_target_match()),
        correct_action_available=True,
    ),
    LendingTask(
        "lending_ctl_idempotent_fix",
        DivergenceCategory.CONTROL,
        "idempotent",
        _record_effect(_target_match(), idem=True),
        correct_action_available=True,
    ),
)


# -- live harness --------------------------------------------------------------


@contextlib.contextmanager
def serve_mockloan() -> Iterator[tuple[str, LedgerDB]]:
    url, db, stop = serve(port=0)
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def trial_params(task_id: str, trial: int) -> dict[str, str]:
    """Derive a deterministic, TRIAL-UNIQUE payload from (task, trial)."""
    tag = hashlib.sha256(f"{task_id}:{trial}".encode()).hexdigest()[:12]
    return {
        "memo": f"funding ref {tag}",
        "record_key": f"key-{tag}",
    }


def _post(
    base_url: str,
    write: dict[str, str],
    memo: str,
    fault: str,
    key: Optional[str],
) -> requests.Response:
    body: dict[str, str] = {
        "loan_id": write["loan_id"],
        "product": write["product"],
        "amount": write["amount"],
        "memo": memo,
    }
    if key is not None:
        body["key"] = key
    path = "/api/disbursement" + (f"?fault={fault}" if fault else "")
    return requests.post(base_url + path, json=body, timeout=_HTTP_TIMEOUT_S)


def _perform_writes(base_url: str, task: LendingTask, params: dict[str, str]) -> None:
    if task.write is None:  # silent no-op: the click never reaches the boundary
        return
    key = params["record_key"] if task.fault == "idempotent" else None
    n_posts = 2 if task.fault in _DOUBLE_POST else 1
    for _ in range(n_posts):
        with contextlib.suppress(requests.RequestException):
            _post(base_url, task.write, params["memo"], task.fault, key)


def _reset_and_seed(base_url: str, task: LendingTask, params: dict[str, str]) -> None:
    requests.post(
        base_url + "/api/reset",
        json={"seed_concurrent": task.seed_concurrent},
        timeout=_HTTP_TIMEOUT_S,
    )
    for decoy in task.decoys:
        _post(base_url, decoy, "pre-existing decoy disbursement", "", None)


def _agent_action(
    arm: str, base_url: str, task: LendingTask, params: dict[str, str]
) -> Callable[[], AgentReport]:
    def screen_only() -> AgentReport:
        _perform_writes(base_url, task, params)
        return AgentReport(
            reported_success=task.screen_success,
            halted=not task.screen_success,
            message="believed the rendered banner",
        )

    def effect_verify() -> AgentReport:
        # The arm's OWN verifier reads its arm-specific surface coverage:
        # ``effect_verify_single`` reads only the disbursements ledger and is
        # blind to a fees-surface (collateral) write; ``effect_verify_full``
        # reads the complete path over every mutable surface.
        own = RestRecordVerifier(
            base_url, records_path=_ARM_READ_PATH[arm], records_key="records"
        )
        own_before = own.capture_pre_state()
        _perform_writes(base_url, task, params)
        resolved = task.effect.resolve(params)
        verdict = own.verify(resolved, own_before)
        return AgentReport(
            reported_success=verdict.confirmed,
            halted=not verdict.confirmed,
            message=f"self-verified effect ({_ARM_READ_PATH[arm]}): "
            f"{verdict.verdict.value}",
        )

    if arm == "screen_only":
        return screen_only
    return effect_verify


def run_episode(
    task: LendingTask, *, arm: str, trial: int, base_url: str
) -> EpisodeRecord:
    params = trial_params(task.task_id, trial)
    _reset_and_seed(base_url, task, params)
    oracle = RestRecordVerifier(base_url, records_path="/api/db", records_key="records")
    return score_episode(
        episode_id=f"{arm}::{task.task_id}::{trial}",
        task_id=task.task_id,
        arm=arm,
        trial=trial,
        substrate=Substrate.WEB,
        category=task.category,
        oracle=oracle,
        expected_effect=task.effect,
        run_action=_agent_action(arm, base_url, task, params),
        correct_action_available=task.correct_action_available,
        params=params,
        seed=trial,
        env_fingerprint={"env": "mockloan", "substrate": "web", "ci_fast": True},
    )


def run_pack(
    tasks: tuple[LendingTask, ...] = TASKS,
    *,
    arms: tuple[str, ...] = ARMS,
    trials: int = 3,
) -> list[EpisodeRecord]:
    episodes: list[EpisodeRecord] = []
    with serve_mockloan() as (base_url, _db):
        for task in tasks:
            for arm in arms:
                for trial in range(trials):
                    episodes.append(
                        run_episode(task, arm=arm, trial=trial, base_url=base_url)
                    )
    return episodes


def _coverage_matrix(episodes: list[EpisodeRecord]) -> list[dict]:
    """Per-scenario (task x arm) outcome, the deterministic-replay unit.

    Because every fault is injected deterministically and the writes are fixed,
    all trials of a (task, arm) agree; this collapses them to one cell per
    scenario and flags the silent-wrong-effect residual explicitly.
    """
    rows: list[dict] = []
    for task in TASKS:
        row: dict = {
            "task_id": task.task_id,
            "category": task.category.value,
            "fault": task.fault or "(identity/no-op)",
        }
        for arm in ARMS:
            outs = sorted(
                {
                    e.outcome.value
                    for e in episodes
                    if e.task_id == task.task_id and e.arm == arm
                }
            )
            row[arm] = "|".join(outs)
            row[f"{arm}_silent"] = "silent_wrong_effect" in outs
        rows.append(row)
    return rows


def measure(trials: int = 3) -> dict:
    episodes = run_pack(trials=trials)
    summaries = {arm: summarize(episodes, arm=arm) for arm in ARMS}
    return {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "platform": f"{platform.system()} {platform.machine()} "
            f"py{platform.python_version()}",
            "domain": "lending (MockLoan) - loan disbursement authorization",
            "oracle": (
                "runtime.effects.RestRecordVerifier over the COMPLETE read path "
                "GET /api/db (disbursements + fees)"
            ),
            "single_surface_read_path": SINGLE_RECORDS_PATH,
            "full_read_path": FULL_RECORDS_PATH,
            "ground_truth": "mockloan.fault_server in-process ledger",
            "arms": list(ARMS),
            "tasks": len(TASKS),
            "trials_per_task_per_arm": trials,
            "deterministic": True,
            "model_calls": 0,
        },
        **{arm: summaries[arm].model_dump(mode="json") for arm in ARMS},
        "coverage_matrix": _coverage_matrix(episodes),
        "episodes": [e.model_dump(mode="json") for e in episodes],
    }


_MD_HEADER = """# Lending (MockLoan) Silent Wrong-Effect Rate - screen / single-surface / complete read path

The non-healthcare replication of the SWER headline on a second system of
record, run as the SAME three-arm ladder as the clinical `effect_e2e` study so
the two domains are directly comparable: screen-only, a SINGLE-surface oracle
(disbursements ledger only, `GET /api/disbursements`), and the COMPLETE read path
over every mutable surface (disbursements + fees, `GET /api/db`). The ground
truth judging all three arms is the complete read path; each arm never trusts the
screen or its own self-report. Every trial binds a trial-unique memo + idempotency
key. Zero model calls.

**The gate (AGENTS.md safety asymmetry): the only dangerous error is a SILENT
WRONG-EFFECT - reporting/rendering success while the ledger disagrees.** A single
out-of-band oracle collapses it to a residual on exactly one class (a collateral
write to a surface it does not read); only a complete read path over every
mutable surface reaches 0. Over-halt (halting when the write was actually fine) is
the safe error; it is reported as the availability cost.

Both MockLoan and the clinical MockMed are SYNTHETIC apps built by the same team.
A matching single-surface residual across the two domains is SUGGESTIVE of
generalizability, not proof; the point it earns is narrower and honest: a single
out-of-band record oracle is not sufficient - 0 requires a read path covering
every mutable surface.
"""


def _fmt_rate(d: dict) -> str:
    return (
        f"**{round(d['swer']['rate'], 3)}** "
        f"({d['swer']['numerator']}/{d['swer']['denominator']})"
    )


def to_markdown(result: dict) -> str:
    m = result["meta"]
    arms = m["arms"]
    by_arm = {arm: result[arm] for arm in arms}
    single = by_arm["effect_verify_single"]
    full = by_arm["effect_verify_full"]
    screen = by_arm["screen_only"]
    lines = [_MD_HEADER, ""]
    lines.append(f"Generated: {m['generated_at']}  ")
    lines.append(f"Platform: {m['platform']}  ")
    lines.append(
        f"Tasks: {m['tasks']} (divergence classes C1-C8 + clean / idempotent "
        f"controls).  "
    )
    lines.append(
        f"Trials per task per arm: {m['trials_per_task_per_arm']} "
        "(DETERMINISTIC replays; run-to-run variance ~ 0, so these are a "
        "coverage matrix over scenarios, not a sampled rate - no confidence "
        "interval is implied).  "
    )
    lines.append("")
    lines.append("## Headline - the ladder")
    lines.append("")
    lines.append(
        "| arm | read path | episodes | SWER | over-halt | task success | "
        "success-effect gap |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    read_paths = {
        "screen_only": "the rendered banner",
        "effect_verify_single": f"single surface (`{m['single_surface_read_path']}`)",
        "effect_verify_full": f"complete (`{m['full_read_path']}`)",
    }
    for arm in arms:
        d = by_arm[arm]
        lines.append(
            f"| `{arm}` | {read_paths[arm]} | {d['n_episodes']} | "
            f"{_fmt_rate(d)} | {round(d['over_halt']['rate'], 3)} | "
            f"{round(d['task_success']['rate'], 3)} | "
            f"{round(d['success_effect_gap'], 3)} |"
        )
    lines.append("")
    lines.append(
        f"- Screen-only SWER = {_fmt_rate(screen)}: the injected faults "
        "render a clean 'Disbursement authorized' banner while the ledger is "
        "wrong (a partial/phantom/duplicate/lost-update/wrong-loan/collateral "
        "write)."
    )
    lines.append(
        f"- Single-surface SWER = {_fmt_rate(single)}: a single out-of-band "
        "oracle over the disbursements ledger catches every same-surface fault "
        "but is BLIND to the `collateral` write on the fees surface, leaving a "
        "residual silent-wrong-effect on exactly that one class. This is the "
        "lending analog of the clinical single-surface REST oracle's 9/90 "
        "residual (the SAME honest finding, a second domain)."
    )
    lines.append(
        f"- Complete-read-path SWER = {_fmt_rate(full)}: reading every "
        "mutable surface (disbursements + fees) sees the collateral row and "
        f"drives the residual to 0; the residual cost is over-halt = "
        f"{round(full['over_halt']['rate'], 3)} (safe: a human finishes a "
        "recoverable "
        "case). 0 requires a read path covering EVERY mutable surface."
    )
    lines.append("")
    lines.append("## Coverage matrix (deterministic - one cell per scenario)")
    lines.append("")
    lines.append(
        "| task | class | fault | screen_only | effect_verify_single | "
        "effect_verify_full |"
    )
    lines.append("|---|---|---|---|---|---|")
    for row in result["coverage_matrix"]:
        def _cell(arm: str, row: dict = row) -> str:
            mark = " (SILENT)" if row[f"{arm}_silent"] else ""
            return f"{row[arm]}{mark}"

        lines.append(
            f"| `{row['task_id']}` | {row['category']} | {row['fault']} | "
            f"{_cell('screen_only')} | {_cell('effect_verify_single')} | "
            f"{_cell('effect_verify_full')} |"
        )
    lines.append("")
    lines.append("## Per-outcome counts")
    lines.append("")
    keys = sorted({k for arm in arms for k in by_arm[arm]["outcome_counts"]})
    lines.append("| arm | " + " | ".join(keys) + " |")
    lines.append("|---|" + "---|" * len(keys))
    for arm in arms:
        d = by_arm[arm]
        row = " | ".join(str(d["outcome_counts"].get(k, 0)) for k in keys)
        lines.append(f"| `{arm}` | {row} |")
    lines.append("")
    lines.append("## Method / oracle independence")
    lines.append("")
    lines.append(
        "All three arms drive the SAME writes against the SAME fault server; "
        "only `reported_success` (and, for the effect arms, which surface the "
        "arm's OWN verifier reads) differs. The independent ground-truth oracle "
        "handed to `score_episode` is a `RestRecordVerifier` reading the "
        "COMPLETE path `/api/db` (both surfaces) pre-action and post-action, and "
        "it is a DISTINCT instance from any arm's own verifier - the arm cannot "
        "influence the judge. The `collateral` (C8) fault books the correct "
        "disbursement AND a spurious fee to the separate fees / general-ledger "
        "surface with the same loan and funding memo: the disbursements-only "
        "read counts one correct money-movement row (CONFIRMED), while the "
        "complete read path counts two for one authorization (at-most-once "
        "violated -> REFUTED). That is why the single-surface arm reports a "
        "silent success and the complete-read-path arm halts. The C6 task seeds "
        "a same-name decoy loan and funds it; the intended loan stays empty, so "
        "a blind (identity-less) write is a silent wrong-effect under "
        "`screen_only` and an over-halt (caught, safe) under both effect arms."
    )
    lines.append("")
    lines.append("## Honest disclosure")
    lines.append("")
    lines.append(
        "- **Both apps are SYNTHETIC.** MockLoan and MockMed are toy apps built "
        "by the same team; two synthetic domains agreeing is suggestive of "
        "generalizability, not proof.\n"
        "- **The single-surface oracle leaves a residual on the collateral "
        "class**, exactly as the clinical study's single-surface REST oracle "
        "does (9/90). The two domains are therefore comparable: neither reaches "
        "0 with a single out-of-band record oracle.\n"
        "- **0 requires a COMPLETE read path** covering every mutable surface. "
        "The complete-read-path arm reaches 0 here only because `/api/db` spans "
        "both the disbursements and the fees surfaces; a real deployment must "
        "enumerate and read every surface a consequential write can touch.\n"
        "- **No confidence intervals are implied.** These are deterministic "
        "replays (variance ~ 0); the table is a coverage matrix over scenarios, "
        "not a sampled estimate."
    )
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```\npython -m benchmark.lending_fault_model.swer\n```")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=3, help="trials per task per arm")
    parser.add_argument(
        "--print", action="store_true", help="print results, do not write files"
    )
    args = parser.parse_args(argv)
    result = measure(trials=args.trials)
    if args.print:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    (HERE / "swer_results.json").write_text(json.dumps(result, indent=2) + "\n")
    (HERE / "SWER.md").write_text(to_markdown(result))
    s = result["screen_only"]["swer"]
    sg = result["effect_verify_single"]["swer"]
    fu = result["effect_verify_full"]["swer"]
    print(
        f"lending SWER: screen_only={s['rate']} ({s['numerator']}/{s['denominator']}), "
        f"effect_verify_single={sg['rate']} ({sg['numerator']}/{sg['denominator']}), "
        f"effect_verify_full={fu['rate']} ({fu['numerator']}/{fu['denominator']}) "
        f"-> wrote swer_results.json + SWER.md under {HERE}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

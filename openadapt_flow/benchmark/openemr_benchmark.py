"""OpenEMR benchmark: compiled replay vs. computer-use agent on a real app.

The external-target counterpart of :mod:`.run_benchmark` (MockMed). The
target is the official OpenEMR public demo — a dense, frame-heavy, slow,
LAMP-era EMR whose shared instance is mutated by other visitors all day and
resets daily. The workflow is the 18-step add-patient-note demonstration
recorded by ``scripts/openemr_demo.py`` (fake demo patients only).

Both arms are judged by the same criterion, implemented once
(:func:`openadapt_flow.benchmark.verify.verify_note_saved`): OCR of the
final screen must show the run's parameterized note text in the patient
message list. Each run — in BOTH arms — uses a distinct note value, so a
pass proves parameter substitution against live state, not replay of a
baked-in literal.

Public-demo courtesy: runs are paced ``pace_s`` seconds apart, compiled and
agent Ns are small (defaults 20 and 10), and per-run failures are recorded
as data points rather than retried against the shared instance.

Outputs written to ``out_dir``: ``results.json``, ``BENCHMARK.md``, and
``latency_cost.png`` (same chart as the MockMed benchmark).
"""

from __future__ import annotations

import json
import platform
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from openadapt_flow.benchmark import agent_baseline
from openadapt_flow.benchmark.run_benchmark import (
    _agent_run,
    _arm_aggregate,
    _compiled_run,
    render_chart,
)
from openadapt_flow.benchmark.verify import verify_note_saved

DEMO_URL = "https://demo.openemr.io/openemr/index.php"
N_COMPILED = 20
N_AGENT = 10
#: Courtesy gap between runs against the shared public demo.
PACE_S = 30.0
#: 18 steps plus headroom for dense, slow screens.
AGENT_MAX_ACTIONS = 40

# One unique sentence per run across BOTH arms. Deliberately mutually
# dissimilar: several runs' notes are visible in the same message list at
# verification time, so any long shared prefix or repeated variant would
# let one run's note satisfy another run's check. A unit test asserts the
# pairwise longest common squashed substring stays below the verifier's
# contiguous-run threshold.
_COMPILED_NOTES = [
    "Renal panel ordered ahead of the next quarterly visit.",
    "Walking program begun, thirty minutes on weekday mornings.",
    "Pharmacy contact changed to the downtown branch office.",
    "Dizziness resolved after the evening dose adjustment.",
    "Low-sodium meal plan handout given and explained.",
    "Home blood-pressure log shows stable readings all month.",
    "Flu shot declined today; revisit the topic in autumn.",
    "Occupational therapy referral faxed this afternoon.",
    "Lab results within limits; no follow-up required.",
    "Refill approved for ninety days of current medication.",
    "Cardiology consult summary scanned into the chart.",
    "Smoking cessation resources mailed to home address.",
    "Podiatry exam scheduled for early next month.",
    "Weight down four pounds since the spring checkup.",
    "Eye exam reminder sent through the patient portal.",
    "Colonoscopy screening due date moved to October.",
    "Physical therapy completed; discharge notes attached.",
    "Sleep study questionnaire returned and scored.",
    "Hearing aid battery supply reordered by front desk.",
    "Orthopedic pillow suggestion discussed for neck pain.",
]
_AGENT_NOTES = [
    "Tetanus booster administered in the left deltoid.",
    "Grip strength improved at this week's therapy session.",
    "Insurance card copied and coverage verified by phone.",
    "Allergy list updated to include seasonal pollen.",
    "Crutches returned; gait steady without assistance.",
    "Dermatology biopsy site healing cleanly, no drainage.",
    "Travel vaccine consult booked before the June trip.",
    "Glucometer readings uploaded from the home device.",
    "Knee brace fitted and sizing documented in chart.",
    "Caregiver contact number added to emergency file.",
]


def note_for(arm: str, i: int) -> str:
    """Distinct per-run note text (unique across BOTH arms).

    Args:
        arm: ``"compiled"`` or ``"agent"``.
        i: Zero-based run index within the arm.

    Returns:
        A clinically-plausible note string unique to (arm, i).
    """
    notes = _COMPILED_NOTES if arm == "compiled" else _AGENT_NOTES
    return notes[i % len(notes)]


def aggregate_openemr_results(
    compiled_rows: list[dict[str, Any]],
    agent_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the full results document from per-run rows.

    Args:
        compiled_rows: Rows from the compiled arm.
        agent_rows: Rows from the agent arm.

    Returns:
        The results dict serialized to ``results.json``.
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task": (
            "OpenEMR public demo: log in, search the demo patient, open "
            "the chart, scroll the dashboard to the Messages card, open "
            "Patient Messages, add a parameterized note, save"
        ),
        "target": DEMO_URL,
        "workflow_steps": 18,
        "model": agent_baseline.MODEL,
        "computer_tool": agent_baseline.COMPUTER_TOOL_TYPE,
        "beta_header": agent_baseline.COMPUTER_USE_BETA,
        "agent_max_actions": AGENT_MAX_ACTIONS,
        "pricing_usd_per_mtok": {
            "input": agent_baseline.INPUT_USD_PER_MTOK,
            "output": agent_baseline.OUTPUT_USD_PER_MTOK,
            "note": (
                "list price; an introductory $2/$10 rate applies through "
                "2026-08-31"
            ),
        },
        "platform": platform.platform(),
        "arms": {
            "compiled": _arm_aggregate(compiled_rows),
            "agent": _arm_aggregate(agent_rows),
        },
        "runs": {"compiled": compiled_rows, "agent": agent_rows},
    }


def render_openemr_markdown(results: dict[str, Any]) -> str:
    """Render ``BENCHMARK.md`` from the results dict.

    Args:
        results: The aggregate results dict.

    Returns:
        The markdown document as a string.
    """
    c = results["arms"]["compiled"]
    a = results["arms"]["agent"]
    date = results["generated_at"][:10]
    agent_errors = [
        r for r in results["runs"]["agent"] if not r["success"]
    ]
    failure_lines = "".join(
        f"- agent run {r['i'] + 1}: {r.get('stopped', '?')}, "
        f"{r['actions']} actions, {r['wall_s']:.0f}s, "
        f"${r['cost_usd']:.4f}, "
        f"OCR matched {r.get('matched_ratio', 0):.0%} of the note"
        + (f" — {r['error']}" if r.get("error") else "")
        + "\n"
        for r in agent_errors
    ) or "- none\n"
    compiled_failures = [
        r for r in results["runs"]["compiled"] if not r["success"]
    ]
    compiled_failure_lines = "".join(
        f"- compiled run {r['i'] + 1}: "
        + (
            r["error"]
            if r.get("error")
            else f"{r.get('actions', '?')} steps executed, "
            f"replayer_success={r.get('replayer_success')}, "
            f"first_failure={r.get('first_failure')}, "
            f"OCR matched {r.get('matched_ratio', 0):.0%} of the note"
        )
        + "\n"
        for r in compiled_failures
    ) or "- none\n"
    return f"""# Benchmark: compiled replay vs. computer-use agent — OpenEMR (real app)

Date: {date}. Same head-to-head as the [MockMed benchmark](../BENCHMARK.md),
run against a real third-party application: the official OpenEMR public
demo (`{results['target']}`, fake patients only, instance resets daily).
One task, two ways to automate it, one success check.

**Task** ({results['workflow_steps']} compiled steps): log in as the demo
admin, search the demo patient "Phil", open the chart of "Belford, Phil",
scroll the dense Medical Record Dashboard to the Messages card, open
Patient Messages, add a note (a distinct parameterized value per run in
BOTH arms), save.

![latency and cost](latency_cost.png)

| | compiled replay | computer-use agent |
|---|---|---|
| runs | {c['n']} | {a['n']} |
| success rate | {c['success_rate']:.0%} ({c['success_count']}/{c['n']}) \
| {a['success_rate']:.0%} ({a['success_count']}/{a['n']}) |
| latency p50 | {c['wall_s_p50']:.1f} s | {a['wall_s_p50']:.1f} s |
| latency p95 | {c['wall_s_p95']:.1f} s | {a['wall_s_p95']:.1f} s |
| model cost / run | $0 | ${a['cost_usd_per_run']:.4f} |
| total model cost | $0 | ${a['cost_usd_total']:.2f} |
| tokens (in/out, total) | 0 / 0 | {a['input_tokens_total']:,} / \
{a['output_tokens_total']:,} |

Failed runs, reported honestly:

Compiled arm:

{compiled_failure_lines}
Agent arm:

{failure_lines}
## Methodology

The [MockMed benchmark](../BENCHMARK.md) remains the CI-reproducible
methodology anchor — same orchestrator, same agent harness, same style of
OCR success check, on an app anyone can rerun deterministically. This is
the real-world result on a live third-party instance, with the caveats
below.

- **Record + compile once.** The workflow is recorded fresh against the
  live demo via `scripts/openemr_demo.py` and compiled into a
  vision-anchored bundle. Recording and compiling are a one-time cost and
  are not included in per-run latency.
- **Fresh browser per run, shared server state.** Each run of either arm
  gets a fresh chromium browser (no session state). Unlike MockMed, the
  server side is a single shared public instance that every run (and every
  other internet visitor) mutates.
- **Same interface.** Both arms drive the same `PlaywrightBackend`,
  vision-only: PNG screenshots in; pixel-coordinate clicks, typed text,
  key presses, and wheel scrolls out. Neither arm uses DOM selectors at
  run time.
- **Agent arm.** Model `{results['model']}` with the
  `{results['computer_tool']}` computer-use tool (beta header
  `{results['beta_header']}`), a {results['agent_max_actions']}-action
  budget ({results['workflow_steps']} steps plus headroom for dense,
  slow screens), and history bounded to the last 3 screenshots. The task
  prompt states user intent — credentials as a user would state them, the
  target patient, the exact note text — not steps or coordinates. Every
  executed action returns a settled screenshot.
- **Same success criterion, implemented once.** After each run, the final
  screenshot is checked by `verify_note_saved` (OCR): a contiguous run of
  at least 16 characters of the run's note must appear in the frame's
  OCR text (whitespace-squashed; retried at 2x resolution when the raw
  frame does not pass, because rapidocr drops dense table lines at
  1280x800). Neither arm's self-reported success is used.
- **Distinct, mutually dissimilar note per run in BOTH arms** (no two
  notes share a 16-character squashed substring — unit-tested), so
  success proves parameter substitution against live state and one run's
  note cannot satisfy another run's check.
- **Pacing.** Runs are spaced ~{results.get('pace_s', 30):.0f}s apart as
  public-demo courtesy; the pacing gap is excluded from latency.
- **Latency** is wall-clock around the replay / agent loop only.
- **Cost** is computed from API `usage` token counts at list pricing
  (${results['pricing_usd_per_mtok']['input']:.2f} /
  ${results['pricing_usd_per_mtok']['output']:.2f} per MTok input/output
  for {results['model']}). An introductory $2/$10 rate applies through
  2026-08-31, so billed cost today is about a third lower than reported.
  Compiled replay makes zero model calls.

## Caveats — read before quoting these numbers

- **The demo instance is shared and mutable.** Anyone on the internet can
  (and does) modify it, and it resets daily. Every successful run also
  appends a message that grows the dashboard for subsequent runs. Failure
  modes here can be demo-instance weather, not tooling; N is small by
  design (public-demo courtesy).
- **Not CI-reproducible.** The numbers depend on the live instance's state
  and load on the day of the run. The MockMed benchmark is the
  reproducible anchor; treat this as a field result.
- **The agent arm has a small N** ({a['n']}) because agent runs cost real
  money, real minutes, and real load on a shared public service. Its
  success rate carries wide error bars.
- **Network variance affects both arms** (live remote server), unlike the
  local MockMed target.
- **Model version pinned.** Results describe `{results['model']}` with
  the `{results['computer_tool']}` tool on {date}; newer models will
  differ.
- **The compiled arm needs a demonstration first.** The one-time
  record + compile step (about a minute of human demonstration) is the
  price of the fast replays; the agent needs only the prompt.
- **OCR verification on dense EMR text under-counts.** rapidocr sometimes
  drops the exact table line containing the note (a known limitation
  documented in
  [docs/showcase-openemr/FINDINGS.md](../../docs/showcase-openemr/FINDINGS.md)),
  so a "failed" verification can be a measurement miss with the note
  plainly visible in the final screenshot. The check errs conservative
  and is identical for both arms. Every run's final screenshot is saved
  to `benchmark/openemr/finals/` (local only, not committed) so failed
  verdicts can be audited against what was actually on screen.
- Single machine ({results['platform']}).

## Reproduce

```
.venv/bin/python scripts/openemr_demo.py benchmark
```

Records a fresh demonstration against the public demo, compiles it, then
runs both arms. Requires network access to the demo and
`ANTHROPIC_API_KEY` (or `~/.anthropic/api_key`). The agent arm costs real
money (about ${a['cost_usd_total']:.2f} at list price for {a['n']} runs
when this was generated) and takes about an hour with pacing. Fake demo
patients only — never point this at a real OpenEMR install.
"""


def write_openemr_outputs(results: dict[str, Any], out_dir: Path) -> None:
    """Write ``results.json``, ``BENCHMARK.md``, and the chart PNG.

    Args:
        results: The aggregate results dict.
        out_dir: Output directory (created if needed).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    render_chart(results, out_dir / "latency_cost.png")
    (out_dir / "BENCHMARK.md").write_text(render_openemr_markdown(results))


def run_openemr_benchmark(
    out_dir: Path | str,
    bundle_dir: Path | str,
    *,
    url: str = DEMO_URL,
    n_compiled: int = N_COMPILED,
    n_agent: int = N_AGENT,
    pace_s: float = PACE_S,
    max_actions: int = AGENT_MAX_ACTIONS,
    headed: bool = False,
    agent_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Run the OpenEMR benchmark from a pre-compiled bundle.

    Unlike the MockMed orchestrator this does not record: the caller
    supplies the freshly recorded + compiled bundle (see
    ``scripts/openemr_demo.py benchmark``, which records first and then
    calls this). Per-run exceptions in either arm are recorded as failed
    rows, never raised — a broken shared demo is a result, not a crash.

    Args:
        out_dir: Directory for ``results.json`` / ``BENCHMARK.md`` / chart.
        bundle_dir: Compiled OpenEMR workflow bundle.
        url: OpenEMR demo URL.
        n_compiled: Compiled-replay iterations.
        n_agent: Agent iterations.
        pace_s: Courtesy sleep between runs (both arms).
        max_actions: Agent action budget.
        headed: Run browsers headed (debugging).
        agent_client: Optional injected Anthropic client (tests).
        sleep: Sleep function (injectable for tests).
        log: Progress logger.

    Returns:
        The results dict (also written to ``results.json``).
    """
    out = Path(out_dir)
    bundle = Path(bundle_dir)
    # Final screenshots per run, for post-hoc audit of the OCR verdict
    # (kept out of version control; see .gitignore).
    finals = out / "finals"

    compiled_rows: list[dict[str, Any]] = []
    agent_rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="oaf-openemr-bench-") as tmp_str:
        tmp = Path(tmp_str)
        for i in range(n_compiled):
            if i:
                sleep(pace_s)
            note = note_for("compiled", i)
            run_dir = tmp / f"compiled_{i:03d}"
            try:
                row = _compiled_run(
                    bundle,
                    url,
                    run_dir,
                    note,
                    verify_fn=verify_note_saved,
                    save_final_to=finals / f"compiled_{i:03d}.png",
                    headed=headed,
                )
            except Exception as exc:  # noqa: BLE001 - a failed run is data
                row = {
                    "arm": "compiled",
                    "wall_s": 0.0,
                    "success": False,
                    "actions": 0,
                    "api_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            row["i"] = i
            row["note"] = note
            compiled_rows.append(row)
            if row["success"]:
                shutil.rmtree(run_dir, ignore_errors=True)
            elif run_dir.exists():
                # Keep failed runs' full step artifacts for audit (local
                # only, next to the final screenshots; not committed).
                keep = finals / f"failed_compiled_{i:03d}"
                shutil.rmtree(keep, ignore_errors=True)
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(run_dir), str(keep))
            log(
                f"compiled {i + 1}/{n_compiled}: success={row['success']} "
                f"replayer={row.get('replayer_success')} "
                f"first_failure={row.get('first_failure')} "
                f"ratio={row.get('matched_ratio')} "
                f"run={row.get('longest_run')} "
                f"{row['wall_s']:.1f}s err={row['error']}"
            )

        for i in range(n_agent):
            sleep(pace_s)
            note = note_for("agent", i)
            row = _agent_run(
                url,
                note,
                task=agent_baseline.openemr_task_prompt(note),
                verify_fn=verify_note_saved,
                save_final_to=finals / f"agent_{i:03d}.png",
                client=agent_client,
                headed=headed,
                max_actions=max_actions,
            )
            row["i"] = i
            row["note"] = note
            agent_rows.append(row)
            log(
                f"agent {i + 1}/{n_agent}: success={row['success']} "
                f"ratio={row.get('matched_ratio')} "
                f"run={row.get('longest_run')} "
                f"{row['wall_s']:.1f}s ${row['cost_usd']:.4f} "
                f"actions={row['actions']} stopped={row.get('stopped')} "
                f"err={row['error']}"
            )

    results = aggregate_openemr_results(compiled_rows, agent_rows)
    results["pace_s"] = pace_s
    write_openemr_outputs(results, out)
    log(f"Wrote {out / 'results.json'}, BENCHMARK.md, latency_cost.png")
    return results

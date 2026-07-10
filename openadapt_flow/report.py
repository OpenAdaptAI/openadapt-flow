"""Render human-readable Markdown reports from run and bench artifacts.

Two entry points:

- :func:`render_run_report` — reads ``<run_dir>/report.json`` (an
  :class:`openadapt_flow.ir.RunReport`) and writes ``<run_dir>/REPORT.md``
  with the outcome headline, per-step table, embedded screenshots
  (relative paths so the report renders on GitHub), rung histogram, and
  totals.
- :func:`render_bench_report` — reads a ``bench.json`` produced by
  :func:`openadapt_flow.bench.run_bench` and writes a Markdown summary.
"""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.ir import RunReport, StepResult

# Ladder order for the rung histogram (cheapest first).
_RUNG_ORDER = ("template", "template_global", "ocr", "geometry", "grounder")


def _md_escape(text: str) -> str:
    """Escape characters that would break a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def _img(rel_path: str | None, alt: str) -> str:
    """Markdown image for a run-dir-relative path, or an em dash if missing."""
    if not rel_path:
        return "&mdash;"
    return f"![{_md_escape(alt)}]({rel_path})"


def _before_after_table(result: StepResult) -> list[str]:
    """Before/after screenshots side by side as a two-column table."""
    return [
        "| Before | After |",
        "| --- | --- |",
        f"| {_img(result.before_png, f'{result.step_id} before')} "
        f"| {_img(result.after_png, f'{result.step_id} after')} |",
    ]


def render_run_report(run_dir: Path | str) -> Path:
    """Render ``REPORT.md`` inside ``run_dir`` from its ``report.json``.

    Args:
        run_dir: Run directory containing ``report.json`` (and the
            ``steps/`` / ``heals/`` image folders it references).

    Returns:
        Path to the written ``REPORT.md``.

    Raises:
        FileNotFoundError: If ``run_dir/report.json`` does not exist.
    """
    run = Path(run_dir)
    report = RunReport.model_validate_json((run / "report.json").read_text())

    ok_count = sum(1 for r in report.results if r.ok)
    icon = "✅" if report.success else "❌"
    outcome = "success" if report.success else "FAILED"

    lines: list[str] = []
    lines.append(f"# {icon} {_md_escape(report.workflow_name)} — {outcome}")
    lines.append("")
    lines.append(f"- **Started:** {report.started_at}")
    lines.append(f"- **Steps:** {ok_count}/{len(report.results)} ok")
    lines.append(f"- **Heals:** {report.heal_count}")
    lines.append("")

    # -- Parameters -----------------------------------------------------
    lines.append("## Parameters")
    lines.append("")
    if report.params:
        lines.append("| Param | Value |")
        lines.append("| --- | --- |")
        for key, value in report.params.items():
            lines.append(f"| `{_md_escape(key)}` | {_md_escape(value)} |")
    else:
        lines.append("_No parameters._")
    lines.append("")

    # -- Identity-protection coverage ------------------------------------
    # Stated on every report: identity verification covers ONLY armed
    # steps; an unarmed click proceeds with no identity check at all
    # (docs/LIMITS.md). Computed over the whole bundle at run start, so
    # the numbers cover steps the run never reached.
    lines.append("## Identity protection coverage")
    lines.append("")
    if report.identity_applicable_steps:
        lines.append(
            f"**{report.identity_armed_steps} of "
            f"{report.identity_applicable_steps} click steps "
            "identity-armed.** Unarmed clicks proceed with **no identity "
            "verification** (see docs/LIMITS.md)."
        )
        if report.identity_unarmed:
            lines.append("")
            lines.append("| Unarmed step | Intent | Reason |")
            lines.append("| --- | --- | --- |")
            for unarmed in report.identity_unarmed:
                lines.append(
                    f"| `{_md_escape(unarmed.step_id)}` "
                    f"| {_md_escape(unarmed.intent)} "
                    f"| {_md_escape(unarmed.reason)} |"
                )
    else:
        lines.append(
            "_No identity-applicable (anchored click/type) steps in this "
            "workflow._"
        )
    lines.append("")

    # -- Per-step table ---------------------------------------------------
    lines.append("## Steps")
    lines.append("")
    lines.append(
        "| # | Step | Intent | Rung | Confidence | Verified | ms "
        "| Healed | OK |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, result in enumerate(report.results, start=1):
        rung = result.resolution.rung if result.resolution else "&mdash;"
        conf = (
            f"{result.resolution.confidence:.2f}"
            if result.resolution
            else "&mdash;"
        )
        healed = "\U0001fa79" if result.heal else ""
        status = "✅" if result.ok else "❌"
        # Identity (clicks) / typed-input (TYPE) verification outcome —
        # "unreadable" identity means the step proceeded on positional
        # evidence alone and is flagged, never silent.
        verified_parts = []
        if result.identity is not None:
            marker = {
                "verified": "id ✓", "mismatch": "id ✗", "unreadable": "id ⚠"
            }[result.identity.status]
            verified_parts.append(marker)
        if result.input_verified is not None:
            marker = "input ✓" if result.input_verified else "input ✗"
            if result.input_retried:
                marker += " (retried)"
            verified_parts.append(marker)
        verified = ", ".join(verified_parts) or "&mdash;"
        lines.append(
            f"| {i} | `{_md_escape(result.step_id)}` "
            f"| {_md_escape(result.intent)} | {rung} | {conf} "
            f"| {verified} | {result.elapsed_ms:.0f} | {healed} | {status} |"
        )
    lines.append("")

    # -- Screenshots: final step, every heal, any failed step --------------
    final_id = report.results[-1].step_id if report.results else None
    shown: list[tuple[str, StepResult]] = []
    for result in report.results:
        reasons = []
        if not result.ok:
            reasons.append("failed")
        if result.heal:
            reasons.append("healed")
        if result.step_id == final_id:
            reasons.append("final step")
        if reasons:
            shown.append((", ".join(reasons), result))

    if shown:
        lines.append("## Screenshots")
        lines.append("")
        for reason, result in shown:
            lines.append(
                f"### `{_md_escape(result.step_id)}` — "
                f"{_md_escape(result.intent)} ({reason})"
            )
            lines.append("")
            if result.error:
                lines.append(f"> ❌ **Error:** {_md_escape(result.error)}")
                lines.append("")
            lines.extend(_before_after_table(result))
            lines.append("")
            if result.heal:
                heal = result.heal
                applied = "applied" if heal.applied else "not applied"
                lines.append(
                    f"**Heal** (`{heal.kind}` via `{heal.rung_used}`, {applied}):"
                )
                lines.append("")
                lines.append(
                    f"- anchor `{heal.old_anchor.template}` → "
                    f"`{heal.new_anchor.template}`"
                )
                if heal.screenshot:
                    lines.append("")
                    lines.append("| Healed frame |")
                    lines.append("| --- |")
                    lines.append(
                        f"| {_img(heal.screenshot, f'{heal.step_id} heal')} |"
                    )
                lines.append("")

    # -- Rung histogram -----------------------------------------------------
    lines.append("## Rung histogram")
    lines.append("")
    lines.append("| Rung | Count | |")
    lines.append("| --- | --- | --- |")
    extras = [r for r in report.rung_counts if r not in _RUNG_ORDER]
    for rung in (*_RUNG_ORDER, *extras):
        count = report.rung_counts.get(rung, 0)
        bar = "█" * count
        lines.append(f"| `{rung}` | {count} | {bar} |")
    lines.append("")

    # -- Totals ---------------------------------------------------------------
    lines.append("## Totals")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Total time | {report.total_ms:.0f} ms |")
    lines.append(f"| Steps ok | {ok_count}/{len(report.results)} |")
    lines.append(f"| Heals | {report.heal_count} |")
    lines.append(f"| model_calls | {report.model_calls} |")
    lines.append(f"| est_model_cost_usd | ${report.est_model_cost_usd:.4f} |")
    lines.append("")

    out = run / "REPORT.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def render_bench_report(bench_json_path: Path | str, out_path: Path | str) -> Path:
    """Render a Markdown summary of a bench run.

    Args:
        bench_json_path: Path to the ``bench.json`` written by
            :func:`openadapt_flow.bench.run_bench`.
        out_path: Destination path for the Markdown report.

    Returns:
        Path to the written Markdown file.
    """
    bench = json.loads(Path(bench_json_path).read_text())
    out = Path(out_path)

    n = bench.get("n", 0)
    successes = bench.get("success_count", 0)
    rate = bench.get("success_rate", 0.0)
    icon = "✅" if successes == n and n > 0 else "❌"

    lines: list[str] = []
    lines.append(
        f"# {icon} Bench — {_md_escape(str(bench.get('workflow_name', '')))}"
    )
    lines.append("")
    lines.append(f"- **Bundle:** `{bench.get('bundle', '')}`")
    lines.append(f"- **Iterations:** {n}")
    lines.append(f"- **Success rate:** {rate:.0%} ({successes}/{n})")
    lines.append(f"- **Total ms p50:** {bench.get('total_ms_p50', 0.0):.0f}")
    lines.append(f"- **Total ms p95:** {bench.get('total_ms_p95', 0.0):.0f}")
    lines.append(f"- **Heals (total):** {bench.get('heal_count', 0)}")
    lines.append(f"- **model_calls (total):** {bench.get('model_calls', 0)}")
    lines.append(
        f"- **est_model_cost_usd (total):** "
        f"${bench.get('est_model_cost_usd', 0.0):.4f}"
    )
    lines.append("")

    lines.append("## Rung histogram (aggregate)")
    lines.append("")
    lines.append("| Rung | Count | |")
    lines.append("| --- | --- | --- |")
    rung_counts = bench.get("rung_counts", {})
    extras = [r for r in rung_counts if r not in _RUNG_ORDER]
    for rung in (*_RUNG_ORDER, *extras):
        count = rung_counts.get(rung, 0)
        lines.append(f"| `{rung}` | {count} | {'█' * count} |")
    lines.append("")

    lines.append("## Iterations")
    lines.append("")
    lines.append("| # | Success | Total ms | Heals | Run dir |")
    lines.append("| --- | --- | --- | --- | --- |")
    for it in bench.get("iterations", []):
        status = "✅" if it.get("success") else "❌"
        lines.append(
            f"| {it.get('i', '?')} | {status} | {it.get('total_ms', 0.0):.0f} "
            f"| {it.get('heal_count', 0)} | `{it.get('run_dir', '')}` |"
        )
    lines.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out

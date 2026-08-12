"""Probe OpenAICompatibleGrounder against a REAL locally served vision model.

STATUS: UNRUN. No results have been recorded with this script. A first run
attempt (2026-08-12, Ollama serving qwen3-vl:8b on loopback) was aborted when
the machine kernel-panicked under concurrent load with local model inference
as a likely contributor. Treat every number as nonexistent until a run under
the conditions below produces ``benchmark/local_grounder_probe/results.json``.

OPERATIONAL REQUIREMENTS (hard):

* **Dedicated solo run window.** Run this with NOTHING else on the machine:
  no builds, no test suites, no parallel agents, no other model servers, no
  VMs. Local VLM inference plus concurrent load starved the watchdog and
  kernel-panicked the host on the first attempt.
* **Never concurrently with builds.** Do not launch this while any compile,
  CI job, or packaging step runs anywhere on the host.
* **Explicitly small model.** Serve a small vision model (8B-class or below,
  quantized — e.g. ``qwen3-vl:8b`` / ``qwen2.5vl:7b``). Do not point this at
  a large local model.

Evidence artifact, not a CI gate. ``OpenAICompatibleGrounder`` had only ever
been exercised against mocked HTTP (``tests/test_byo_grounding_model.py``) and
an in-process fake server (``tests/test_grounder_openai_compatible_contract.py``).
This probe closes the last gap: the same adapter, unmodified, pointed at a real
OpenAI-compatible endpoint serving a real vision model (e.g. Ollama's
``/v1``), on the same dense-EMR surface and ground truth as
``benchmark/grounding_eval`` — so the numbers are directly comparable with the
July baseline there (bespoke remote-VLM grounder: 0/6 @ ~472 px median on this
surface; OCR row-anchoring: 4/6 @ ~3 px).

ENV-GATED — CI never needs a model and never runs this. Set:

    OPENADAPT_GROUNDER_BASE_URL   e.g. http://127.0.0.1:11434/v1  (required)
    OPENADAPT_GROUNDER_MODEL      e.g. qwen3-vl:8b                (required)
    OPENADAPT_GROUNDER_API_KEY    optional bearer token (loopback needs none)

Run (inside a solo window only):

    uv run python scripts/probe_local_grounder.py \
        [--out benchmark/local_grounder_probe/results.json]

Surface and truth (identical to benchmark/grounding_eval/harness.py):
``render_frame(build_dense_table(seed=1, n_rows=18), RECORD_CONDITION,
top_offset_px=12)`` -> ~51 rows; targets are the same deterministic spread
(``indices[::step][:6]``); truth is the DOM centre of each target row's Open
button (``frame.points[i][1]``). Requires the ``dev`` extra (playwright, with
its Chromium runtime installed) for the render.

The probe records EXACTLY what the grounder returns: hits, misses, and
abstentions. It does not retry, downscale, crop, or massage.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

TOL_STRICT = 40  # px — the grounding_eval REPORT.md headline tolerance
TOL_LOOSE = 60  # px — run_validation.run_grounder default
N_TARGETS = 6  # same deterministic spread as the July baseline
SEED = 1
N_ROWS = 18  # floor; renders ~51 rows, exactly as the baseline harness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        default="benchmark/local_grounder_probe/results.json",
        help="where to write the raw per-target JSON",
    )
    args = parser.parse_args()

    base_url = os.environ.get("OPENADAPT_GROUNDER_BASE_URL", "").strip()
    model = os.environ.get("OPENADAPT_GROUNDER_MODEL", "").strip()
    if not base_url or not model:
        print(
            "UNRUN: set OPENADAPT_GROUNDER_BASE_URL and OPENADAPT_GROUNDER_MODEL "
            "to point at a served OpenAI-compatible vision model "
            "(e.g. ollama serve -> http://127.0.0.1:11434/v1, qwen3-vl:8b)."
        )
        return 2
    api_key = os.environ.get("OPENADAPT_GROUNDER_API_KEY", "")

    from openadapt_flow.runtime.grounder import OpenAICompatibleGrounder
    from openadapt_flow.validation.dense_surface import (
        RECORD_CONDITION,
        build_dense_table,
        render_frame,
    )

    print(f"endpoint: {base_url}  model: {model}")
    print(f"rendering dense surface (seed={SEED}, n_rows={N_ROWS}) ...")
    table = build_dense_table(seed=SEED, n_rows=N_ROWS)
    frame = render_frame(table, RECORD_CONDITION, top_offset_px=12)
    vw, vh = frame.viewport
    print(f"rendered {len(frame.points)} rows at {vw}x{vh} px")

    indices = sorted(frame.points.keys())
    step = max(1, len(indices) // N_TARGETS)
    chosen = indices[::step][:N_TARGETS]

    grounder = OpenAICompatibleGrounder(
        base_url=base_url, model=model, api_key=api_key, timeout=600.0
    )

    rows_out: list[dict] = []
    for i in chosen:
        row = table.rows[i]
        truth = frame.points[i][1]  # DOM centre of the row's Open button
        intent = f"click Open in the row for patient {row.name} (MRN {row.mrn})"
        t0 = time.time()
        match = grounder.locate(frame.png, intent, "Open")
        latency = time.time() - t0
        if match is None:
            rec = {
                "row": i,
                "name": row.name,
                "mrn": row.mrn,
                "truth": list(truth),
                "proposed": None,
                "abstained": True,
                "error_px": None,
                "hit@40": False,
                "hit@60": False,
                "latency_s": round(latency, 2),
            }
            print(f"row {i:3d}  {row.mrn}  ABSTAIN            ({latency:.1f}s)")
        else:
            px, py = match.point
            err = math.dist((px, py), truth)
            rec = {
                "row": i,
                "name": row.name,
                "mrn": row.mrn,
                "truth": list(truth),
                "proposed": [px, py],
                "abstained": False,
                "error_px": round(err, 1),
                "hit@40": err <= TOL_STRICT,
                "hit@60": err <= TOL_LOOSE,
                "latency_s": round(latency, 2),
            }
            print(
                f"row {i:3d}  {row.mrn}  proposed ({px}, {py})  "
                f"truth {truth}  err {err:7.1f} px  "
                f"{'HIT ' if err <= TOL_STRICT else 'miss'}  ({latency:.1f}s)"
            )
        rows_out.append(rec)

    n = len(rows_out)
    hits40 = sum(r["hit@40"] for r in rows_out)
    hits60 = sum(r["hit@60"] for r in rows_out)
    abstained = sum(r["abstained"] for r in rows_out)
    errors = sorted(r["error_px"] for r in rows_out if r["error_px"] is not None)
    median_err = errors[len(errors) // 2] if errors else None
    out = {
        "meta": {
            "endpoint": base_url,
            "model": model,
            "surface": (
                "dense_surface.render_frame(build_dense_table(seed=1, n_rows=18), "
                "RECORD_CONDITION, top_offset_px=12)"
            ),
            "viewport_px": [vw, vh],
            "chosen_row_indices": list(chosen),
            "truth": "DOM centre of each row's Open button (frame.points[i][1])",
            "tol_px": {"strict": TOL_STRICT, "loose": TOL_LOOSE},
            "adapter": "openadapt_flow.runtime.grounder.OpenAICompatibleGrounder",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
        "summary": {
            "n_targets": n,
            "hits@40": hits40,
            "hits@60": hits60,
            "abstained": abstained,
            "median_error_px": median_err,
        },
        "rows": rows_out,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n")
    print(
        f"\nhit@40 {hits40}/{n}  hit@60 {hits60}/{n}  abstain {abstained}/{n}  "
        f"median err {median_err} px\nwrote {out_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Probe OpenAICompatibleGrounder against a REAL served vision model.

Works against any OpenAI-compatible endpoint: a locally served model (Ollama,
vLLM, LM Studio) or a hosted provider (e.g. Together AI at
``https://api.together.xyz/v1``). A hosted run measures model capability and
wire-protocol correctness with the real open weights; it is NOT evidence about
an on-prem deployment, which is a separate scheduled artifact.

LOCAL-RUN STATUS: UNRUN. A first local run attempt (2026-08-12, Ollama serving
qwen3-vl:8b on loopback) was aborted when the machine kernel-panicked under
concurrent load with local model inference as a likely contributor. Treat every
local number as nonexistent until a run under the conditions below produces
``benchmark/local_grounder_probe/results.json``. Hosted results live in
``benchmark/hosted_grounder_probe/``.

OPERATIONAL REQUIREMENTS for a LOCAL run (hard — irrelevant to a hosted
endpoint, which puts no inference load on this machine):

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

Run (a local run only inside a solo window):

    uv run python scripts/probe_local_grounder.py \
        [--out benchmark/local_grounder_probe/results.json] \
        [--price-in USD_PER_1M --price-out USD_PER_1M] [--max-spend USD]

Surfaces and truth. Two deterministic dense-list surfaces, both rendered by
``openadapt_flow.validation.dense_surface`` (the repo's committed fixture
source — ``benchmark/dense_surface/record_seed1.png`` is the committed render
of the first):

* ``record_seed1`` — ``render_frame(build_dense_table(seed=1, n_rows=18),
  RECORD_CONDITION, top_offset_px=12)`` -> ~51 rows at 2240x3726 px. Identical
  to benchmark/grounding_eval/harness.py, so numbers are directly comparable
  with the July baseline (bespoke remote-VLM grounder: 0/6 @ ~472 px median;
  OCR row-anchoring: 4/6 @ ~3 px).
* ``small_dense_seed2`` — ``render_frame(build_dense_table(seed=2, n_rows=18),
  REPLAY_CONDITIONS small_dense, top_offset_px=12)`` -> a visually and
  textually distinct screenshot (different names/MRNs, 12 px font, tighter
  rows, device_scale_factor 1).

Targets are the same deterministic spread on each surface
(``indices[::step][:6]``); truth is the DOM centre of each target row's Open
button (``frame.points[i][1]``). Requires the ``dev`` extra (playwright, with
its Chromium runtime installed) for the render.

The probe records EXACTLY what the grounder returns: hits, misses, and
abstentions. It does not retry, downscale, crop, or massage. It also records
the token ``usage`` of every call (via an injected recording HTTP client — the
grounder itself is unmodified) and, when per-1M-token prices are given, the
estimated spend; it stops before a call that would break ``--max-spend``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

TOL_STRICT = 40  # px — the grounding_eval REPORT.md headline tolerance
TOL_LOOSE = 60  # px — run_validation.run_grounder default
N_TARGETS = 6  # per surface; same deterministic spread as the July baseline
N_ROWS = 18  # floor; renders ~51 rows, exactly as the baseline harness


class _UsageRecordingClient:
    """httpx-backed client that records each call's token ``usage``.

    Injected into ``OpenAICompatibleGrounder`` via its ``client`` parameter so
    the adapter under test stays byte-for-byte the shipped one; this wrapper
    only observes the response on the way through.
    """

    def __init__(self) -> None:
        import httpx

        self._client = httpx.Client()
        self.last_usage: dict = {}

    def post(
        self, url: str, *, json: Any = None, headers: Any = None, timeout: Any = None
    ):  # noqa: ANN401,E501
        self.last_usage = {}
        resp = self._client.post(url, json=json, headers=headers, timeout=timeout)
        try:
            usage = resp.json().get("usage")
            if isinstance(usage, dict):
                self.last_usage = {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                }
        except ValueError:
            pass
        return resp


def _build_surfaces() -> list[tuple[str, Any, Any]]:
    """Render the two probe surfaces; return (name, table, frame) triples."""
    from openadapt_flow.validation.dense_surface import (
        RECORD_CONDITION,
        REPLAY_CONDITIONS,
        build_dense_table,
        render_frame,
    )

    small_dense = next(c for c in REPLAY_CONDITIONS if c.name == "small_dense")
    out: list[tuple[str, Any, Any]] = []
    for name, seed, cond in (
        ("record_seed1", 1, RECORD_CONDITION),
        ("small_dense_seed2", 2, small_dense),
    ):
        table = build_dense_table(seed=seed, n_rows=N_ROWS)
        frame = render_frame(table, cond, top_offset_px=12)
        vw, vh = frame.viewport
        print(f"rendered surface {name}: {len(frame.points)} rows at {vw}x{vh} px")
        out.append((name, table, frame))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        default="benchmark/local_grounder_probe/results.json",
        help="where to write the raw per-target JSON",
    )
    parser.add_argument(
        "--price-in",
        type=float,
        default=0.0,
        help="endpoint price in USD per 1M input tokens (0 = unknown/local)",
    )
    parser.add_argument(
        "--price-out",
        type=float,
        default=0.0,
        help="endpoint price in USD per 1M output tokens (0 = unknown/local)",
    )
    parser.add_argument(
        "--max-spend",
        type=float,
        default=5.0,
        help="hard cap in USD; the probe stops once the estimate reaches it",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=256,
        help=(
            "completion-token budget per call (adapter default 256; raise for "
            "a hosted reasoning model, which spends the budget on reasoning "
            "before emitting the coordinate JSON)"
        ),
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

    print(f"endpoint: {base_url}  model: {model}")
    surfaces = _build_surfaces()

    recording_client = _UsageRecordingClient()
    grounder = OpenAICompatibleGrounder(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=600.0,
        max_tokens=args.max_tokens,
        client=recording_client,
    )

    spend_usd = 0.0
    capped = False
    rows_out: list[dict] = []
    for surface_name, table, frame in surfaces:
        indices = sorted(frame.points.keys())
        step = max(1, len(indices) // N_TARGETS)
        chosen = indices[::step][:N_TARGETS]
        for i in chosen:
            if spend_usd >= args.max_spend:
                capped = True
                print(
                    f"SPEND CAP: estimate ${spend_usd:.2f} >= ${args.max_spend:.2f}; stopping."
                )
                break
            row = table.rows[i]
            truth = frame.points[i][1]  # DOM centre of the row's Open button
            intent = f"click Open in the row for patient {row.name} (MRN {row.mrn})"
            t0 = time.time()
            match = grounder.locate(frame.png, intent, "Open")
            latency = time.time() - t0
            usage = dict(recording_client.last_usage)
            pt = usage.get("prompt_tokens") or 0
            ct = usage.get("completion_tokens") or 0
            call_usd = (pt * args.price_in + ct * args.price_out) / 1e6
            spend_usd += call_usd
            rec = {
                "surface": surface_name,
                "row": i,
                "name": row.name,
                "mrn": row.mrn,
                "truth": list(truth),
                "latency_s": round(latency, 2),
                "usage": usage,
                "est_cost_usd": round(call_usd, 6),
            }
            if match is None:
                rec.update(
                    proposed=None,
                    abstained=True,
                    error_px=None,
                    **{"hit@40": False, "hit@60": False},
                )
                print(
                    f"{surface_name}  row {i:3d}  {row.mrn}  ABSTAIN            "
                    f"({latency:.1f}s)"
                )
            else:
                px, py = match.point
                err = math.dist((px, py), truth)
                rec.update(
                    proposed=[px, py],
                    abstained=False,
                    error_px=round(err, 1),
                    **{"hit@40": err <= TOL_STRICT, "hit@60": err <= TOL_LOOSE},
                )
                print(
                    f"{surface_name}  row {i:3d}  {row.mrn}  proposed ({px}, {py})  "
                    f"truth {truth}  err {err:7.1f} px  "
                    f"{'HIT ' if err <= TOL_STRICT else 'miss'}  ({latency:.1f}s)"
                )
            rows_out.append(rec)
        if capped:
            break

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
            "surfaces": {
                name: {"viewport_px": list(frame.viewport)}
                for name, _table, frame in surfaces
            },
            "surface_spec": (
                "dense_surface.render_frame(build_dense_table(seed=S, n_rows=18), "
                "COND, top_offset_px=12); record_seed1: S=1, COND=RECORD_CONDITION; "
                "small_dense_seed2: S=2, COND=small_dense"
            ),
            "truth": "DOM centre of each row's Open button (frame.points[i][1])",
            "tol_px": {"strict": TOL_STRICT, "loose": TOL_LOOSE},
            "adapter": "openadapt_flow.runtime.grounder.OpenAICompatibleGrounder",
            "max_tokens": args.max_tokens,
            "price_usd_per_1m": {"input": args.price_in, "output": args.price_out},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
        "summary": {
            "n_targets": n,
            "hits@40": hits40,
            "hits@60": hits60,
            "abstained": abstained,
            "median_error_px": median_err,
            "est_spend_usd": round(spend_usd, 4),
            "spend_capped": capped,
        },
        "rows": rows_out,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n")
    print(
        f"\nhit@40 {hits40}/{n}  hit@60 {hits60}/{n}  abstain {abstained}/{n}  "
        f"median err {median_err} px  est spend ${spend_usd:.4f}\nwrote {out_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Hosted grounder probe — real model, real wire (Together AI)

**Date: 2026-08-12.** First run of `OpenAICompatibleGrounder` against a real
served model. Endpoint: `https://api.together.xyz/v1` (Together AI serverless).
Driver: `scripts/run_hosted_probe.sh` -> `scripts/probe_local_grounder.py`;
the adapter under test is the shipped
`openadapt_flow.runtime.grounder.OpenAICompatibleGrounder` (an injected
recording HTTP client observes token usage; request construction and reply
parsing are the adapter's own).

**Caveat (read first).** Hosted weights are NOT an on-prem deployment. This
probe measures (a) the adapter's wire-protocol correctness against a real
commercial OpenAI-compatible endpoint and (b) the capability of the served
open-weights model on our dense-list surfaces. It says nothing about on-prem
latency, quantized local builds, or appliance behaviour; the local/on-prem run
remains a separate scheduled artifact (`benchmark/local_grounder_probe/`,
still UNRUN).

## Model availability on Together (measured 2026-08-12, models API)

The classic Qwen VL family is listed but **dedicated-endpoint only** — every
serverless chat call returns `model_not_available`:

| model id | serverless? | listed price in/out (USD per 1M) |
|---|---|---|
| `Qwen/Qwen2-VL-72B-Instruct` | no (dedicated only) | 1.20 / 1.20 |
| `Qwen/Qwen2.5-VL-72B-Instruct` | no (dedicated only) | 1.95 / 8.00 |
| `Qwen/Qwen3-VL-8B-Instruct` | no (dedicated only) | 0.18 / 0.68 |
| `Qwen/Qwen3-VL-32B-Instruct` | no (dedicated only) | 0.50 / 1.50 |
| `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` | no (dedicated only) | n/a |
| `Qwen/Qwen3.5-9B` | **yes** (image-capable, reasoning) | 0.17 / 0.25 |
| `Qwen/Qwen3.7-Plus` | streaming-only (image-capable) | 0.32 / 1.28 |

Creating a dedicated GPU endpoint for the Qwen VL pair (8B at $0.09/min,
32B at $0.18/min) was prepared but the session's permission system denied the
endpoint-creation call, and per standing instructions no workaround was
attempted. The probed pair is therefore the two hosted Qwen models the shipped
adapter can reach: `Qwen/Qwen3.5-9B` (smallest image-capable serverless Qwen)
and `Qwen/Qwen3.7-Plus` (strongest hosted Qwen reachable at all — which turns
out to be unreachable by a non-streaming client; measured below).

## Setup

- Surfaces (both rendered by `openadapt_flow.validation.dense_surface`, the
  committed fixture source of `benchmark/dense_surface/record_seed1.png`):
  - `record_seed1`: seed 1, RECORD_CONDITION — 51 rows, 2240x3726 px
    (device_scale_factor 2). Identical to `benchmark/grounding_eval`, so
    directly comparable with the July baselines.
  - `small_dense_seed2`: seed 2, `small_dense` condition — 51 rows,
    1120x1401 px (12 px font, tighter rows, device_scale_factor 1).
- Targets: 6 per surface, the deterministic spread `indices[::step][:6]`
  (rows 0, 8, 16, 24, 32, 40). Truth = DOM centre of each row's Open button.
  Tolerances: hit@40 px (headline) and hit@60 px.
- Prompt (the adapter's `_PROMPT`, verbatim template):

  ```
  You are grounding a UI automation target on a screenshot.
  Target intent: {intent}
  Target text label (may be stale): {ocr_text}

  Reply with ONLY a JSON object of pixel coordinates for the point to
  click, e.g. {"x": 123, "y": 45}. If the target is not visible, reply
  with ONLY {"x": null, "y": null}.
  ```

  with `intent = "click Open in the row for patient {name} (MRN {mrn})"` and
  `ocr_text = "Open"`.

## Run 1 — `Qwen/Qwen3.5-9B`, adapter default `max_tokens=256`

**hit@40 0/12 · hit@60 0/12 · abstain 11/12 · 1 miss @ 2594 px · $0.0108**

Every abstain shows `completion_tokens: 256` — the hosted reasoning model
spends the whole fixed budget on reasoning and returns EMPTY content, which
the adapter (correctly, fail-safe) treats as an abstain. The single completed
reply (219 tokens) proposed (957, 623) for a truth of (2059, 2971).

| surface | row | MRN | proposed | truth | err px | result | latency s |
|---|---|---|---|---|---|---|---|
| record_seed1 | 0 | MG584224 | — | (2059, 251) | — | abstain (truncated) | 7.2 |
| record_seed1 | 8 | MG129724 | — | (2059, 795) | — | abstain (truncated) | 5.8 |
| record_seed1 | 16 | MG499721 | — | (2059, 1339) | — | abstain (truncated) | 8.3 |
| record_seed1 | 24 | MG536396 | — | (2059, 1883) | — | abstain (truncated) | 5.3 |
| record_seed1 | 32 | PLl9181 | — | (2059, 2427) | — | abstain (truncated) | 4.3 |
| record_seed1 | 40 | 2OO633 | (957, 623) | (2059, 2971) | 2593.7 | miss | 3.6 |
| small_dense_seed2 | 0 | RC903088 | — | (1015, 118) | — | abstain (truncated) | 5.0 |
| small_dense_seed2 | 8 | MG551589 | — | (1015, 318) | — | abstain (truncated) | 3.2 |
| small_dense_seed2 | 16 | MG687737 | — | (1015, 518) | — | abstain (truncated) | 3.5 |
| small_dense_seed2 | 24 | MG899943 | — | (1015, 718) | — | abstain (truncated) | 5.0 |
| small_dense_seed2 | 32 | PLl9444 | — | (1015, 918) | — | abstain (truncated) | 2.6 |
| small_dense_seed2 | 40 | 5OO675 | — | (1015, 1118) | — | abstain (truncated) | 3.7 |

This run motivated the `max_tokens` constructor parameter on
`OpenAICompatibleGrounder` (pinned by `tests/test_grounder_max_tokens.py`):
the fixed 256 was a 100% availability loss against a hosted reasoning model.
Safety was never at risk — truncation abstains.

## Run 2 — `Qwen/Qwen3.7-Plus` (strongest hosted Qwen), `max_tokens=256`

**hit@40 0/12 · hit@60 0/12 · abstain 12/12 · $0.00 · ~1 s/call**

Together serves this model streaming-only: every non-streaming
`chat/completions` request is rejected (`This model only supports streaming`),
the adapter sees a 4xx and abstains — 12/12, no crash, no spend. The shipped
adapter cannot measure this model's grounding capability; a streaming client
would be a new adapter feature, not a probe change.

## Run 3 — `Qwen/Qwen3.5-9B`, `max_tokens=2048` (the capability number)

**hit@40 0/12 · hit@60 0/12 · abstain 6/12 · 6 misses, median err 1792 px ·
$0.0137**

| surface | row | MRN | proposed | truth | err px | result | latency s |
|---|---|---|---|---|---|---|---|
| record_seed1 | 0 | MG584224 | (958, 76) | (2059, 251) | 1114.8 | miss | 8.5 |
| record_seed1 | 8 | MG129724 | — | (2059, 795) | — | abstain (truncated @2048) | 19.2 |
| record_seed1 | 16 | MG499721 | (684, 190) | (2059, 1339) | 1791.9 | miss | 3.9 |
| record_seed1 | 24 | MG536396 | — | (2059, 1883) | — | abstain (truncated @2048) | 24.3 |
| record_seed1 | 32 | PLl9181 | (929, 608) | (2059, 2427) | 2141.4 | miss | 4.3 |
| record_seed1 | 40 | 2OO633 | (942, 758) | (2059, 2971) | 2478.9 | miss | 19.4 |
| small_dense_seed2 | 0 | RC903088 | — | (1015, 118) | — | abstain (701 tok, model declined) | 11.3 |
| small_dense_seed2 | 8 | MG551589 | (925, 458) | (1015, 318) | 166.4 | miss | 10.3 |
| small_dense_seed2 | 16 | MG687737 | — | (1015, 518) | — | abstain (truncated @2048) | 20.0 |
| small_dense_seed2 | 24 | MG899943 | — | (1015, 718) | — | abstain (truncated @2048) | 27.2 |
| small_dense_seed2 | 32 | PLl9444 | (870, 768) | (1015, 918) | 208.6 | miss | 14.3 |
| small_dense_seed2 | 40 | 5OO675 | — | (1015, 1118) | — | abstain (466 tok, model declined) | 4.8 |

Reading the misses: on `record_seed1` (device_scale_factor 2) the proposals
cluster around x≈940 against a truth of x=2059 — consistent with the model
answering roughly in CSS-pixel space (1120 CSS px wide) on a 2240-device-px
screenshot — and even after a hypothetical 2x rescale the y is still several
hundred px (many rows) off. On `small_dense_seed2` (scale factor 1) the errors
drop to 166–209 px but remain 5–7 rows away from the right patient. This is
the July failure mode again — column roughly right, row wrong — now
reproduced on hosted, unquantized open weights, so it is not an artifact of
the local 4-bit build.

## Prompt-token footprint (from the API `usage` field)

- `record_seed1` (2240x3726): ~8,233 prompt tokens/call.
- `small_dense_seed2` (1120x1401): ~1,652 prompt tokens/call.

## Total spend

$0.0245 across the three runs (24 billed calls + 12 free-failing calls), plus
under $0.01 of model-discovery smoke calls. Cap was $5; nothing approached it.

## Comparison (same `record_seed1` surface and truth)

| grounder | hit@40 | median err |
|---|---|---|
| OCR row-anchoring, local, $0 (`benchmark/grounding_eval`, July) | 4/6 (88–100% over 50 targets) | ~3 px |
| Bespoke remote VLM, local 4-bit Qwen3-VL-4B (July) | 0/6 | ~472 px |
| `OpenAICompatibleGrounder` -> hosted `Qwen/Qwen3.5-9B` @2048 (this run) | 0/6 | ~2141 px |

**Bottom line.** The wire protocol works end-to-end against a real commercial
endpoint (auth, image transport, parsing, fail-safe abstain on truncation,
4xx, and refusal all behaved exactly as the contract tests pin). The
single-shot VLM grounding approach stays broken on dense lists with real
hosted open weights — every wrong proposal was a wrong ROW, which is precisely
the silent-wrong-record class the ladder's OCR anchoring + identity band exist
to prevent. The grounder rung remains bottom-of-ladder and risk-gated.

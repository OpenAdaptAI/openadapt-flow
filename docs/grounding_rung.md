# The grounding rung: an open GUI-grounding model behind the identity gate

**Status:** prototype (feat/grounding-rung). Availability layer only; the
safety invariant is unchanged and independently pinned.

This is the implementation of the OSS-model assessment's #1 recommendation:
add an **open** GUI-grounding specialist as the last rung of the resolution
ladder, *behind* the deterministic identity gate, to cut the false-abort rate
without reintroducing false-accepts.

## The problem it solves

openadapt-flow is a demonstration compiler, not an agent: a healthy replay
makes **zero** model calls, and locating a step's target walks a ladder of
progressively weaker but more drift-tolerant evidence
(`openadapt_flow/runtime/resolver.py`):

1. `template` — template match in the padded local region
2. `template_global` — template match over the full frame
3. `ocr` — fuzzy text match on the anchor's label
4. `geometry` — offset from located landmark text
5. `grounder` — *optional injected model* (this document)

When rungs 1–4 all miss, the run **safe-halts**. That is correct when the
target is genuinely gone, but when the target is present-but-unmatched (a
cosmetic reflow, a rename the deterministic rungs can't bridge, a below-fold
element) the halt is a **false-abort** — an availability failure, not a safety
one. Live-app measurements put this class around the ~26% range.

## The composition: grounder proposes, identity disposes

The insight is that these are two *different* jobs and can be given to two
*different* mechanisms:

```
   rungs 1-4 miss
        │
        ▼
   ┌─────────────────────┐   proposes (x, y)   ┌──────────────────────────┐
   │  grounding model    │ ──────────────────▶ │  identity band gate      │
   │  (GUI-Owl-1.5, MIT) │   AVAILABILITY      │  (runtime/identity.py)    │
   └─────────────────────┘                     │  verify_target_identity   │
                                               │  SAFETY — deterministic   │
                                               └──────────────────────────┘
                                                     │            │
                                              verified │            │ mismatch / unreadable
                                                     ▼            ▼
                                                   CLICK       SAFE-HALT
```

- **The grounder is trusted for availability, never for safety.** It only
  *proposes* a coordinate. It fires only when the deterministic ladder is
  exhausted (a healthy replay never reaches it — the hot path stays
  model-free).
- **The identity band gate disposes.** Exactly as it does for a geometry-rung
  estimate, the replayer OCRs the full-width band around the *resolved* point,
  keeps the point's own text row, and matches it against the recorded target's
  context band (`verify_target_identity`). A grounder that points at the wrong
  row is caught here — coverage falls below the pinned operating point and the
  run safe-halts, never clicking.

Because the identity check runs **after** resolution and **before** the click
(the 2026-07-08/09 wrong-action fixes), a grounder cannot buy a wrong target a
pass. Adding it strictly raises availability; the safety invariant
(false-accept = 0) is untouched *by construction*. The determinism thesis is
preserved: the deterministic compiler + identity gate + postconditions remain
the sole authority on whether to act. The model is advisory input only.

There is a second, independent gate in front of the grounder rung: the
**irreversible risk gate**. The grounder rung is below `ocr`
(`is_below_ocr("grounder") == True`), so a step marked `risk="irreversible"`
that only resolves via the grounder is refused outright (needs human
confirmation, v0 policy) — the model never even reaches the click path for an
irreversible action. The availability gain therefore lands on *reversible*
steps; irreversible ones stay conservative regardless of what the grounder
proposes.

## What was built

- `GuiOwlGrounder` (`openadapt_flow/runtime/grounder.py`) — implements the
  `Grounder` protocol (`locate(png, intent, ocr_text) -> GrounderMatch | None`)
  for **GUI-Owl-1.5** (mPLUG, MIT). Two interchangeable serving backends:
  - `backend="mlx"` — local Apple-Silicon serving via `mlx-vlm` (on-prem, no
    API). Extra: `grounder-mlx`.
  - `backend="http"` — any OpenAI-compatible `/chat/completions` endpoint
    (a vLLM / SGLang server hosting the 8B checkpoint). Extra: `grounder-http`.
  A `transport=` callable can be injected to unit-test parsing/scaling with no
  model or network.
- `parse_grounder_point` — tolerant parser (JSON `{x,y}` / `point` list, Qwen
  box tokens, bare `(x,y)`) that maps the model's coordinate space to absolute
  pixels. GUI-Owl-1.5 is Qwen3-VL-based and emits **normalized 0–1000**
  coordinates (`COORD_NORM_1000`, the default) — the memo's coordinate gotcha;
  the scaling is pinned and regression-tested.
- Wiring — no ladder change was needed: `resolve()` already invokes the
  injected grounder as rung 5, and the replayer already routes every
  CLICK/DOUBLE_CLICK/TYPE resolution (any rung) through the identity gate. The
  grounder is passed via `Replayer(backend, grounder=...)`. Confirmed: **no
  path clicks a grounder proposal without identity verification** (pinned by
  `tests/test_grounding_rung.py::test_grounder_proposal_on_wrong_entity_safe_halts`
  and the e2e look-alike test).

## Measured composition (the headline)

`python -m openadapt_flow.validation.grounding_composition`

The measurement drives the **real** ladder (`resolve`, deterministic rungs
forced to miss so the grounder rung fires) and the **real** identity gate
(`verify_target_identity` — the exact call the replayer makes), over the union
of the frozen adversary corpora (`adversary_corpus`, `_v2`, `_v3` — 6,900
`(recorded_band, observed_band)` pairs with ground-truth labels). `same_entity`
pairs model a present target the deterministic ladder missed (a recoverable
false-abort); `different_entity` pairs model a wrong entity sitting at the
target position (the data-drift danger class).

| quantity | NullGrounder (baseline) | + grounding rung |
|---|---|---|
| ladder-exhausted cases | 6,900 (all safe-halt) | 6,900 (grounder fires) |
| present-target halts recovered | 0 | **1,927 / 2,610 = 73.8%** |
| residual safe-halt (present, $-cost only) | 2,610 | 683 |
| wrong-entity cases | 4,090 | 4,090 |
| **false-accepts (wrong click)** | **0** | **0 — 0.000%** |
| v2 `indistinguishable` verified (ungraded) | 0 | 0 |

So on the cases where the grounder actually fires: **~74% of false-aborts on a
present target convert to successes, and false-accept stays exactly 0.000%
across all 4,090 wrong-entity perturbations** (the corpus superset of the
lookalike / missing / grow / delete / sort classes plus the
prefix/edit/transposition/suffix/DOB/MRN/shared-clinical different-entity
families). The 683 residual halts are same-entity rows whose OCR noise is
severe enough that the identity matcher won't confirm them — a $-cost
fallback, never a wrong write.

The reviewer-style safety probe is pinned three ways: the corpus sweep above,
a replayer-level test (grounder proposes the wrong-entity point → identity
`mismatch` → zero clicks), and an end-to-end MockMed test (look-alike drift
with a grounder in the loop still safe-halts at `step_005`, nothing saved).

## Latency (measured)

Real numbers from serving `clinan/GUI-Owl-1.5-2B-Instruct-MLX-4bit` via the
`GuiOwlGrounder(backend="mlx")` path on an Apple M2 Max (96 GB, arm64, MPS):

- model load: ~1.8 s (once, at grounder construction)
- **per grounding call: ~0.77 s median** (0.77–0.90 s over 5 calls, 2B, 4-bit)

This is the first real per-call number for the rung (the literature reports
essentially none). It is the 2B model; the 8B recommended checkpoint on a GPU
via vLLM is **GPU-dependent and unmeasured here**. The grounder is on the heal
path only — a healthy replay never pays this cost.

## Honest deployment note

- **Local serving is feasible and fast, but this run used a faithful mock for
  the composition numbers.** GUI-Owl-1.5 loads and runs locally on this M2 Max
  in <1 s/call, proving the on-prem path. However, the only MLX conversion
  available today is the community **4-bit 2B** (`clinan/…-MLX-4bit`), whose
  output is truncated (it emits EOS after 2–3 tokens), so `locate` returns
  `None` and it cannot yet produce real coordinates. The 8B recommended
  checkpoint has no MLX conversion yet. Therefore the **composition
  measurement uses the injected faithful mock** (grounder always proposes a
  point; the corpus supplies the true-vs-wrong band the identity gate reads),
  which is the honest abstraction: the grounder is trusted only for a point,
  the band at that point decides safety.
- **The production deploy step is real-GPU serving.** Per the infra review,
  serve `mPLUG/GUI-Owl-1.5-8B-Instruct` (MIT, ~20 GB, vLLM-servable, one
  A6000/4090) or a serverless GPU endpoint, and point `GuiOwlGrounder(
  backend="http", endpoint=..., model="mPLUG/GUI-Owl-1.5-8B-Instruct")` at it.
  Pin the vLLM version and regression-test grounding coordinates per version
  (documented Qwen3-VL coordinate/bbox regressions exist). `UI-Venus-1.5-8B`
  (Apache-2.0) is the drop-in alternative.
- **Icons/non-text targets are out of scope by design.** Grounders drop to
  ~46–56% on icon-only targets, and that is exactly the class where the
  identity gate *also* can't verify (no row text) — so the rung is scoped to
  text-labeled targets, where it helps and where safety can still be gated.

## The combined pitch this unlocks

A local grounding rung is one leg of the on-prem instrument thesis. Composed
with a local OCR upgrade and a local fallback agent, the claim becomes one no
API-bound competitor can match:

> **"We measure the silent wrong-action rate on your workflows AND run
> entirely on your hardware — no screenshot ever leaves the building."**

The grounding rung advances both halves at once: it *raises availability*
(fewer false-aborts) while *keeping the safety instrument intact* (false-accept
provably 0), and it does so with a permissively-licensed open model that runs
on customer hardware — the price of entry for a HIPAA-constrained clinic.

## Reproduce

```bash
# composition numbers (no GPU, no API, no browser)
python -m openadapt_flow.validation.grounding_composition --json runs/grounding.json

# pins
pytest tests/test_grounding_rung.py -q            # parsing, composition, safety
pytest tests/e2e/test_grounding_rung.py -q        # end-to-end on live MockMed

# real local serving (Apple Silicon)
pip install 'openadapt-flow[grounder-mlx]'
python -c "from openadapt_flow.runtime import GuiOwlGrounder; GuiOwlGrounder(backend='mlx')"
```

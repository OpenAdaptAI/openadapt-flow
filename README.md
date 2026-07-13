# openadapt-flow

[![CI](https://github.com/OpenAdaptAI/openadapt-flow/actions/workflows/ci.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-flow/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/openadapt-flow)](https://pypi.org/project/openadapt-flow/)
[![Python](https://img.shields.io/pypi/pyversions/openadapt-flow)](https://pypi.org/project/openadapt-flow/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Record a GUI workflow once. Replay it deterministically, locally, for free.
A model only touches the script to repair it.

![One demonstration, two UIs, same compiled workflow — the right side self-heals under a theme it has never seen](docs/showcase/demo.gif)

*Real screenshots from the two runs in [`docs/showcase/`](docs/showcase).
Left: the UI the demo was recorded on. Right: a theme it had never seen — each
step re-resolves through OCR or geometry, and each fix is written back to the
script as a reviewable diff. Zero model calls on either side.*

**Safety, stated honestly.** It halts instead of guessing, and we measure how
often it could still resolve the wrong target under UI drift — then publish it.
Read [what it doesn't do yet](docs/LIMITS.md) and
[how we test it](docs/validation/VALIDATION.md), including five adversarial
rounds against our own wrong-target check.

## Try it

```bash
pip install openadapt-flow && playwright install chromium

openadapt-flow demo-record --out rec                     # record a demonstration
openadapt-flow compile rec --out bundle --name my-task   # compile it
openadapt-flow replay bundle                             # replay: local, $0
openadapt-flow replay bundle --drift theme               # drift the UI, watch it heal
```

The last two commands serve the bundled MockMed demo app and write an
illustrated `REPORT.md` per run. Pass `--url` to replay against your own app;
recorded parameter values are the defaults and `--param` overrides them.

## How it works

Computer-use agents re-reason through your task with a large model on every
run. That's the right shape for a task nobody has automated before, and the
wrong one for the 500th referral this month. openadapt-flow compiles the
demonstration instead.

Each compiled step carries a template crop, an OCR label, geometry landmarks,
and postconditions derived from what the demo actually changed on screen. At
replay time a resolution ladder tries them in order: local template match,
global template match, OCR, landmark geometry, then (optionally) a grounding
model. Healthy scripts never leave the first rung. Milliseconds, no model
calls, no per-run cost.

When the UI drifts, a lower rung still finds the target and the fix lands in
the bundle as a diff you can review. When the screen stops matching
expectations entirely, the run halts with a report instead of guessing, and
steps tagged irreversible won't act on a low-confidence match at all.

The runtime is vision-only (PNG in, clicks and keys out) behind a small
`Backend` protocol. The reference headless-browser backend is the production
path — it's why the whole loop runs in CI with no OS permissions. The same
four-method protocol already has two more backends, no rewrites: a
`WindowsBackend` (desktop over the WAA HTTP contract, experimental) and a
`FreeRDPBackend` for pixel-only RDP (its frame decode is proven against a
real captured RDP frame). Browser is what we'd put in production today;
desktop and RDP are real but earlier on the maturity curve.

## When to use something else

Reach for a selector-based tool when a stable programmatic surface exists and
you control it:

- **Playwright codegen / Selenium IDE** — you own a web app with stable DOM
  selectors and only need to drive *that* app. A recorded selector script is
  simpler and faster than vision. Use them.
- **Selector RPA (UiPath, Power Automate Desktop)** — you want a large vendor
  suite, a visual designer, and enterprise connectors, and your targets expose
  reliable selectors/UIA automation IDs.

openadapt-flow is for the case those tools struggle with: **the same
demonstration has to run across substrates that don't share a selector model**
— a browser today, a Citrix/RDP pixel stream or a legacy desktop app tomorrow —
and you need it to be *deterministic and auditable* rather than re-reasoned by a
model each run.

Why vision anchors instead of selectors, even when a DOM is available?

- **Determinism + review.** Each step resolves through a fixed ladder (template
  → OCR → geometry) and every self-heal is written back as a reviewable diff.
  No hidden model reasoning on the hot path.
- **Cross-substrate.** A pixel-in/clicks-out contract is the one interface that
  exists over RDP/Citrix, a native desktop, and a browser alike, so one
  compiled workflow spans all three.
- **Identity + safety layer.** The runtime verifies it's acting on the *right*
  target and halts instead of guessing; steps tagged irreversible won't act on a
  low-confidence match. That gate is substrate-independent.

Honest caveat: where a real DOM or UIA tree exists, selectors are often the
better signal — and we take that seriously. We ship a DOM-selector benchmark
arm (`openadapt_flow.benchmark.dom_arm`) that steelmans a hand-written
Playwright script against the same schedule, precisely so the vision claim is
tested against the real incumbent rather than an agent. Vision-first is the
floor that works on every substrate, not a rejection of structure; where a DOM
is present, consuming it is on the roadmap.

## Proof

Every CI run records a demonstration, compiles it, and checks:

| Scenario | Outcome |
|---|---|
| Baseline replay ×3 | all steps `template` rung, 0 heals, 0 model calls |
| Theme drift | succeeds; 8/8 anchors healed; healed bundle replays clean |
| Moved buttons | succeeds via global template search |
| Renamed buttons | succeeds via landmark geometry |
| Surprise modal | fails loudly, naming the violated postcondition |
| Non-recorded parameter | substituted and verified by OCR of the final screen |

Artifacts: [baseline run report](docs/showcase/baseline-run/REPORT.md) ·
[theme-drift run report](docs/showcase/theme-drift-run/REPORT.md).

Compiled workflows can also be emitted as Agent Skills or MCP servers
(`emit-skill` / `emit-mcp`), so other agents can invoke them.

## Benchmark

![OpenEMR: compiled replay vs computer-use agent, latency and cost](benchmark/openemr/latency_cost.png)

The lead result is on a real third-party app: the official OpenEMR public
demo (fake patients only, resets daily). We ran an 18-step add-patient-note
workflow both ways — log in, find a patient, scroll a dense dashboard, add
a note — with a distinct note value each run and the same OCR success
check on both arms: 20 compiled replays against 10 runs of a
claude-sonnet-5 computer-use agent. Compiled went 20/20 at 39.2s (p50)
with zero model calls; the agent went 10/10 at 70.4s (p50), about $0.55
per run at list price ($5.52 total for the 10 runs, with prompt caching
and hard cost caps enforced in the harness). It's a shared public demo
that other users mutate and that resets daily — not CI-reproducible, and
the sample is small. Correctness alone (no agent arm, 5/5 fresh browsers,
zero model calls, closed-loop scrolling) is in
[docs/showcase-openemr/FINDINGS.md](docs/showcase-openemr/FINDINGS.md).
Full numbers, methodology, and caveats:
[benchmark/openemr/BENCHMARK.md](benchmark/openemr/BENCHMARK.md).

For a controlled, CI-reproducible comparison — the methodology anchor — we
ran the bundled MockMed task both ways on 2026-07-08 with the same OCR
success check: 100 compiled replays against 20 runs of the same agent.
Both arms went 100 for 100 and 20 for 20, so on an app this simple the
story isn't success rate. It's that a compiled replay finishes in 4.9s
(p50; 5.1s p95) with zero model calls, while the agent takes 37.5s (p50;
43.4s p95) at about $0.27 per run at list price, every run, forever. Full
numbers, methodology, and caveats:
[benchmark/BENCHMARK.md](benchmark/BENCHMARK.md).

## Status

v0: 864 tests, drift matrix in CI. Solid for the reference browser backend.
`DESIGN.md` has the module contracts; `docs/L1_INTEGRATION.md` covers feeding
layered clinical-data platforms.

## Privacy (PHI)

For regulated deployments, PHI scrubbing on the persist/log paths is provided by
the optional `privacy` extra (Presidio-backed
[openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy)):

```bash
pip install 'openadapt-flow[privacy]' && python -m spacy download en_core_web_trf
export OPENADAPT_FLOW_SCRUB=on          # scrub REPORT.md + logs, fail closed
```

The shareable `REPORT.md` and console logs are scrubbed; the compiled bundle and
`report.json` keep literal identifiers on purpose (identity check + audit trail)
and are protected by a documented boundary. Identity crops sent to the on-prem
VLM appliance are deliberately not scrubbed — the control there is
on-prem-only + no-retention. Full map: [docs/PRIVACY.md](docs/PRIVACY.md).

## Development

```bash
git clone https://github.com/OpenAdaptAI/openadapt-flow && cd openadapt-flow
pip install -e '.[dev]' && playwright install chromium
pytest -q
```

The demo GIF is generated from real run artifacts by
`scripts/make_demo_gif.py`. MIT license.

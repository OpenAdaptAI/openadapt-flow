# openadapt-flow

[![CI](https://github.com/OpenAdaptAI/openadapt-flow/actions/workflows/ci.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-flow/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/openadapt-flow)](https://pypi.org/project/openadapt-flow/)
[![Python](https://img.shields.io/pypi/pyversions/openadapt-flow)](https://pypi.org/project/openadapt-flow/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Record a GUI workflow once. Replay it deterministically, locally, for free.
A model only touches the script to repair it.

![One demonstration, two UIs, same compiled workflow — the right side self-heals under a theme it has never seen](docs/showcase/demo.gif)

*Every frame above is a real screenshot saved by the replayer during the two
runs in [`docs/showcase/`](docs/showcase). Left: the UI the demonstration was
recorded on — every step resolves by template match in ~10ms. Right: a dark
theme the workflow has never seen — steps re-resolve through OCR and
geometry, and each fix is written back to the script as a reviewable diff.
Zero model calls on either side.*

## Try it in five commands

```bash
pip install openadapt-flow && playwright install chromium

openadapt-flow demo-record --out rec              # 1. record a demonstration
openadapt-flow compile rec --out bundle --name my-task   # 2. compile it
openadapt-flow replay bundle                      # 3. replay: deterministic, local, $0
openadapt-flow replay bundle --drift theme        # 4. drift the UI — watch it heal
```

Steps 3–4 serve the bundled MockMed demo app automatically and write an
illustrated `REPORT.md` per run — what ran, what it saw, what healed. To
replay against your own app, pass `--url` (recorded parameter values are the
defaults; `--param note="Booking 3 months"` overrides them).

## Why compile, when agents exist?

Computer-use agents re-reason through your task with a large model on every
run: slow, non-deterministic, and billed per run. That's the right shape for
a task nobody has automated before, and the wrong one for the 500th referral
this month. openadapt-flow makes the other bet:

- **Compile once.** A recorded demonstration becomes an editable script:
  every step carries redundant visual evidence (template crop, OCR label,
  geometry landmarks), the action, and postcondition assertions derived from
  what actually changed on screen during the demo.
- **Replay for free.** A resolution ladder finds each target: local template
  match → global template match → OCR → landmark geometry → (optional)
  grounding model. Healthy scripts never leave the first rung — milliseconds,
  zero model calls, zero marginal cost.
- **Heal on drift, as a diff.** When the UI shifts, a lower rung still finds
  the target and the fix is written back to the bundle for review. The
  automation gets cheaper and more robust over time instead of re-reasoning
  the same eleven clicks.
- **Halt on surprise.** Postconditions verify every step. If the screen stops
  matching expectations (an unexpected dialog, a changed process), the run
  stops with an illustrated report instead of guessing. Steps tagged
  irreversible refuse to act on low-confidence resolutions at all.

The runtime is **vision-only** — PNG in, clicks and keys out — behind a small
`Backend` protocol. The reference backend drives a headless browser, which is
why the whole loop runs in CI with no OS permissions. Native desktop and RDP
backends are planned adapters behind the same protocol.

## Proof, not promises

The E2E suite records a demonstration, compiles it, and proves the claims on
every CI run:

| Scenario | Outcome |
|---|---|
| Baseline replay ×3 | all steps `template` rung, 0 heals, 0 model calls |
| Theme drift (dark UI) | succeeds; 8/8 anchors healed; healed bundle replays all-`template` |
| Moved buttons | succeeds via global template search, heals |
| Renamed buttons | succeeds via landmark geometry, heals refresh the labels |
| Surprise modal | **fails loudly**, naming the violated postcondition with screenshots |
| Non-recorded parameter | substituted and verified by OCR of the final screen |

Browse the artifacts: [baseline run report](docs/showcase/baseline-run/REPORT.md)
· [theme-drift run report with heals](docs/showcase/theme-drift-run/REPORT.md).

Compiled workflows can also be emitted as Agent Skills (`SKILL.md`) and MCP
servers (`openadapt-flow emit-skill` / `emit-mcp`), so other agents can invoke
them.

## Status

v0 (124 tests, the drift matrix runs headlessly in CI). Solid for the
reference browser backend; native desktop and RDP backends are design seams,
not finished features. See [`DESIGN.md`](DESIGN.md) for module contracts and
[`docs/L1_INTEGRATION.md`](docs/L1_INTEGRATION.md) for feeding layered
clinical-data platforms (L1→L2 acquisition contracts).

## Development

```bash
git clone https://github.com/OpenAdaptAI/openadapt-flow && cd openadapt-flow
pip install -e '.[dev]' && playwright install chromium
pytest -q        # full suite incl. the E2E drift matrix
```

The demo GIF is generated from real run artifacts:
`python scripts/make_demo_gif.py --baseline docs/showcase/baseline-run
--drift docs/showcase/theme-drift-run --out docs/showcase/demo.gif`.

MIT license.

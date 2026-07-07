# openadapt-flow

Record a GUI workflow once. Replay it deterministically, locally, for free.
A model only touches the script to repair it.

Computer-use agents re-reason on every run: screenshot, think, click, repeat.
That's the right shape for a task nobody has automated before, and the wrong
one for the 500th referral this month. openadapt-flow makes the other bet. It
compiles a human demonstration into a plain, reviewable script with vision
anchors and per-step assertions. A healthy script replays in milliseconds with
zero model calls. When the UI shifts, a fallback ladder still finds the
target, and the fix gets written back into the script as a diff you can
review. The automation gets cheaper over time instead of billing you to
re-think the same eleven clicks.

## How it works

A recording captures screenshots and input events while a person does the
task once. The compiler turns that into a workflow bundle: for every click it
stores a template crop, the OCR'd label, and offsets to nearby landmark text,
then derives postconditions from what actually changed on screen after the
action. Typed values can be tagged as parameters, so one demo yields a
reusable, parameterized automation plus a generated `workflow.py` rendering a
human can code-review.

At replay time, each step walks a resolution ladder:

1. template match near the recorded location (milliseconds, no model)
2. template match across the whole screen
3. OCR match on the label
4. geometry from surrounding landmarks
5. optionally, a grounding model (local or API)

Resolving below the top rung triggers a heal: fresh crop, updated
coordinates, re-read label, saved as a reviewable change to the bundle.
Postconditions check that each step actually worked. If the screen stops
matching expectations entirely (a surprise dialog, a changed process), the
run halts with an illustrated report rather than guessing. Steps tagged
irreversible refuse to act on low-confidence resolutions at all.

The runtime is vision-only: PNG in, clicks and keys out, behind a small
`Backend` protocol. The reference backend drives a headless browser, which is
what lets the entire record→compile→replay→heal loop run in CI with no OS
permissions. Native desktop and RDP backends are planned adapters behind the
same protocol.

## What it looks like

One demonstration was recorded on the light UI (left). The compiled workflow
then ran unattended against a dark theme it had never seen (right): all 11
steps succeeded, every anchor re-resolved through the OCR or geometry rungs,
and all 8 anchors healed themselves back onto the fast path. No human
involved.

| Recorded UI → baseline replay (all `template` rung, 0 model calls) | Theme-drifted UI → replay + self-heal (8/8 anchors healed) |
| --- | --- |
| ![Baseline replay final screen](docs/showcase/baseline-run/steps/step_010_after.png) | ![Theme-drift replay final screen](docs/showcase/theme-drift-run/steps/step_010_after.png) |

Every replay generates an illustrated run report:
[baseline](docs/showcase/baseline-run/REPORT.md) ·
[theme drift with heals](docs/showcase/theme-drift-run/REPORT.md).

## Quickstart

```bash
pip install -e '.[dev]'
playwright install chromium

# 1. Record a demonstration (drives the bundled MockMed demo app):
openadapt-flow demo-record --out /tmp/rec

# 2. Compile it into a workflow bundle:
openadapt-flow compile /tmp/rec --out /tmp/bundle --name triage-demo

# 3. Replay it — deterministic, local, zero model calls:
openadapt-flow replay /tmp/bundle --run-dir /tmp/run
open /tmp/run/REPORT.md

# 4. Drift the UI (dark theme it has never seen) and watch it heal:
openadapt-flow replay /tmp/bundle --drift theme \
    --run-dir /tmp/run-drift --save-healed-to /tmp/healed
open /tmp/run-drift/REPORT.md
```

Steps 3–4 serve the bundled MockMed app automatically. To replay against your
own running app, pass `--url` (parameters default to the values recorded
during the demo; `--param` overrides them):

```bash
openadapt-flow replay /tmp/bundle --url <APP_URL> \
    --run-dir /tmp/run --param note="Booking 3 months"
```

`bench` replays a bundle N times and aggregates success rate, latency
percentiles, and cost: `openadapt-flow bench /tmp/bundle --n 10 --run-root
/tmp/bench`.

The test suite includes the full drift matrix (theme, moved buttons, renamed
buttons, surprise modal) end to end:

```bash
pytest -q
```

## Status

v0. The record→compile→replay→heal loop runs end to end against MockMed, the
bundled mock clinical app, under four kinds of deliberate UI drift. The suite
(124 tests) runs headlessly in GitHub Actions and uploads run reports as
build artifacts. Compiled workflows can also be emitted as Agent Skills
(`SKILL.md`) and MCP servers, so other agents can invoke them.

Solid for the reference browser backend; everything else should be treated as
a design seam, not a finished feature. See `DESIGN.md` for module contracts
and `docs/L1_INTEGRATION.md` for feeding layered clinical-data platforms
(L1→L2 acquisition contracts). MIT license.

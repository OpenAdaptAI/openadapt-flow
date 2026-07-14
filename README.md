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
pip install openadapt-flow

openadapt-flow demo-record --out rec                     # record a demonstration
openadapt-flow compile rec --out bundle --name my-task   # compile it
openadapt-flow lint bundle                               # report coverage gaps
openadapt-flow certify bundle --policy clinical-write    # refuse it if unsafe
openadapt-flow replay bundle                             # replay: local, $0
openadapt-flow replay bundle --drift theme               # drift the UI, watch it heal
```

On the first command that needs a browser, openadapt-flow downloads the
Chromium build Playwright needs (a one-time ~150MB fetch) — no separate
`playwright install chromium` step. Prefer the fast, isolated installs
`uvx openadapt-flow …` or `uv tool install openadapt-flow`. In air-gapped
or CI environments that pre-provision the browser, set
`OPENADAPT_FLOW_NO_AUTO_INSTALL=1` to disable the auto-download.

The last two commands serve the bundled MockMed demo app and write an
illustrated `REPORT.md` per run.

### Record your own app

`record --url` opens a headed browser on YOUR app and watches what you do —
real clicks, typing, key presses and scrolls — writing the same recording
format `compile` consumes. Perform the workflow, then press Ctrl-C (or close
the window) to finish:

```bash
openadapt-flow record --url https://your.app --out rec   # do the task, Ctrl-C
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --url https://your.app       # replay it
```

Pass `--url` to `replay` to run against your own app; recorded parameter values
are the defaults and `--param` overrides them.

**Secrets never get recorded.** A `input[type=password]` field (or any field
named with `--secret <name>`) is a secret parameter: its value is never written
to the recording, the events log, the compiled bundle, or the saved frames (its
region is redacted). At replay it is injected from the environment and a missing
one fails fast:

```bash
openadapt-flow record --url https://your.app --out rec --secret password
export OPENADAPT_FLOW_SECRET_PASSWORD='…'                 # supplied at replay
openadapt-flow replay bundle --url https://your.app
```

**Compiled is not the same as certified safe.** `lint` reports a bundle's
coverage gaps (clicks that act with no identity check, steps that assert
nothing, write steps left mis-classified) with a severity each; `certify`
enforces a policy and exits nonzero — refusing the bundle before it deploys —
when it fails. Risk is auto-classified at compile time (write-shaped clicks —
save/submit/create/delete/... — become `irreversible`, which arms the
low-confidence refusal), and two example policies ship: a permissive default
and a strict `clinical-write.yaml`. See [docs/LIMITS.md](docs/LIMITS.md) for
what the heuristic does and does not catch.

## How it works

Computer-use agents re-reason through your task with a large model on every
run. That's the right shape for a task nobody has automated before, and the
wrong one for the 500th referral this month. openadapt-flow compiles the
demonstration instead.

Each compiled step carries a template crop, an OCR label, geometry landmarks,
a structural locator, and postconditions derived from what the demo actually
changed on screen. At replay time a resolution ladder tries them in order: a
structural element match where the backend owns a DOM/UIA tree, then local
template match, global template match, OCR, landmark geometry, then
(optionally) a grounding model. Healthy scripts never leave the first rung.
Milliseconds, no model calls, no per-run cost.

When the UI drifts, a lower rung still finds the target and the fix lands in
the bundle as a diff you can review. When the screen stops matching
expectations entirely, the run halts with a report instead of guessing, and
steps tagged irreversible won't act on a low-confidence match at all.

The runtime is **vision-first**: it can always operate a pure pixel surface
(PNG in, clicks and keys out), but it is not limited to pixels. Where a backend
owns a structured layer — a browser DOM, a native UI Automation / accessibility
tree — the ladder's top rung re-finds the recorded target as an *element* and
acts on it deterministically; the visual rungs are the fallback floor for
pixel-only substrates (RDP, Citrix, canvas). On a desktop drift benchmark the structural
rung resolved 21/21 targets where visual replay alone managed 6/21
([`benchmark/structural_action/`](benchmark/structural_action/STRUCTURAL_ACTION.md)).
Structure never bypasses the identity gate — it makes identity stronger, an
exact element rather than a pixel guess.

It all sits behind a small four-method `Backend` protocol. The reference
backend is a headless browser (which is why the whole loop runs in CI with no
OS permissions); a `WindowsBackend` (UI Automation over the WindowsAgentArena
server) and a FreeRDP-driven RDP backend already exist and are exercised
against mocked servers in CI — adapters, not rewrites.

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

## From trace to program

A single demonstration under-specifies intent, so openadapt-flow does not stop
at replaying one. These capabilities layer onto the same $0, model-free runtime:

- **A workflow *program*, not just a line of steps.** Beyond the linear v0
  bundle, the IR (`openadapt_flow/ir.py`) expresses a parameterized program:
  states and guarded transitions, loops over worklists, subflows, typed
  parameters, predicates, and exception paths (`ProgramGraph` / `State` /
  `Transition` / `LoopSpec` / `Guard` / `Predicate` / `ParamSpec`). The flat
  trajectory is the degenerate case, so the migration is backward-compatible.
  Design: [`docs/design/WORKFLOW_PROGRAM_IR.md`](docs/design/WORKFLOW_PROGRAM_IR.md).
- **Multi-trace induction that refuses when it isn't sure.** `induce_program`
  aligns several demonstrations of the same task to recover the shared
  parameters, loops, and branches — deterministic and model-free at its core.
  When a branch condition or a value stays underdetermined it *quarantines* the
  program (`certified` is `False`) instead of guessing, and `disambiguate`
  surfaces the ambiguity as concrete multiple-choice questions rather than
  inventing an answer.
- **Effect verification against the system of record.** The screen can lie: an
  optimistic UI, a duplicate submit, a partial save all read as success. A step
  may declare typed `effects`, and when a run is given an `EffectVerifier` the
  replayer checks the *real* record — REST (`RestRecordVerifier`), FHIR
  (`FhirEffectVerifier`), or a document hash (`DocumentHashVerifier`) — before
  and after the action, halting on a refuted or unverifiable write, still with
  zero model calls. A fault-model study found the screen-only oracle silently
  mishandles 5 of 7 transactional fault classes; all five halt through the real
  replayer once effects are declared ([`benchmark/fault_model/`](benchmark/fault_model/FAULT_MODEL.md),
  [`docs/design/EFFECT_VERIFIER.md`](docs/design/EFFECT_VERIFIER.md)).
- **An API actuator tier.** Where the target app exposes a real API, driving its
  GUI to make the write is the wrong tool. A step carrying an `ApiBinding`, with
  an `ApiActuator` configured, performs the write by calling the API
  deterministically and confirms it with the same `EffectVerifier` — the `api`
  leaf of the capability ladder (API → DOM/UIA → geometry → OCR → template → VLM
  → human). It is an optimization whose safe fallback is always the GUI.
- **Policy: lint and certify.** `lint` reports a bundle's coverage gaps (unarmed
  clicks, vacuous postconditions, under-classified risk) with a severity each;
  `certify` enforces a policy and exits nonzero, refusing a bundle before it
  deploys. Runnable is not the same as certified safe.
- **Governed healing.** Every fix under drift lands in the bundle as a reviewable
  diff, and a step classified irreversible will not act on a low-confidence
  match — structure and the identity gate govern the heal, they are not bypassed
  by it.
- **Durable checkpoint / resume.** A run checkpoints verified progress
  (`openadapt_flow/runtime/durable/`) so a halt becomes a durable pause the
  operator can approve and resume from the last verified state — not a restart,
  and explicitly not "hand the rest to a free-form agent."
- **PHI-free identity.** The wrong-patient identity check can run against a
  salted-hash, shape-preserving `IdentityTemplate` instead of a plaintext
  name / DOB / MRN band, so a compiled bundle need carry no readable PHI while
  still enforcing identity (`openadapt_flow/runtime/identity_template.py`).

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

v0 for the reference browser backend: solid there, with a drift matrix and a
broad unit suite in CI (a consistency gate keeps this README honest — see
`scripts/check_consistency.py`). `DESIGN.md` has the module contracts; the
Phase-2 workflow-program IR is specified in
[`docs/design/WORKFLOW_PROGRAM_IR.md`](docs/design/WORKFLOW_PROGRAM_IR.md), and
[`docs/L1_INTEGRATION.md`](docs/L1_INTEGRATION.md) covers feeding layered
clinical-data platforms.

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
pip install -e '.[dev]'
playwright install chromium  # optional: else auto-downloads on first launch
pytest -q
```

The demo GIF is generated from real run artifacts by
`scripts/make_demo_gif.py`. MIT license.

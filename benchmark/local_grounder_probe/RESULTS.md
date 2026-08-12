# Local grounder probe — UNRUN

**Status: UNRUN. There are no results.** No measurement of
`OpenAICompatibleGrounder` against a real served model has completed with this
probe. Nothing on this page or in this directory may be cited as evidence.

## What this probe is

`scripts/probe_local_grounder.py` points the shipped, unmodified
`openadapt_flow.runtime.grounder.OpenAICompatibleGrounder` at a real
OpenAI-compatible endpoint serving a real local vision model, on the same
dense-EMR surface and DOM ground truth as `benchmark/grounding_eval`
(seed 1, ~51 rows, 6 deterministic targets, truth = the DOM centre of each
target row's Open button, tolerances 40/60 px). It records per-target
proposals, errors, hits, misses, and abstentions verbatim to
`results.json` in this directory — no retries, no downscaling, no massaging.

The wire protocol itself is proven in CI without any model by
`tests/test_grounder_openai_compatible_contract.py` (in-process loopback
server, real httpx transport). This probe adds only the real-model evidence,
which CI never needs: the probe is env-gated behind
`OPENADAPT_GROUNDER_BASE_URL` / `OPENADAPT_GROUNDER_MODEL` and exits UNRUN
when they are unset.

## Why it is unrun

The first run attempt (2026-08-12, Ollama serving `qwen3-vl:8b` on loopback)
was aborted: the machine kernel-panicked under concurrent load, with local
model inference a likely contributor.

## Requirements for the first real run (hard)

- A **dedicated solo run window**: nothing else on the machine — no builds,
  no test suites, no parallel agents, no other model servers, no VMs.
- **Never concurrently with builds** or CI jobs anywhere on the host.
- An **explicitly small model** (8B-class or below, quantized — e.g.
  `qwen3-vl:8b` / `qwen2.5vl:7b`). Never a large local model.

## Prior comparable numbers (different setups, recorded elsewhere)

- Bespoke remote-VLM grounder (served `mlx-community/Qwen3-VL-4B-Instruct-4bit`),
  same surface: **0/6 hits @ 40 px, ~472 px median error**
  (`benchmark/appliance_validation/REPORT.md`).
- OCR row-anchoring (local, $0), same surface: **4/6 @ 40 px, ~3.1 px median**
  (`benchmark/grounding_eval/REPORT.md`).

A single-shot VLM grounder is measured BROKEN on dense lists in those
baselines. This probe exists to measure, not to vindicate.

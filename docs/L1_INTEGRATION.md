# Feeding a layered clinical-data platform (L1 integration)

Layered clinical research platforms separate **acquisition** (L1: get artifacts out of source
systems) from **standardization** (L2: parse artifacts into a research-grade
common data model) and **federation** (L3). The L1→L2 seam is deliberately
thin: *an on-disk file under a resolved extraction directory, plus manifest
metadata* (`{file_number}_{date}_{doctype}` filename or a CSV sidecar).

openadapt-flow targets the L1 role: instead of hand-building a bespoke
acquisition harness per site (per-EMR navigation code, per-screen detection,
custom recovery logic), a site operator **records the workflow once** and the
compiler produces the deterministic, self-healing script that acquires the
artifacts on every subsequent run — locally, with no per-run model calls on
the happy path.

## What works today (v0)

- **Record → compile → replay → heal** end-to-end, vision-only (PNG in,
  clicks/keys out), validated against a mock EMR-like app including four
  drift scenarios (theme, layout move, label rename, unexpected modal) in CI.
- **Postconditions per step** derived from the recording (what actually
  changed on screen), so unattended runs verify progress instead of assuming
  it; semantic drift halts the run with an illustrated report rather than
  guessing.
- **Risk gate**: steps tagged irreversible refuse to act on low-confidence
  resolutions.
- **L1 artifact emission** (`openadapt_flow.emit.l1_artifact.emit_l1_artifact`):
  writes a workflow output file into an extraction directory under the
  canonical `{file_number}_{date}_{doctype}` name, appends a `manifest.csv`
  row, and drops a JSON provenance envelope (sha256, session, tool, version,
  captured_at) alongside — idempotent on identical re-emits, loud on content
  conflicts.

```python
from openadapt_flow.emit.l1_artifact import emit_l1_artifact

ref = emit_l1_artifact(
    downloaded_pdf,                # produced by a replayed workflow step
    extraction_dir,                # the root the L2 layer watches
    file_number="P1",
    date="2026-07-06",
    doctype="referral",
    session_id=run_report.workflow_name,
)
```

## Honest gaps (the roadmap, in order)

1. **Native (macOS/Windows-local) backend.** The `Backend` protocol
   (screenshot / click / type / press) is designed for it; a native
   macOS backend is a remaining adapter. The compiled bundles and the
   runtime do not change. *(The RDP backend below has landed as a spike;
   the WAA/Windows backend already covers native Windows over HTTP.)*

   **RDP backend — spike landed** (`openadapt_flow/backends/rdp_backend.py`,
   `docs/backends/RDP.md`). The load-bearing L1/Retinology case reaches a
   legacy ophthalmology EMR over **RDP**, read **pixel-only** (no
   accessibility tree) — exactly the vision-only substrate the runtime was
   built for, so RDP is *an adapter, not a rewrite*. `FreeRDPBackend`
   implements the `Backend` protocol on top of a minimal, swappable
   `RDPTransport` (`connect` / `disconnect` / `framebuffer` / `pointer` /
   `key` / `wheel`); a real `AardwolfTransport` (pure-Python async RDP client,
   bridged to sync, behind the optional `rdp` extra) and a `FakeRDPTransport`
   sit under it. The unmodified Recorder → compiler → Replayer stack drives it
   end-to-end in the mock-tested conformance test — **zero compiler/replayer
   changes**. Status: adapter shape proven (mock-tested in CI + a gated live
   smoke test); the real transport installs, imports lazily and constructs
   valid connections, but **validation against a real clinic EMR over RDP is
   pending a screen recording** — OCR/grounding quality under RDP compression
   is the open question, and the VLM fallback is expected to matter there.
   RDP is pixel-only, so the backend deliberately does **not** claim the
   optional `IdentityBackend` / `StructuralBackend` capabilities; identity
   falls back to the OCR name+DOB-primary tier (docs/LIMITS.md).
2. **Tier-0 per-workflow detector.** The current ladder is template → OCR →
   geometry → optional VLM grounder. Distilling a per-workflow YOLO-class
   detector from recordings (clicks auto-label crops) is planned as the
   sub-10ms rung and removes manual labeling.
3. **Local grounder serving.** The `Grounder` protocol exists with a null and
   an API-based implementation; an MLX-served small grounding model (e.g.
   Holo3.1-4B class) is the intended local default so the entire ladder runs
   offline.
4. **Read-back extraction.** v0 verifies screens and clicks reliably; pulling
   *data* off screens (beyond what workflows download as files) will prefer
   clipboard/select-all patterns with OCR as verification, not primary.

## Trying it

```bash
pip install -e '.[dev]' && playwright install chromium
pytest -q                        # full suite incl. the drift/heal E2E matrix
openadapt-flow demo-record --out /tmp/rec
openadapt-flow compile /tmp/rec --out /tmp/bundle --name demo
openadapt-flow bench /tmp/bundle --n 3 --run-root /tmp/bench
```

Then read `/tmp/bench/BENCH.md` and the per-run `REPORT.md` — the same
artifacts a site operator would review after an unattended run.

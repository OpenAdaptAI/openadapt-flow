# Citrix Workspace-window pixel backend

Part 2 of the no-DOM/Citrix validation (`~/oa/src/.private/rdp_citrix_validation_2026_07_20.md`).
A backend that drives a **Citrix Workspace/Viewer session WINDOW as a no-DOM
pixel surface**: window-scoped screen capture + OS-level input injection into the
Workspace window, **pixel-only** (no structural/a11y), conforming to the base
`Backend` contract so the resolver ladder + effect verification run over it
UNCHANGED.

## What this is (and what already existed)

The window-scoped-capture + OS-input mechanism is
`openadapt_flow.backends.remote_display.RemoteDisplayBackend` (merged, PR #106):
`CGWindowListCreateImage` / `PrintWindow` capture by window id + `CGEvent` /
`SendInput` injection, per-monitor-v2 DPI, and fail-loud safety gates
(frame-freshness lease, occlusion guard, DPI-consistency, input-trust). It is
target-window-agnostic. This deliverable adds the thin **Citrix preset** over it:

- `openadapt_flow/backends/citrix_workspace.py`
  - `CITRIX_WINDOW_OWNERS` — per-platform Workspace/Viewer window owner names.
  - `default_citrix_owner()` — pick the host's Citrix owner.
  - `CitrixWorkspaceBackend` — a `RemoteDisplayBackend` that defaults its target
    owner to the host's Citrix Workspace window and carries the ICA scope note.
    Pixel-only by construction (base `Backend` only; NOT `StructuralBackend` /
    `IdentityBackend` / `StructuralActionBackend`), so the structural rung is
    unavailable — the ICA floor.
- Factory: `--backend citrix` now builds `CitrixWorkspaceBackend` with the
  default owner (no need to know the per-platform string; `--rdp-window`
  overrides it, `--rdp-window-title` disambiguates multiple sessions).

## Status — DONE vs PENDING (honest)

**DONE (validated this pass):**
- The backend records → compiles → replays through the vision-only ladder and
  **safe-halts under drift**, exercising the REAL `RemoteDisplayBackend` code
  paths (window resolution, per-frame capture + DPI/scale, pixel→screen-point
  map, frame-freshness lease, occlusion guard, input-trust gate) over a genuine
  no-DOM surface. Evidence: `results.json`
  (`schema_version: openadapt.citrix-workspace-qualification.v1`, `accepted:true`):
  - healthy: record→compile→replay succeeds, **0 model calls**, **visual rungs
    only** (template ×3, structural never used), write **independently
    confirmed** by the document oracle;
  - severe (illegible) drift: **HALTS** with no write and no model call — no
    blind coordinate replay.
- Unit contract: `tests/test_citrix_workspace_backend.py` (owner presets,
  pixel-only protocol surface, factory wiring).

**PENDING (needs the CVAD/DaaS trial lab + a GUI host):**
- Validation against a **REAL Citrix Workspace window** on a **live ICA/HDX
  session**. The ICA-specific delta the canvas stand-in cannot cover: HDX codec
  artifacts, ICA compression, and the real Workspace-client input path.
- Confirming the exact **owner/title** string on the target Workspace build and
  a **session-lock readiness marker**.

## How it was validated now: the no-DOM canvas STAND-IN

`run_citrix_workspace_qualification.py` drives the **unmodified**
`CitrixWorkspaceBackend` over the Part-1 no-DOM canvas fixture
(`benchmark/canvas_ladder/fixture`) by swapping only the backend's `WindowClient`
seam for a `CanvasWindowClient` that captures the noVNC `<canvas>` and injects
into it (Playwright). This is a **real** proof of the backend (all its logic
runs), over the **no-DOM HTML5-canvas class** — **NOT** Citrix ICA/HDX.

```bash
# Reuses the Part-1 fixture container (benchmark/canvas_ladder):
docker build -t oaflow-canvas-fixture:latest benchmark/canvas_ladder/fixture
docker run -d --name oaflow-canvas-ladder --shm-size=1g -p 6080:6080 \
    oaflow-canvas-fixture:latest
sleep 15
python3 benchmark/citrix_workspace/run_citrix_workspace_qualification.py \
    --container oaflow-canvas-ladder \
    --output benchmark/citrix_workspace/results.json \
    --candidate-commit "$(git rev-parse HEAD)"
docker rm -f oaflow-canvas-ladder
```

Env-gated e2e: `tests/e2e/test_citrix_workspace_standin_e2e.py`
(`OAFLOW_CITRIX_STANDIN_E2E=1`). Nightly draft CI:
`.github/workflows/citrix-workspace-standin.yml`.

## Point at the real CVAD lab (when it is up)

A separate agent is standing up a CVAD 30-day-trial Azure lab
(`~/oa/src/.private/rdp_citrix_validation_2026_07_20.md` §7). Once a published
app is reachable via the Citrix Workspace app on a **GUI host we control**:

1. **Find the exact window owner/title.** On the host, enumerate on-screen
   windows and confirm the Workspace session window's owner (macOS: usually
   `Citrix Viewer`; Windows: `Citrix Workspace` / `wfica32` / `CDViewer`). Set
   `--rdp-window <owner>` if it differs from the preset, `--rdp-window-title` to
   disambiguate.
2. **Grant input/capture trust** to the driver process (macOS: Screen Recording
   + Accessibility; Windows: run the driver at the target window's integrity
   level). The backend **fails loud** if trust is missing — a dropped click can
   never look like success.
3. **Set a session readiness marker** (`readiness_text` / `--rdp-readiness-text`)
   — a stable in-app word — so a lock/login/disconnect frame is refused.
4. **Swap the client:** construct `CitrixWorkspaceBackend()` with **no**
   `client` (the host's native Mac/Win `WindowClient` is used automatically), or
   `--backend citrix` from the CLI. Everything else — the ladder, identity,
   effect, drift-halt — is identical to the stand-in harness.
5. **Run the same contract** (record→compile→replay pixel-only + independent
   oracle + drift halt) and commit `benchmark/citrix/…` evidence with an
   **ICA-specific manifest** (HDX codec / adaptive-display settings recorded) and
   a PHI-free sanitized report (reuse `scripts/sanitize_rdp_qualification_report.py`).

## Honest scope / label

This proves the **Citrix backend contract + ladder + effect + safe-halt** over a
**no-DOM HTML5-canvas STAND-IN** (the class Citrix Workspace-web presents). It is
**NOT Citrix ICA/HDX**. We do not fake Citrix evidence; the real-ICA gate is a
trial-clock + GUI-host constraint, not a code gap upstream of the window.

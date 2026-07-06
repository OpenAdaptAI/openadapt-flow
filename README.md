# openadapt-flow

**Record a workflow once. Run it deterministically, locally, forever — a model
only touches it to heal it.**

openadapt-flow is a demonstration compiler for GUI workflows:

1. **Record** — capture a human demonstration (screenshots + input events).
2. **Compile** — turn the recording into an editable, vision-anchored script:
   every step carries redundant evidence (template crop, OCR text, geometry
   landmarks), the action, and postcondition assertions derived from what
   actually changed on screen.
3. **Replay** — a resolution ladder finds each target: local template match →
   global template match → OCR text → relative geometry → (optional) local
   grounding model. Healthy scripts never leave the first rung: milliseconds,
   zero model calls, zero marginal cost.
4. **Heal** — when the UI drifts, a lower rung resolves the target, and the
   fix is written back to the script as a reviewable diff. The automation gets
   cheaper and more robust over time instead of re-reasoning every run.

The runtime is **vision-only** (PNG in, click/keys out) behind a small
`Backend` protocol, so the same compiled workflow logic drives a headless
browser (reference/test backend), a native desktop, or an RDP session.

## Quickstart

```bash
pip install -e '.[dev]'
playwright install chromium

# End-to-end demo against the bundled MockMed app:
openadapt-flow demo-record --out /tmp/rec                          # scripted demonstration
openadapt-flow compile /tmp/rec --out /tmp/bundle --name triage-demo
openadapt-flow bench /tmp/bundle --n 1 --run-root /tmp/bench       # serves MockMed and replays
open /tmp/bench/BENCH.md
```

To replay a bundle against your own running app (parameters default to the
values recorded during the demo; `--param` overrides them):

```bash
openadapt-flow replay /tmp/bundle --url <APP_URL> \
    --run-dir /tmp/run --param note="Booking 3 months"
open /tmp/run/REPORT.md
```

Run the full test suite (includes end-to-end record→compile→replay→heal under
deliberate UI drift):

```bash
pytest -q
```

## Status

v0: reference implementation validated end-to-end against MockMed (a bundled
mock EMR-like app) including drift/healing scenarios, via the bundled test
suite (`pytest -q`; a GitHub Actions workflow in `.github/workflows/ci.yml`
runs the same suite and uploads run reports as artifacts). Native macOS and
RDP backends are planned adapters behind the same `Backend` protocol.

See `DESIGN.md` for architecture and module contracts. MIT license.

# Real-RDP vision-ladder qualification

This directory holds a **real RDP** end-to-end qualification of the vision-only
resolution ladder — the missing live proof for the pixel path on a no-DOM
remote-display surface. It complements, and must not be confused with, the two
adjacent proofs:

| Proof | What it exercises | Substrate |
| --- | --- | --- |
| `benchmark/rdp` (PR #142) | RDP **transport + input** (aardwolf) | real Windows RDP (Parallels) |
| `benchmark/windows_uia`, `benchmark/macos_native` | **structural** rungs (UIA / AX) | native a11y |
| **`benchmark/rdp_ladder` (here)** | **vision-only resolution ladder + contract** (`template → template_global → ocr → geometry`), effect verification, halt-under-drift | real RDP **pixels**, no a11y |

## What it proves

`run_rdp_ladder_qualification.py` drives the **unmodified** `Recorder` →
`compile_recording` → `Replayer` over a genuine RDP round-trip, with **no
structural backend**, and asserts the validation contract:

- **healthy**: record → compile → replay a patient-note write **succeeds**, with
  **zero model calls**, resolution through the **visual rungs only** (the
  structural rung is never used), and the write **independently confirmed** by a
  document oracle (the note the kiosk persisted equals the intended value);
- **halt-under-drift**: with DPI + theme-inversion + JPEG-compression drift
  injected onto the real session, the ladder **HALTS** — no model call, no
  silent write. This is the substrate complement to the ambiguity/look-alike
  hardening in `~/.private/vision_hardening_2026_07_20.md` (#165/#166).

Evidence is written to `results.json`
(`schema_version: openadapt.rdp-ladder-qualification.v1`) with an `accepted`
gate.

## Why FreeRDP (not the product's aardwolf client) for the Linux surface

The product's RDP transport, the pure-Python `aardwolf` client, **cannot
interoperate with Linux RDP servers** (reproduced):

- **xrdp**: aardwolf blocks forever on an MCS `tokenInhibitConfirm` PDU xrdp
  never sends (aardwolf's own code: "TODO: implement properly").
- **FreeRDP3 shadow**: rejects aardwolf's MCS Erect-Domain Request — "invalid
  TPKT header length 12, 1 bytes too long" (aardwolf non-conformance).
- **FreeRDP2 shadow**: server-side container TCP-listener bug; never binds.

aardwolf works only against real **Windows** RDP (lenient parser + full MCS) —
which is why `benchmark/rdp` uses Parallels Win11. A **CI-viable Linux** RDP
surface for the *ladder* therefore uses a conformant client/server pair. The
`RDPTransport` protocol is explicitly swappable, so this is in-design: the fixture
serves a Tk kiosk over a FreeRDP3 **server** and renders it back with a FreeRDP3
**client**, and the harness observes/injects on the client display — so both the
pixels read and the input injected cross a real RDP exchange.

## Determinism note

The FreeRDP client runs with the RemoteFX / NSCodec / GFX-pipeline lossy codecs
disabled (`-gfx -rfx -nsc`), so it decodes **raw bitmaps** and consecutive frames
are byte-identical. Lossy RDP codec jitter otherwise perturbs template scores and
trips the identity band on write steps (a real, but fixture-induced, over-halt).
DPI/theme/compression realism is injected in software on top of this clean
baseline (the harness `_DriftBackend`) — a deliberate, labeled degradation.

## Honest scope

Real RDP-transported pixels + input, raw-bitmap RDP frames, the real
resolver ladder and the $0 / identity / effect gates. **NOT** Citrix ICA/HDX,
**NOT** the aardwolf transport (that is `benchmark/rdp`), and the drift is
**simulated-on-a-real-session** (not WAN capture). See
`~/.private/rdp_citrix_validation_2026_07_20.md`.

## Runbook

```bash
# 1. Build the fixture image (multi-arch: amd64 CI + arm64 Apple Silicon)
docker build -t oaflow-rdp-fixture:latest benchmark/rdp_ladder/fixture

# 2. Start the RDP round-trip (self-contained)
docker run -d --name oaflow-rdp-ladder --shm-size=1g oaflow-rdp-fixture:latest
sleep 20   # let the kiosk + shadow server + client come up

# 3. Run the qualification (needs the flow stack with cv2 + rapidocr on PATH)
python3 benchmark/rdp_ladder/run_rdp_ladder_qualification.py \
    --container oaflow-rdp-ladder \
    --output benchmark/rdp_ladder/results.json \
    --candidate-commit "$(git rev-parse HEAD)"

# 4. Tear down
docker rm -f oaflow-rdp-ladder
```

Exit code `0` iff `accepted` is true. The run takes a few minutes (the transport
screenshots/injects over `docker exec`, one op at a time).

The env-gated pytest wrapper `tests/e2e/test_docker_rdp_vision_ladder_e2e.py`
(`OAFLOW_DOCKER_RDP_E2E=1`) builds the fixture, runs the qualification, and
asserts `accepted`. A nightly CI job wiring is drafted in
`.github/workflows/docker-rdp-vision-ladder.yml`.

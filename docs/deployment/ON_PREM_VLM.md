# On-prem VLM inference service

A single GPU appliance serves the identity-veto, grounding, and state-verification
VLM tiers to a fleet of **GPU-less** automation runners over the LAN. The runtime
stays GPU-free and **patient data never leaves the building**.

> Status: appliance + fail-safe clients. Runtime wiring into the identity ladder
> and the resolution ladder's grounder slot lands separately (after the
> identity-ladder PR #33 merges). This document is the contract those integrations
> target.

## Topology

```
   GPU-less runners (Windows VM / Citrix / desktop)          On-prem GPU box
  ┌───────────────────────────────────────────┐          ┌────────────────────┐
  │ openadapt-flow replayer                    │          │ VLM service        │
  │  ├─ RemoteGrounder ───────────┐            │  HTTPS   │  FastAPI + batcher │
  │  ├─ RemoteIdentityVLM ────────┼──── LAN ───┼────────► │  ├─ /v1/identity   │
  │  └─ RemoteStateVerifier ──────┘  bearer    │  (LAN)   │  ├─ /v1/ground     │
  │                                   token    │          │  ├─ /v1/verify_st. │
  │  NO GPU. Fail-safe: appliance down => HALT │          │  └─ one open VLM   │
  └───────────────────────────────────────────┘          │     (24GB GPU)     │
        (N runners)                                        └────────────────────┘
```

One model, loaded once, serves N runners. Nothing leaves the LAN; there is **no
API dependency** and no PHI egress. (A cloud API endpoint is an **opt-in** for
non-regulated customers only — never the default for a clinical deployment.)

## Endpoints (contract)

All request/response bodies are defined in
`openadapt_flow/services/vlm_service/schemas.py`. Images cross the wire as
base64-encoded PNG. All `/v1/*` endpoints require the bearer token.

| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/v1/identity/compare` | `{crop_a, crop_b}` (b64 png) | `{verdict: "same"\|"different"\|"uncertain", confidence?, latency_ms}` |
| POST | `/v1/ground` | `{screenshot, target_description, ocr_text?, viewport?}` | `{point: [x,y]\|null, confidence, latency_ms}` |
| POST | `/v1/verify_state` | `{screenshot, expected_state}` | `{holds: "yes"\|"no"\|"uncertain", latency_ms}` |
| GET | `/health` | — | `{status: "ok"}` (liveness; no auth) |
| GET | `/ready` | — | `{ready, backend, model}` (model loaded?; no auth) |

**The server never authorizes an action.** `identity/compare` reuses the
validated veto-only same/different prompt + parser from the identity probe
(PR #28, `validation/vlm_identity_probe.py`) — Qwen3-VL-4B, 0% false-accept on
the collapse surface: any non-confident/unparseable answer is reported as
`different` (a veto). `ground` only *proposes* a point (the deterministic
identity band still disposes before any click). `verify_state` reports the
drift-oracle postcondition (semantic "did the intended state happen?",
robust to font/scale/theme drift) used when the deterministic postcondition
false-fails under render drift.

## Fail-safe behaviour (mandatory)

The clients (`openadapt_flow/runtime/remote_vlm.py`) return the SAFE outcome on
**any** failure — unreachable, timeout, auth error, 5xx, malformed body — so a
GPU-less runner degrades to a **safe-halt**, never to a wrong action, when the
appliance is down:

| Client | Failure outcome | Effect |
|---|---|---|
| `RemoteIdentityVLM` | `IdentityVerdict.ABSTAIN` | tier abstains → halt |
| `RemoteGrounder` | `None` (no proposal) | resolution ladder halts |
| `RemoteStateVerifier` | `"uncertain"` | postcondition unproven → halt |

Only a confident `same` avoids the identity veto, and even then it is a
*fail-to-veto*, not a grant: the deterministic identity authority still governs
(invariant E — a model never sits between "resolve target" and "verify identity"
as the authority; it can only veto). This is proven in
`tests/test_remote_vlm.py` (all six failure modes → safe outcome).

## Batching

Many runners hit one GPU. Each request is enqueued and a single async worker
drains a short **window** of queued requests and dispatches them together,
bounded by a max batch size (`openadapt_flow/services/vlm_service/batching.py`).
With the vLLM backend the co-submitted calls land in vLLM's own
continuous-batching scheduler, so throughput scales with GPU occupancy.

| Tunable | Env var | Default | Notes |
|---|---|---|---|
| Batch window | `VLM_BATCH_WINDOW_MS` | 15 ms | ≪ the ~0.8 s inference budget → invisible latency |
| Max batch size | `VLM_MAX_BATCH_SIZE` | 8 | cap on concurrent in-flight model calls |

## Auth

A shared bearer token gates every `/v1/*` endpoint (`VLM_SERVICE_TOKEN`).
On-prem, but still authenticated — an unauthenticated request is rejected with
`401`. `/health` and `/ready` are unauthenticated for load-balancer probes. Run
the service behind TLS on the LAN.

## Model + hardware sizing

Per `.private/vlm_identity_verification_2026_07_12.md` and
`.private/oss_model_assessment_2026_07_10.md`:

- **Production model:** GUI-Owl-1.5-8B-Instruct (MIT, Qwen3-VL base) — one model
  covers grounding (availability) and identity comparison (safety-veto). Falls
  back to Qwen3-VL-4B-Instruct (Apache-2.0) for a smaller footprint.
- **Serving:** vLLM / SGLang, OpenAI-compatible (`/v1/chat/completions`).
- **Hardware:** a **single 24 GB GPU-class card** (4090-class) hosts the 8B model
  (~20 GB) or the 4B model with headroom. Pin the inference stack and
  regression-test grounding coordinates per version (Qwen3-VL uses normalized
  0–1000 coords).
- **Dev backend:** MLX (`mlx-community/Qwen3-VL-4B-Instruct-4bit`), ~5–9 GB at
  4-bit, runs on an Apple-Silicon laptop so the fleet can be tested with no GPU
  box (measured ~0.77 s/call for a 2–4B model via MLX on an M2 Max).

## Latency budget (escalation path)

The VLM fires only as a **targeted escalation** (identity veto on a
glyph-confusable discriminator; grounding when the deterministic ladder can't
resolve; state check when the deterministic postcondition false-fails). Budget:
**LAN round-trip (sub-ms to low-ms) + ~0.8 s inference**. Because escalation
fires on a tiny fraction of steps, even multi-hundred-ms latency is invisible to
the overall replay.

## Running the service

Production Linux GPU box (after `vllm serve <model> --port 8000`):

```bash
pip install -e '.[service]'
VLM_BACKEND=vllm \
VLM_MODEL=mPLUG/GUI-Owl-1.5-8B-Instruct \
VLM_VLLM_URL=http://localhost:8000/v1 \
VLM_SERVICE_TOKEN="$(cat /etc/openadapt/vlm_token)" \
  openadapt-flow-vlm-service --host 0.0.0.0 --port 8077
```

Apple-Silicon dev box (local MLX model, no GPU box):

```bash
pip install -e '.[service-mlx]'
VLM_BACKEND=mlx VLM_SERVICE_TOKEN=devtoken \
  openadapt-flow-vlm-service --port 8077
```

Runner side (GPU-less):

```python
from openadapt_flow.runtime.remote_vlm import (
    RemoteVLMClient, RemoteGrounder, RemoteIdentityVLM, RemoteStateVerifier,
)

client = RemoteVLMClient("https://gpu-box.lan:8077", token=TOKEN, timeout=2.0)
grounder = RemoteGrounder(client)          # Grounder protocol, drop-in
identity = RemoteIdentityVLM(client)       # verify / mismatch / abstain
state    = RemoteStateVerifier(client)     # yes / no / uncertain
```

## Integration

Wired into the `replay` CLI. An appliance is **opt-in** — set three env vars on
the runner and the grounding rung and identity veto tier come online; leave them
unset (the default) and the run stays fully local and model-free.

```bash
export OPENADAPT_FLOW_VLM_URL="https://gpu-box.lan:8077"   # unset => dormant
export OPENADAPT_FLOW_VLM_TOKEN="$(cat /etc/openadapt/vlm_token)"
export OPENADAPT_FLOW_VLM_TIMEOUT=2.0                       # optional, seconds
openadapt-flow replay bundle
```

`appliance_from_env()` (`runtime/remote_vlm.py`) reads these and returns a
`RemoteAppliance` (or `None`); the CLI passes its handles into
`Replayer(grounder=..., identity_vlm=...)`.

- **Grounder slot:** `RemoteGrounder` satisfies the `Grounder` protocol
  (`runtime/grounder.py`) and drops into the resolution ladder's grounder slot
  in place of `NullGrounder` — no other change. It only ever *proposes* a point;
  the deterministic identity band still disposes before any click, and an outage
  yields no proposal (availability down, not safety).
- **Identity ladder VLM tier:** `RemoteIdentityVLM.same_or_different(...)` adapts
  the service's verdict onto the tier's veto-only contract —
  `VERIFY → "same"` (fail-to-veto), and `MISMATCH`/`ABSTAIN` (the latter the
  default on any uncertainty or appliance outage) → `"different"` (halt). The
  tier can only veto; a down appliance means more halts, never a wrong click.
- **Drift-oracle postcondition** (`RemoteStateVerifier`): *not yet wired.* It
  needs a postcondition-failure hook in the replayer (call the verifier only
  when a deterministic postcondition false-fails under render drift; `"uncertain"`
  keeps it a halt). Tracked as a follow-up.

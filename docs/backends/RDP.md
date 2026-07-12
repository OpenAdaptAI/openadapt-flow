# RDP backend (FreeRDP / pixel-only remote desktop)

The L1/Retinology wedge reaches a **legacy ophthalmology EMR over RDP**, read
**pixel-only** — no accessibility tree, no DOM, no structured layer at all.
That is precisely the substrate the vision-only runtime was built for: PNG
frames in, pixel-coordinate clicks and keys out. So RDP is **an adapter, not a
rewrite** — `openadapt_flow/backends/rdp_backend.py` implements the `Backend`
protocol on top of a small, swappable RDP transport, and the compiled bundles,
compiler and replayer do not change.

## The transport abstraction

Two layers keep the adapter CI-testable without a live RDP server and keep the
RDP library replaceable:

```
Backend protocol  ──implemented by──▶  FreeRDPBackend  ──drives──▶  RDPTransport
(screenshot/click/type/press/scroll)                                     ▲
                                                          ┌──────────────┴───────────────┐
                                                   AardwolfTransport            FakeRDPTransport
                                                   (real, `rdp` extra)          (scripted, tests)
```

**`RDPTransport`** is deliberately tiny and honest — a single framebuffer read
plus three input primitives:

| method | contract |
| --- | --- |
| `connect()` | open the session, block until a frame is available |
| `disconnect()` | tear down (idempotent, never raises) |
| `framebuffer()` | return `(frame, width, height)` — `frame` is a PIL image or raw RGB/RGBA bytes; the dims define the coordinate space |
| `pointer(x, y, button, down)` | pointer button transition at framebuffer pixel `(x, y)` |
| `key(keysym_or_char, down)` | a character to type (`'a'`, `'1'`) or a normalized key name (`'enter'`, `'ctrl'`, `'up'`) |
| `wheel(dx, dy)` | wheel gesture by `(dx, dy)` framebuffer pixels |

**`FreeRDPBackend`** maps the flow `Backend` protocol onto it: `screenshot`
PNG-encodes the framebuffer; `viewport` reports its size; `click` sends pointer
down/up (double = the sequence twice); `type_text` sends per-character key
down/up; `press` decomposes a key/chord into ordered key down-then-reverse-up
events; `scroll` sends a wheel gesture.

### Coordinate space

Everything is in **framebuffer pixels** — the same pixels the resolver emits
and the same pixels `screenshot()` encodes, because both come from
`RDPTransport.framebuffer()`. A transport that downsamples the remote desktop
MUST report the downsampled `(width, height)`, so screenshot pixels and click
pixels stay in one space; no scaling happens in the backend. `AardwolfTransport`
runs 1:1 (PIL video-out at the requested width/height).

### Identity: pixel-only, honestly

RDP is a pure-pixel substrate, so `FreeRDPBackend` deliberately does **not**
implement the optional `IdentityBackend.structured_text_at` or the
`StructuralBackend` url/title/page-count observations — there is no structured
layer to read, and claiming a capability it cannot honor would be a lie.
Identity falls back to the OCR name+DOB-primary tier exactly as documented for
pixel-only substrates (`openadapt_flow/backend.py`, `docs/LIMITS.md`). This
mirrors how `WindowsBackend` omits `StructuralBackend`.

## The `rdp` extra

The real transport uses [`aardwolf`](https://pypi.org/project/aardwolf/), a
pure-Python **async** RDP client. It is lazily imported and gated behind an
optional extra so core installs stay lean and importing `rdp_backend` never
imports aardwolf:

```bash
pip install 'openadapt-flow[rdp]'
```

`AardwolfTransport` owns a private asyncio event loop on a daemon thread and
marshals every operation onto it, presenting the synchronous `RDPTransport`
API the backend expects. Construct it from plain credentials:

```python
from openadapt_flow.backends.rdp_backend import AardwolfTransport, FreeRDPBackend

transport = AardwolfTransport.from_credentials(
    "10.0.0.5", "clinicuser", "password",
    domain=None,          # omit for a local account; set for a Windows domain
    port=3389,
    width=1280, height=800,
)
backend = FreeRDPBackend(transport)   # connects on construction
png = backend.screenshot()
backend.click(640, 400)
backend.close()
```

(Or pass a full aardwolf URL:
`rdp+ntlm-password://DOMAIN\\user:password@host:port`.)

## Running the tests

Mock tests (no RDP server, run in CI):

```bash
pytest tests/test_rdp_backend.py -q
```

They cover screenshot→PNG (PIL and raw-bytes framebuffers), viewport, click /
double-click, per-character `type_text`, `press` key/chord mapping, `scroll`,
protocol conformance (`isinstance(backend, Backend)`), the pixel-only
capability omissions, and a full **record → compile → replay** run over the
`FreeRDPBackend` against a stateful fake desktop (proving zero compiler/replayer
changes).

## Live smoke test (gated; skipped in CI)

A single gated test connects to a real RDP target via `AardwolfTransport`,
grabs **one** framebuffer, asserts it is a non-trivial image, and disconnects.
It is skipped unless all three env vars are set and needs the `rdp` extra:

```bash
pip install 'openadapt-flow[rdp]'
export OPENADAPT_FLOW_RDP_TARGET=host_or_ip[:port]
export OPENADAPT_FLOW_RDP_USER=username
export OPENADAPT_FLOW_RDP_PASS=password
# optional: OPENADAPT_FLOW_RDP_DOMAIN, OPENADAPT_FLOW_RDP_WIDTH, OPENADAPT_FLOW_RDP_HEIGHT
pytest tests/test_rdp_backend.py -k live_smoke -s
```

### Against the local Parallels VM

The dev Mac has a Parallels "Windows 11" VM. To point the live test at it, the
guest must be running an RDP host (this is a mutating guest change — snapshot
first with `prlctl snapshot "Windows 11" -n rdp-smoke`, and never delete the
user's snapshots):

1. Resume the VM: `prlctl resume "Windows 11"`.
2. In the guest (Windows 11 **Pro** — Home cannot host RDP): Settings → System
   → Remote Desktop → **On**; ensure the login account has a **non-blank
   password** (RDP rejects blank-password accounts by default).
3. Find the guest IP: `prlctl exec "Windows 11" ipconfig` (the shared-network
   IPv4, not `169.254.x`).
4. `export OPENADAPT_FLOW_RDP_TARGET=<guest-ip>` plus `_USER` / `_PASS` and run
   the command above.

## Status — proven vs pending

**Proven:**
- The adapter shape: `Backend` protocol satisfied on top of `RDPTransport`,
  mock-tested including a full record→compile→replay conformance run with **zero
  compiler/replayer changes**.
- The real dependency is real: `aardwolf` (the `rdp` extra) pip-installs and
  builds a wheel; `AardwolfTransport` imports lazily, constructs valid
  connection URLs, and aardwolf's own factory parses them.

**Pending (out of scope for this spike):**
- A live frame decode over the wire against a real RDP host has not been run in
  this environment (the local Parallels VM has no RDP host configured and no
  credentials on hand; enabling it is a disruptive guest change). The gated
  live test above is the harness for it.
- **Validation against the real clinic EMR** is pending a screen recording. The
  open question is OCR / grounding quality under **RDP compression artifacts**
  — lossy tiles, subsampled color, scaled fonts — where the template/OCR/geometry
  ladder may degrade and the **VLM grounding fallback is expected to matter
  most**. Until we have a recording of the actual EMR over RDP, that quality
  claim stays honest as *unmeasured*.

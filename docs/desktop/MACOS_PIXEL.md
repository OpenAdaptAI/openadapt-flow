# macOS pixel path — `--backend macos` + the pixel-compare identity tier

This wires the **local macOS remote-display window backend**
(`RemoteDisplayBackend`, `backends/remote_display.py`) all the way to the CLI and
arms the **pixel-compare identity tier** for it. It is the productized form of
the Citrix analog proven in [`CITRIX_PIXEL.md`](CITRIX_PIXEL.md): the same
host-side pixels of a remote guest, host-OS input injection, and **no guest
accessibility tree through the window**.

Every claim is tagged **PROVEN-IN-CODE** (real code + green unit tests) or
**GAP** (what a real Citrix/Accuro session adds that this does NOT cover). No
result is fabricated.

---

## 1. Driving it from the CLI

```
# Replay a compiled bundle against a Citrix Workspace client window on macOS:
openadapt-flow replay ./bundle --backend macos --window "Citrix Workspace"

# The local Parallels VM window (the on-infra-we-control analog):
openadapt-flow replay ./bundle --backend macos --window Parallels

# Record a demonstration on the same substrate (substrate-agnostic capture):
openadapt-flow record --backend macos --out ./rec --task "open chart, add note"
```

- `--backend macos` selects the macOS Quartz backend. It is an **alias**: the
  factory (`backends/factory.py::_normalize_kind`) folds `macos`/`mac` to the
  `rdp` family, and — because `--window` sets `rdp_window` and no `--rdp-host`
  is given — `build_backend` constructs `RemoteDisplayBackend` against the named
  client window (never a network RDP session). `--backend rdp` with a configured
  `rdp_window` is byte-for-byte equivalent; `macos` just names the substrate.
- `--window OWNER` is the case-insensitive **owner-app substring** of the
  on-screen client window (`"Citrix Workspace"`, `Parallels`, …). It maps to
  `backend.rdp_window`; the window title can still be disambiguated via
  `backend.rdp_window_title` in a `--config` file.
- Selection is **fail-loud**: an unknown backend, or `macos` with neither a
  window nor a host, raises rather than silently drive the wrong substrate.

**PROVEN-IN-CODE** — `tests/test_backend_factory.py`
(`test_macos_kind_builds_remote_display_backend`,
`test_macos_window_flags_merge_to_remote_display`,
`test_normalize_kind[macos]`). The default `web` path is untouched.

---

## 2. Identity on a pixel-only substrate — the pixel-compare tier

UIA/DOM does not cross the ICA/RDP boundary, so `RemoteDisplayBackend` exposes
**no structured text**. Identity therefore cannot rest on the structured tier;
historically it fell straight to the OCR name+DOB band — and OCR **collapses the
very glyphs identifiers turn on** (`O`/`0`, `l`/`1`). The rendered *pixels* do
not collapse them. The pixel-compare tier
(`runtime/identity.py::verify_pixel_identity`) exploits that: it compares the
recorded identifier crop to the live crop re-cut at the resolved point and, on
an otherwise-matching render, MISMATCHES a **localized one-glyph change** — a
different patient — at any crop scale.

That tier was **already implemented and consumed by the replayer**, but it was
**never production-reachable**: the compiler did not persist an identifier crop,
so `anchor.identifier_crop` was always `None` and the tier abstained. This
change closes that gap.

### What the compiler now does

When a click compiles with an **armed OCR identity band** (`context_text`) and
**no structured identity** (the pixel-only substrate), the compiler
(`compiler/compile.py`) also:

1. computes the tight bounding box of the same identity-band OCR lines
   (`runtime/identity.py::context_region_from_lines` — one filter, shared with
   `context_from_lines`), in the recorded frame's coordinates;
2. writes those pixels to `identifiers/<step_id>.png` in the bundle;
3. sets `anchor.identifier_crop` + `anchor.identifier_region`.

On a **structured** substrate (browser DOM, Windows UIA) `structured_identity`
is present, so this is skipped — the structured tier owns identity and **no
identity pixels are written at rest**.

**PROVEN-IN-CODE** — `tests/test_compile_identifier_crop.py`
(`test_pixel_only_recording_captures_identifier_crop`,
`test_structured_recording_writes_no_identifier_crop`).

---

## 3. The zero-false-accept guarantee (why arming this is safe)

Capturing the crop makes the pixel tier reachable **only for its safe verdicts**.
The pixel VERIFY path is **hard-gated off** (`PIXEL_VERIFY_ENABLED = False`):
across two real renders, sub-pixel JITTER of the *same* value spikes larger than
a one-glyph change, so no threshold can make a pixel VERIFY sound. The tier is
therefore **MISMATCH-or-ABSTAIN only**:

- a different identifier → localized spike not riding a drift floor → **MISMATCH
  → HALT**. The identity ladder (`run_identity_ladder`) returns the first
  definitive verdict, so this mismatch is returned **before** the lower OCR tier
  can ever VERIFY — a wrong MRN halts unconditionally on the MISMATCH branch;
- the same value, or a whole-crop render wash (theme/zoom/font drift) →
  **ABSTAIN (None)** → fall through to the OCR name+DOB tier as before.

So arming the tier can **only add a safe halt on a different identifier** — it
can never turn a wrong patient into a verified one. The correct patient is never
newly halted by it (it abstains on the same value). The worst a bad/loose crop
can do is a *fail-safe* halt, never a false accept.

**PROVEN-IN-CODE** —
`tests/test_compile_identifier_crop.py::test_captured_crop_halts_wrong_identifier_and_never_verifies`:
the compiled crop MISMATCHES a one-MRN-glyph-different live crop, ABSTAINS
(never `verified`) on the same value, and `PIXEL_VERIFY_ENABLED is False` is
asserted as the invariant that makes it unconditional.

---

## 4. PHI-at-rest posture

The identifier crop is **rendered identity pixels** (name/DOB/MRN) stored in the
bundle. This is deliberate and unavoidable: a pixel-compare identity tier
*requires* the recorded pixels — you cannot salt-hash pixels and still compare
them (the salted-hash identity template that removes plaintext identity text,
audit REM-2, exists precisely because the OCR/structured tiers *can* hash; the
pixel tier cannot). It is written **only on the pixel-only substrate** and
**only for identity-armed clicks**, so its footprint is minimal, but it is PHI
at rest. Encrypt the bundle at rest (`openadapt_flow/crypto.py`) for any real
patient data. This is the accepted price of pixel-only glyph-safe identity where
no DOM/UIA string exists.

---

## 5. TLS pin (Windows agent) — now wired in the factory

Separately, `openadapt-flow#112` (the in-guest agent's per-run TLS certificate
pin) has landed in `WindowsBackend`, so the factory no longer refuses a set
`backend.agent_tls_pin`: it threads it through as `pin_fingerprint`. An
`https://` agent session is then accepted **only** if the server presents
exactly that certificate; a pin set against a plaintext non-loopback `agent_url`
fail-closes in the backend rather than run unpinned. Unset leaves the connection
unpinned exactly as before.

**PROVEN-IN-CODE** — `tests/test_backend_factory.py::test_windows_threads_tls_pin`,
`test_windows_no_pin_leaves_connection_unpinned`.

---

## 6. GAPs (what this does NOT cover)

- **GAP — no assembled live macOS run here.** The mechanism is unit-proven; a
  live capture→compile→replay against a real Citrix Workspace window (HDX
  compression, network latency, windowed DPI scaling, credential/lock screens)
  is the deferred live pass described in [`CITRIX_PIXEL.md`](CITRIX_PIXEL.md) §5.
- **GAP — crop tightness depends on OCR.** `identifier_region` is the bounding
  box of the OCR-read identity lines; if OCR drops the identifier word entirely,
  the crop may not cover it and the tier abstains for that step (fail-safe: the
  OCR name+DOB tier and its disclosed same-name/same-DOB residual still apply,
  see [`LIMITS.md`](LIMITS.md)).
- **GAP — same-surface only.** Like all pixel-substrate verification, identity
  and on-screen read-back read the same surface the action drove; independent
  system-of-record confirmation needs an `EffectVerifier` (`effects` config).

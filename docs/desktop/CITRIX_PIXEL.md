# Citrix / remote-display PIXEL-ONLY proof

A faithful, on-infra-we-control proof that the compiler's **visual floor** works
when the substrate is **pixels only and the structural (UIA) rung is
unavailable** — the exact constraint a Citrix-delivered Windows EMR (Accuro)
imposes. This de-risks a real clinic pilot *before* Accuro access.

Every claim below is tagged **PROVEN-LIVE** (demonstrated on the real remote
-display surface this session), **PROVEN-IN-CODE** (real code + green offline
unit tests, assembled live-run deferred), or **GAP** (what real Accuro adds and
this proof does NOT cover). No result is fabricated.

---

## 1. Topology — and why it is a faithful Citrix analog

Over Citrix, the local machine holds a **Citrix Workspace/Receiver window that
paints the pixels of a remote session**. There is no in-guest agent on our side
of the ICA boundary, and **UIA/MSAA does not cross ICA**. So the production
Accuro wire is a **local-OS backend that screenshots the Workspace client window
and injects OS-level input into it** — *not* an RDP-protocol client
(`backends/rdp_backend.py` speaks RDP via `aardwolf`, which is a different wire
and cannot connect to Citrix ICA).

This proof uses the same *class* of substrate we can stand up locally: the
**Parallels Desktop VM window** on the Mac, showing the Windows guest, driven by
`openadapt_flow/backends/remote_display.py` (`RemoteDisplayBackend`):

- **capture** — `CGWindowListCreateImage` grabs ONLY the target window's pixels
  (the remote display; nothing else is visible to the driving process);
- **input** — `CGEvent` posts OS-level mouse/keyboard at screen points mapped
  from captured-pixel space;
- **no structural layer** — the backend implements ONLY the base `Backend`
  protocol, never `StructuralBackend` / `IdentityBackend` /
  `StructuralActionBackend`, so the resolver's `structural` (UIA) rung is
  genuinely unavailable and identity falls back to the OCR name+DOB tier.

Why it is faithful: host-side pixels of a remote guest + host OS input injected
into the window + **no access to the guest accessibility tree through the
window** — the same three properties Citrix imposes. **The backend is title
-swappable**: point `owner_substr`/`title_substr` at "Citrix Workspace" / the
Accuro window and the screenshot + inject code is byte-for-byte identical. That
reusability is the point (the readiness assessment's §4 "MISSING backend").

Why it is not the *whole* story — see §5 (GAP): a local hypervisor console is
not a network remote-display, so HDX compression/latency, windowed DPI scaling
of the ICA client, and credential/lock screens are not exercised here.

---

## 2. What is PROVEN-LIVE this session

Captured on the real Parallels VM window (Windows 11 Pro ARM guest, macOS host,
Apple M2 Max):

- **PROVEN-LIVE — pixel capture of the remote-display client window.** Screen
  Recording is granted; `CGWindowListCreateImage` returns real guest pixels
  (3024×1888 at 2.0× Retina scale for a 1512×944-pt window). The stand-in
  clinical app (`scripts/desktop/patient_notes.ps1`, a WinForms
  chart+note UI over a SQLite store) was captured showing its full roster
  (Neil Sorenson, Nell Sorensen, …) — OCR-legible from the client-window pixels.
- **PROVEN-LIVE — the structural rung is genuinely OFF.** `isinstance(backend,
  StructuralActionBackend/IdentityBackend/StructuralBackend)` are all `False`;
  the backend has no `structural_locator_at` / `structured_text_at`. Resolution
  therefore *can only* run the visual ladder — the Citrix floor.
- **PROVEN-LIVE — OS-level input injection works.** With Accessibility granted,
  `CGEvent` clicks are delivered (they dismissed the Parallels update dialog and
  resumed the VM via its play button — real state changes). Control **location**
  works pixel-only: `vision.find_text` located the Search button (confidence
  1.0), patient rows, and the Save Note button from the client-window pixels.
- **PROVEN-LIVE — the harness is real and snapshot-safe.** On this Parallels
  *Standard* edition: `snapshot` / `snapshot-switch` (revert) / `snapshot-delete`
  all work; the four pre-existing user snapshots were never touched; the harness
  deploys `pn_db.py` + `patient_notes.ps1` + `session1_launch.py`, seeds the DB
  (10 patients incl. the built-in look-alike pair), launches the app in the
  interactive session, and reads DB ground truth — all via `prlctl`.

## 3. What is PROVEN-IN-CODE (assembled live run DEFERRED)

The assembled end-to-end assertions live in the opt-in test
`tests/e2e/test_citrix_pixel_e2e.py` and are covered by green offline unit tests
(`tests/test_remote_display_backend.py`, `tests/test_onscreen_verifier.py`):

- **PROVEN-IN-CODE — pixel-only == no UIA arming.** The recorder duck-types
  `structural_locator_at`; the backend lacks it, so the compiled bundle carries
  **no** structural locator. The test asserts `structural_armed_coverage == 0`
  (the exact inverse of the in-guest structural proof PR #102's
  `armed_coverage == 1.0`).
- **PROVEN-IN-CODE — visual-floor resolution.** Replay runs
  `Replayer(backend, use_structural=False)`; the test asserts `rung_counts`
  contains only `template`/`template_global`/`ocr`/`geometry`, never
  `structural`, and prints each step's rung + confidence + identity status.
- **PROVEN-IN-CODE — on-screen OCR read-back verify.**
  `runtime/effects/onscreen.py::OnScreenReadbackVerifier` implements the
  `EffectVerifier` protocol, OCRs the saved-state region and rules
  CONFIRMED / REFUTED (readable-but-wrong → HALT) / INDETERMINATE (unreadable →
  HALT). **This is SAME-SURFACE verification, not independent confirmation**
  (§4).
- **PROVEN-IN-CODE — identity gate on pixels HALTs on a look-alike.** With the
  target row's identity band changed to a different patient, the OCR identity
  tier returns `mismatch`/`abstain` and the run HALTs rather than write the wrong
  chart; DB ground truth confirms the wrong chart was untouched.
- **PROVEN-IN-CODE — halt-on-ambiguity.** Under render drift
  (`font_scale=1.5`, dark theme via `pn_env.json`) that degrades OCR/template,
  the resolver returns no confident match and the run HALTs rather than click a
  guessed target.

**DEFERRED (rerun with `OAFLOW_CITRIX_PIXEL_E2E=1` when the VM is up):** a single
assembled live pass emitting the real `rung_counts` / confidences / verify /
identity-halt / ambiguity-halt logs. It was not completed live this session for
environment reasons (§6), not for any gap in the mechanism. The test is
env-gated and **skips cleanly** when the VM/permissions are absent — it never
fails spuriously and never fabricates a pass.

---

## 4. Honest posture — where the safety actually rests

The on-screen read-back reads the **same screen the action drove**, so it cannot
see the transactional faults an independent system-of-record read catches
(`docs/LIMITS.md`: "5 of 7 write faults silent" — partial save, phantom
optimistic-UI success, duplicate, lost update, double-delivered click). A
rendered "Saved" that the record never received still reads as "Saved". A single
trailing-glyph difference (patient "3" vs "8") is likewise **not** discriminated
by fuzzy same-surface OCR — the same glyph-collapse that limits OCR identity
(`test_onscreen_verifier.py` pins this honestly).

So the pixel-only safety guarantee does **not** rest on the read-back. It rests
on the **identity gate** (is the resolved row the recorded patient?) and
**halt-on-ambiguity** (never guess a target). The read-back is a best-effort,
fail-safe consistency signal layered on top.

---

## 5. PROVEN here vs what real Accuro-over-Citrix adds (GAP list)

- **GAP — ICA, not a hypervisor console.** This drives the Parallels *local*
  console window. A real Citrix session adds **HDX compression artifacts,
  server-side font rendering, and network latency**; OCR/template on a
  compressed ICA frame is the single biggest unmeasured unknown. `RapidOCR` and
  `TEMPLATE_THRESHOLD=0.985` are tuned on locally-rendered ARM WinForms
  (`docs/desktop/LIMITS.md §1`); those absolute numbers will move on ICA.
- **GAP — coordinate mapping through a windowed/DPI-scaled ICA client.** Here the
  captured window is a clean 2.0× multiple of window points. A Workspace window
  that is windowed, zoomed, or on a mismatched DPI breaks the
  "captured-pixels == scale × window-points" assumption the backend relies on;
  a real deployment needs a client-window calibration step.
- **GAP — synthetic input acceptance.** OS `CGEvent` injection is delivered to
  the Parallels window; a hardened Citrix Workspace may treat synthetic vs
  hardware input differently, and remote focus/cursor races differ over HDX.
  Note this session already found that **synthetic Unicode keystrokes are not
  forwarded to the guest** — the backend types via **hardware-like scancodes**
  (`_CHAR_KEYCODES`) for exactly this reason; Citrix may impose further limits.
- **GAP — credential / lock / session-timeout screens.** Citrix sessions
  disconnect, lock, and re-auth; none of that is modelled here. A pilot needs an
  explicit "am I on the expected app screen?" gate before every consequential
  action.
- **GAP — no independent effect verification.** Accuro exposes no DB/FHIR/file
  to the local box, so on-screen read-back is the *only* no-API substrate — with
  the same-surface limits in §4. Any consequential write whose effect cannot be
  independently certified must remain operator-gated at volume.
- **GAP — identity availability (over-halt rate) unmeasured on real charts.** The
  gate's *safety* invariant (0 wrong-patient accept) is well-argued, but how
  often it HALTs a legitimate action on real Accuro pixels is unknown until
  measured on real (or realistic ICA-rendered) patient lists.

---

## 6. Environment constraints discovered (for the rerun)

- **Parallels Standard edition** blocks `prlctl start/stop/suspend/resume`; a
  suspended VM must be resumed via the GUI **play button** (doable via injected
  input). `snapshot`/`revert`/`delete` and `exec`/`capture` do work.
- The VM is configured **"suspend on window close"**, so it suspends whenever its
  window loses foreground on a shared desktop — the main flakiness for a
  sustained multi-step live drive. A dedicated single-app machine (what a real
  pilot uses) removes this. Changing the setting needs the VM fully stopped.
- **macOS permissions are load-bearing:** Screen Recording (capture) and
  Accessibility (input) must be granted to the driving app. The backend FAILS
  LOUD if Accessibility is missing — a dropped synthetic click must never look
  like success.
- **Occlusion matters for input, not capture:** window-buffer capture works
  through occlusion, but a coordinate click lands on whatever is visually
  topmost, so the client window must be truly **frontmost**
  (`ensure_foreground()` verifies this).

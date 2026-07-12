# Limits — what compiled replay does not do

openadapt-flow compiles a demonstration into a deterministic, vision-only
replay. The README shows what that buys; this page states what it costs,
found by deliberately attacking our own system before anyone else does.
The full experiment matrix with evidence is in
[docs/validation/VALIDATION.md](validation/VALIDATION.md); the failure
modes below are pinned by characterization tests so they cannot silently
change.

## The dangerous list: today's silent failure modes

These are the cases where compiled replay does the wrong thing and reports
success. They are open problems, not caveats. (Former members of this
list: wrong-entity clicks in repeated structures and unverified typed
input were fixed on 2026-07-08; anti-robust postcondition mining — clock
fragments, "longest new text" grabbing data, DOB banners eaten by the
timestamp filter, parameter values leaking into landmarks — was fixed on
2026-07-09; near-name sibling rows sailing through the identity matcher
were fixed on 2026-07-10. All moved to the safe-halt section below.)

- **Identity verification covers ONLY armed steps — and real bundles arm
  a minority of clicks.** The most recent live OpenEMR check (2026-07-09)
  armed **4 of 12** click steps; the earlier fresh bundle armed 7 of 12.
  The rest compile with no identity context at all (no readable row text
  outside the target's own crop: login buttons, icon-only pencils,
  too-generic bands) and an UNARMED click proceeds with **no identity
  check whatsoever** — every guarantee in the wrong-entity section below
  is scoped to armed steps only. As of this PR the coverage is a
  first-class, auditable metric: `workflow.json` carries per-step
  `identity_armed` / `identity_unarmed_reason` (auditable BEFORE running),
  every run's REPORT.md states "N of M click steps identity-armed" and
  lists the unarmed steps by id with the reason, and the benchmark
  methodology sections report the same number. Disclosure does not close
  the gap: a wrong-entity click on an unarmed step is still silent.
- **Steps with no visual AND no structural effect assert nothing.** Since
  2026-07-09 an action whose recorded before/after frames are identical
  falls back to structural postconditions (URL change, title change, new
  tab opened) when the recording backend can observe them — the new-tab
  click is now verified. What remains vacuous: actions with no structural
  effect either (an inert native `<select>`), and bundles recorded on
  backends without structural observations (native OS, RDP). There is
  still no minimum-verification floor.
- **Targets whose only discriminative text is their own label.** The
  identity check deliberately excludes the target's own label (labels are
  mutable evidence the resolution ladder heals through under rename
  drift), so a control with no OTHER text on its row — e.g. a typeahead
  suggestion for a *parameterized* prefix — compiles with no identity
  context and is still clicked by position, unverified. Bands whose
  surviving text is shorter than 12 squashed characters are treated the
  same way (a generic fragment like "Active High 3" matches every sibling
  row — recording it would arm false confidence, so it is not recorded).
- **Risk classification is opt-in and never auto-assigned.** Every step
  compiles as `risk="reversible"` unless the compile caller passes
  `risk_overrides` naming the step; nothing in the compiler infers
  irreversibility. Concretely: in a default-compiled bundle, an
  unreadable identity band on a chart-open click **proceeds** (flagged
  `identity: "unreadable"` in the report), and the wrong-patient-write
  tail behind that click **remains reachable with a green report** — the
  "irreversible steps refuse on unreadable band" branch never runs unless
  a human marked the step at compile time.
- **Postconditions read the SCREEN, not the system of record — so
  transactional write faults are silent.** A 2026-07-12 fault-model study
  (`benchmark/fault_model/`) drove 90 replays through a real persistence
  boundary and found the vision postconditions (`text_present` /
  `region_stable` / `url_changed`) silently mishandle **5 of 7** transactional
  fault classes: a duplicate submission or double-click writes a SECOND record
  behind a clean success; an optimistic-UI update the backend later rejects
  reports success over an empty database; a partial save drops a field; a
  stale/concurrent edit overwrites another user's change. None is render drift
  — the recorded pixels match perfectly, so self-healing cannot catch them —
  and none is caught, because the screen showed success. Closing this needs
  verification against the system of record (an app-specific effect check or
  API/DB read) and an at-most-once guard (idempotency keys); neither is
  generically expressible in a vision-only replay. Full taxonomy:
  `benchmark/fault_model/FAULT_MODEL.md`.

### The optional drift-oracle can turn a halt into a pass (opt-in tradeoff)

When an on-prem VLM appliance is configured (`OPENADAPT_FLOW_VLM_URL`, off by
default), a postcondition that deterministically FAILED gets one confirmation
pass through the VLM state-verifier — the same heal-under-drift the resolution
ladder does for click targets. It is deliberately narrow and veto-safe: only
`text_present` / `region_stable` are eligible (never structural or
`text_absent`, where a failure is real, not render drift), and only a confident
`"yes"` rescues — `"no"`, `"uncertain"`, and any appliance outage keep the
halt. Every rescue is recorded in the run report and counted as a model call.

The honest residual risk, now **measured** end-to-end against a real served
model (Qwen3-VL-4B-4bit, MLX; `benchmark/appliance_validation`): a screen-reading
VLM confirms what is *on screen*, so a genuine failure whose screen ambiguously
reads as success can be rescued. On a labelled trap set the state-verifier
**correctly refused 7 of 8** should-halt screens (error banner, blank form,
cancelled, wrong-patient, logout, validation error, stale dashboard) and
**false-rescued 1 of 8 (~12.5%)** — an in-progress `"Saving…"` screen read as
saved — while correctly confirming 6/6 drift-obscured real successes. So the
rescue is real but imperfect: it trades ~1-in-8 on genuinely-ambiguous
in-progress states for availability, which is why it is opt-in and audited.
It does **not** address the transactional class above — that screen already
showed success, so both the deterministic check and the VLM would.

Two more measured caveats from the same run: (1) the served 4-bit model emits
degenerate output on native-Retina (~1800px+) screenshots, so full frames are
downscaled below ~1024px before the state-verifier / grounder see them (identity
crops are small and unaffected); un-downscaled, both tiers silently went inert
(safe-halt, but useless). (2) The **grounder does not resolve dense lists** at
this model/scale: on a dense patient table it found the correct *column* but not
the *row* (0/6 hits, ~470px median error). It fails safe (a bad proposal still
faces the deterministic identity band before any click), but it is not yet a
dependable rung for list-dense UIs — a stronger grounding model is the open
item.

## Identity is verified against STRUCTURED text where the backend provides it

The wrong-entity story above is an OCR story, and OCR has a proven ceiling.
An adversarial review established an **impossibility result**: two DIFFERENT
patients with the same NAME and same DOB whose only distinguishing field is a
collapsible identifier — an MRN differing by a single O/0 or l/1 glyph, whether
ALPHANUMERIC (`MG4408` vs `MG44O8`, `AC50061` vs `AC5OO61`) or PURELY NUMERIC
(`100512` vs `1OO512`, `417063` vs `4l7063` — the 9th reopening) — render to a
**byte-identical OCR band**. That band is literally the same input a legitimate re-read of the
true row produces, so *no function downstream of OCR* can separate the two
(same input, no distinguishing output). Measured on the real render→OCR→match
pipeline this WAS **~43.8% false accept** on the digit-flanked shape in the
name-in-band config (#27 trusted a matched name+DOB and let the collapsible
MRN "corroborate"). The **8th wrong-patient reopening** proved that unsound: a
matched name+DOB cannot rule out a same-name/same-DOB homonym whose MRN glyph
OCR collapsed, so the OCR tier now **ABSTAINS** on any collapsible identifier
instead of verifying — turning that ~43.8% false accept into a safe HALT (0
false-accept, high over-halt). It is not a tuning gap; it is the limit of
OCR-based identity, and the honest response is to refuse rather than guess.

The fix is architectural: **stop relying on OCR for identity when a
higher-fidelity signal exists.** Identity is now an ordered LADDER of verifier
tiers (`openadapt_flow.runtime.identity.run_identity_ladder`), highest-fidelity
first; the first tier that can judge the substrate wins and its verdict is
FINAL:

1. **Structured text (DOM / a11y).** When the backend exposes
   `structured_text_at(point)` — the browser reads the DOM element under the
   point (`elementFromPoint` → row `textContent` + `aria-label`); a native
   desktop backend reads the accessibility tree (Windows UI Automation
   `Name`/`Value`/text, macOS AX) — the recorded target's structured identity
   string and the live structured string at the resolved point are compared
   by **exact/normalized match, in which `0` and `O`, `1` and `l` are DISTINCT
   characters**. The glyph-collapse cannot occur: the two rows are different
   strings in the DOM/a11y tree. This runs on the browser backend today and on
   native desktop wherever the a11y API returns text — importantly, an element
   with **no stable `AutomationId` usually STILL exposes Name/Value text**, so
   UIA/AX identity is viable on most native apps even where an AutomationId
   selector is not. On the real dense sibling surface
   (`benchmark/dense_surface/DENSE_SURFACE.md`) the structured-text path closes
   the class at **0 false accept AND ~0 added over-halt** — including the exact
   digit-flanked attack that produces ~43.8% false accept on the OCR path —
   because DOM text is invariant across replay font/resolution: it closes the
   class **with no OCR-availability cost.** A structured-text mismatch is
   authoritative; the OCR fallback never overrides it.
2. **Pixel-compare of the identifier crop (`verify_pixel_identity`) — VERIFY
   HARD-GATED (Blocker 2).** Citrix/RDP/VDI sessions and apps with a broken
   a11y tree expose NO structured text. There, the recorded target's rendered
   identifier CROP is compared against the live resolved identifier crop at the
   pixel level: OCR collapses `O`/`0` and `l`/`1`, but the PIXELS do not.
   **The adversarial review of PR #31 found the promoted metric was
   crop-scale-SENSITIVE**: `PIXEL_SAME_THRESHOLD` was an absolute whole-crop
   mean-abs-diff on a crop force-resized to a fixed WIDTH, so on a realistic
   wide identifier CELL a one-glyph-different MRN's diff DILUTES below the
   threshold and **VERIFIES a different patient** (measured: a 420px cell,
   `AC50061` vs `AC58061`, scores 0.016 < 0.049 → false-accept; a same-value
   1px cross-render JITTER scores 0.087 → false-abort — the metric is
   inverted at realistic scale). The distance is now **scale-invariant**
   (canonicalize to a fixed HEIGHT preserving aspect; a one-glyph change is a
   consistent localized SPIKE above the per-window drift floor at any crop
   width), so a DIFFERENT MRN MISMATCHES at every cell scale. But sub-pixel
   cross-render JITTER of the SAME value spikes LARGER than a one-glyph change,
   so no threshold makes VERIFY safe. The **VERIFY path is therefore HARD-GATED
   (`PIXEL_VERIFY_ENABLED=False`)**: the tier may only MISMATCH (a scale-
   invariant localized spike → safe HALT) or ABSTAIN, never grant a pass, until
   (a) the compiler captures a FIXED-SIZE identifier crop at record time and
   (b) a jitter-robust distance is validated end to end. The pixel tier is not
   production-reachable today (the compiler does not populate `identifier_crop`),
   so the gate has no production impact — it prevents a latent false-accept from
   ever shipping. Free, no model.
3. **Local-VLM veto (`verify_vlm_identity`) — OPTIONAL, off by default, TRULY
   VETO-ONLY.** Only when a verifier is injected, identity rests on a
   glyph-confusable identifier, AND the cheaper tiers abstained (render drift
   the pixel tier can't judge): a LOCAL open VLM (Qwen3-VL-4B via MLX,
   ~0.8s/call, ZERO cloud calls) answers same/different. **Veto-only** now
   means what it says: a `DIFFERENT` or unsure answer HALTS, and a `SAME`
   answer does **NOT** grant a pass — it ABSTAINS (returns None), leaving the
   decision to prior/other evidence. A local VLM reading a glyph-confusable
   identifier is trustworthy to REJECT a wrong patient (100% detection on the
   collapse surface) but not to CERTIFY a right one, so when the VLM is the
   sole remaining signal a `SAME` answer → abstain → the ladder HALTs. (The
   earlier code returned `verified` on `SAME`, so in the pixel-abstain path it
   acted as a full verifier — fixed in this PR.) It is OPTIONAL like the
   grounder: the **default install pulls no model**. Enable it by passing an
   `openadapt_flow.runtime.identity_vlm.MLXIdentityVLM` (or any `IdentityVLM`)
   into `Replayer(identity_vlm=...)`.
4. **OCR name+DOB band — ABSTAINS on any collapsible identifier (8th
   reopening).** When no structured text is available and the pixel/VLM tiers
   abstained, identity falls back to the OCR matcher below. #27 let a matched
   NAME "carry" identity so a digit-body confusable MRN only "corroborated"
   and did not block — the adversarial review of PR #31 proved that unsound: a
   same-name/same-DOB HOMONYM (`AC50061` vs `AC5OO61`) collapses to a
   byte-identical band, so a matched name+DOB CANNOT rule it out. The OCR tier
   now **ABSTAINS** whenever the band rests on a glyph-confusable identifier —
   **ANY identifier-position token carrying an O/0 or l/1/I, numeric or
   alphanumeric** (the 9th reopening removed the earlier alphanumeric-only
   scoping: a purely-numeric `100512` vs a homonym's `1OO512` is exactly as
   collapsible, and the letter+digit-mix predicate missed it). It can neither
   certify SAME nor assert DIFFERENT — and on a pure-pixel substrate with no
   structured/pixel/VLM verifier the ladder then HALTs. A different-NAME
   sibling is still an affirmative MISMATCH; a clean name+DOB with a
   NON-confusable identifier (one bearing NONE of `{0,1,O,l,I}`, e.g. `RC79284`)
   still verifies. An OCR-split identifier is covered too: a confusable glyph in
   any numeric/alnum FRAGMENT abstains. The glyph-disambiguating /
   high-resolution identifier OCR pass is the roadmapped mitigation.

Net — the ladder is fail-safe, and its safety number is measured on the REAL
production tier stack. **On browser/desktop substrates the structured-text
tier CLOSES the glyph-collapse class at no availability cost** (O and 0 are
distinct in the DOM/a11y tree). **On a PURE-PIXEL substrate a collapsible MRN
is NOT safely verifiable and HALTS today**: the pixel-compare VERIFY path is
hard-gated (Blocker 2, above), the VLM is veto-only, and the OCR name+DOB tier
ABSTAINS on any confusable identifier (8th reopening). In other words —
**OCR alone cannot verify a collapsible MRN; a pixel-only substrate needs the
structured-text tier (or, once Blocker 2's crop capture + jitter-robust
distance land, the pixel-crop tier) to VERIFY, and otherwise HALTS.** Every
tier is fail-safe — unsure abstains to the next, and if nothing verifies the
run HALTS.

The integrated ladder, driven through the REAL `Replayer._verify_identity`
(no hand-built tier subset), measures **0 false-accept across ALL substrate
configs — including the same-name/same-DOB homonym**
(`openadapt_flow.validation.identity_ladder`,
`benchmark/identity_ladder/IDENTITY_LADDER.md`): `structured` 0 FA / 0
over-halt; every pure-pixel config 0 FA / 100% over-halt (the honest cost of
"OCR cannot verify a collapsible MRN"). An EARLIER version of that harness
measured the pixel-only configs against a `[pixel]`-only tier subset, omitting
the OCR tier the replayer always appends — so its "0 false-accept" was measured
against a NON-production stack and never exercised the OCR tier where the
homonym actually false-accepts; the harness now drives the real method. The one
irreducible floor no vision method can cross is a font that renders `O` and `0`
(or `l` and `1`) **pixel-identical** — none was found among 14 common UI fonts
(`benchmark/pixel_identity`).

## What it halts on (safely, but it halts)

Failures below stop the run with an accurate per-step report — no wrong
actions observed — at the cost of availability:

- **Wrong-entity targets in repeated structures** (fixed 2026-07-08,
  matcher hardened 2026-07-09, matcher REBUILT 2026-07-10 after the
  near-name sibling reopening, then REDESIGNED the same day after the
  out-of-corpus review found 13 silent-verify probes — the fourth
  reopening of this P0; formerly the top silent failure mode). When data
  shifts between runs — a row added above the target, the target's row
  deleted, a look-alike sibling, a re-sorted table — the resolver still
  finds a pixel-identical target at a plausible position, but the
  pre-click **identity check** compares the resolved row's text (the OCR
  lines of the resolved point's own text row, minus the target's own
  label and volatile cells, excluded identically at record and replay
  time) against the recorded row and refuses to click on mismatch.
  Matching is order-insensitive per token (OCR re-reads the same band in
  different segmentation orders), accepts a token ONLY when it is
  OCR-equivalent — identical under the character-confusion classes real
  engines produce (l/1/i, O/0, 5/s, rn/m, cl/d, ...) or a
  full-consumption token split/join — and the decision holds several
  budgets at once: >= 0.8 coverage; no contiguous uncovered run over 4
  squashed characters; zero *contradicted* characters (near-miss
  siblings — Phil/Philip, John/Joan, an off-by-one DOB or an all-DIGIT
  swapped MRN digit, a Jr/Sr suffix on one side, a replaced word or a
  replaced 1-2 char token such as a middle initial or the SEX column);
  zero *suspect* characters — a DISCRIMINATOR token matched ONLY by a
  confusion equivalence, which comes in two kinds: a **name** collision
  (a name-plausible LETTER-LETTER confusion — Neil/Nell, Clay/Day,
  Marnie/Mamie — indistinguishable from a real sibling), and an
  **identifier** collision (a RECORDED token that contains a digit — an
  MRN, account number, chart ref or DOB — matched only across a
  letter/DIGIT confusion, l/1, O/0, S/5, Z/2, B/8, g/9, so 'A01234' and
  'AO1234' are DIFFERENT identifiers, exactly what disambiguates
  same-name patients); zero unexplained observed name-shaped tokens (an
  appended middle name, a second row OCR-merged into the band, a
  message/cc row that merely MENTIONS the recorded patient); and no
  absent name-like alphabetic token of 4+ characters (a band must not
  verify with its identity token never read). The 2026-07-09 matcher's
  containment and 0.7-similarity tiers measured 53.9% false-accept on
  frozen corpus v1; the first rebuild measured 0.0% there but the FIRST
  2026-07-10 review showed that zero was partially tautological (v1's
  labeling rule excluded confusion-collided names, short-token
  discriminators, observed supersets and absent-name shapes; 13
  out-of-corpus probes in those classes all silently VERIFIED), and the
  SECOND 2026-07-10 review then found the suspect budget guarded NAME
  tokens only — it was OFF for MRNs/account numbers, so a different
  patient's alphanumeric identifier one letter/digit-confusable char
  apart silently VERIFIED (the 5th wrong-patient reopening; v1's
  `mrn_digit_swap` class only ever changed DIGITS, which are never
  confusion-equivalent, so it could not surface the hole). With the
  identifier-suspect fix the matcher measures **0.000% false accepts on
  corpus v1+v2+v3 plus the 18-probe set** — scoped exactly to those
  corpora (STRING pairs with hand-injected OCR noise), not to the world,
  and the operating point was fit on the same corpora that produce the
  headline (docs/validation/IDENTITY_ROC.md states this bias plainly).
  That string-level zero did **NOT** hold on real dense OCR (the SIXTH
  wrong-patient reopening): a dense sibling record LIST rendered to pixels
  and read by the repo's own RapidOCR
  (`benchmark/dense_surface/DENSE_SURFACE.md`) measured **7.22% false
  accept (26/360)** — two same-name patients whose MRNs differ by a single
  letter/digit near-homoglyph (`C0X3834` digit-ZERO vs `COX3834` letter-O)
  are read as the SAME string BEFORE band matching, so the bands are
  RAW-IDENTICAL, the identifier-suspect rule (which needs two DIFFERENT
  strings) never fires, and the sibling verified at coverage 1.0 (60% on
  the O/0 class). The corpora could not surface this because they inject
  the collision as a text edit that keeps the two variants textually
  distinct — the exact condition the suspect rule was built for; the
  collapse happens INSIDE OCR, upstream of any string-level rule. The
  sixth fix HALTED on a homoglyph LETTER (O/l/I) in the identifier.
  **SEVENTH reopening (digit-flanked):** a real MRN is `<alpha
  prefix><digit body>`; when the confusable glyph is DIGIT-FLANKED,
  RapidOCR reads the DIGIT form on BOTH a patient (`AC50061`) and a
  DIFFERENT same-name/DOB patient (`AC5OO61`, letter O) — both collapse to
  `AC50061`, NO homoglyph letter survives, the sixth flag misses it, and
  the sibling verified. Measured **~87% false accept on the digit-flanked
  shape** through the real render→OCR→match pipeline. No string flag on
  the identifier can recover a distinction OCR destroyed at the pixel
  level, and flagging the digit side (any 0/1 in an MRN) would halt ~3 of
  4 real MRNs. So identity was moved onto the OCR-reliable, redundant
  signal — the patient **NAME + DOB** — with a confusable-glyph identifier
  used only as CORROBORATION. When a discriminative NAME carries identity
  (the name-in-band config) a confusable-DIGIT MRN does NOT block
  verification (a wrong sibling differs in name/DOB and is caught by
  coverage/contradiction — the common case). When identity would rest
  SOLELY on a glyph-vulnerable identifier — the clicked NAME cell excluded,
  leaving only DOB + MRN + generic columns — a DIGIT-body glyph-vulnerable
  MRN HALTS. A homoglyph LETTER stays a hard halt, preserving the
  sixth-reopening closure with no regression: **the real dense-surface
  study (`DENSE_SURFACE.md`) holds at 0/360 false accept**, and the
  digit-flanked collapse is CLOSED in the name-excluded config. The cost is
  availability, not safety, and is NOT a reduction: closing the name-
  excluded hole raises that study's per-click false abort 18.89%→45.00%
  (all of it the digit-side sole-discriminator halt in `click_name`;
  `click_action`, where the name carries, stays at 18.89%) — the cheap
  direction. **EIGHTH reopening (same-name/same-DOB homonym) — CLOSED:**
  #27 disclosed a residual — a same-name/DOB DIFFERENT patient whose
  digit-body MRN collapses to the target's, WITH the name displayed and
  matching — as a "known OCR-substrate limit". An adversarial review of PR #31
  proved it a LIVE, production-reachable wrong-patient VERIFY (`AC50061` vs
  `AC5OO61`, both OCR to `AC50061`; name+DOB carried → verified at coverage
  1.0 through the real replayer). The name-carry suppression of the
  confusable-identifier halt is REMOVED: the OCR tier now **ABSTAINS**
  whenever the discriminative band contains a glyph-confusable identifier —
  **any identifier-position token with an O/0 or l/1/I, numeric OR
  alphanumeric** (see the 9th-reopening note below; the earlier
  "alphanumeric MRN/account token" scoping is WITHDRAWN), REGARDLESS of a
  matched name+DOB — it can neither certify SAME nor assert DIFFERENT, so on
  a pure-pixel substrate the ladder HALTs. A different-NAME sibling still
  MISMATCHES; a clean name+DOB with a NON-confusable identifier still
  verifies. The complete upstream fix (glyph-disambiguating / high-resolution
  identifier OCR, or the structured-text tier) is what lets such a target
  VERIFY rather than HALT. The availability bill is the honest cost of this
  refusal — the OCR tier now aborts EVERY same-entity band that carries a
  confusable identifier: **49.3% false aborts on frozen corpus v1** and
  **43.6% on v2** (from 28.2% / lower before the 8th fix; up from 48.2% after
  the 9th-reopening change flagged the numeric identifiers the frozen corpora
  already contained), the jump being exactly the collapsible-identifier
  abstains; on real browser/desktop the
  structured-text tier verifies these with no OCR ambiguity, so this cost
  bites only on pure-pixel substrates. Earlier availability history:
  **28.2% pre-8th on v1's noise classes** (10.7% pre-review, 21.2% after the
  first redesign), concentrated in occlusion — where
  a recount showed ~half the aborted bands still had BOTH name tokens
  readable and aborted on trailing DOB/MRN loss, an availability cost,
  not the "correct epistemic refusal" the earlier doc claimed — plus
  digit-class OCR noise that lands on an identifier token and now aborts
  (v2's `digit_confusion_true_row` class rose from 0% to ~49%: the
  true-row identifier-noise cost of the 5th-reopening fix), the
  letter-letter confusion mechanism, and capitalized adjacent-row bleed.
  For a parameterized target (e.g. *which patient*
  to open), the run's value is substituted into the recorded band and
  the whole substituted band must match — a row that merely mentions
  the run's value does not verify. Caveats, disclosed: only ARMED steps
  get any of this (see the dangerous list — live bundles armed 4-7 of
  12 clicks); when the live band is unreadable even at 2x resolution,
  reversible steps proceed exactly as before with the step flagged in
  the run report (`identity: "unreadable"`), and only
  compile-time-marked irreversible steps refuse; dense-table OCR
  undercount is real, which is why the 2x retry exists. Residual
  verify/abort classes are listed in "Known remaining" below.
  **NINTH reopening (purely-numeric MRN) — CLOSED:** the 8th fix's predicate
  required an identifier to contain BOTH a letter and a digit, so it flagged
  only ALPHANUMERIC MRNs. A real MRN can be all digits, and a numeric MRN is
  just as glyph-collapsible: `100512` (recorded) and a DIFFERENT same-name/DOB
  patient's `1OO512` (letter O's) OCR to the byte-identical `100512`, so the
  letter+digit predicate never flagged `100512` and the homonym VERIFIED on the
  real replayer (also `400761`/`4OO761`, `417063`/`4l7063`). The rule is now
  STRUCTURAL and conservative: the OCR tier VERIFIES same-identity only when
  there is provably NO collapsible glyph in ANY identifier-position token — a
  bare alphanumeric run ≥ 3 chars carrying a digit (numeric, alphanumeric, or
  lowercase; a separator-bearing date and a digit-free name are excluded) that
  bears one of `{0,1,O,l,I}` forces ABSTAIN, and when uncertain whether a token
  is an identifier it is treated AS one (→ abstain, the safe over-halting
  direction). Split identifiers are covered — the flag is a property of the
  recorded token charged on any match path, so a confusable glyph in a numeric
  FRAGMENT of an OCR-split MRN still abstains. The numeric hole was hidden
  because BOTH the ROC corpora and the dense/ladder collapse corpus were
  alpha-prefixed; purely-numeric and split homonyms were added and re-measured
  on the real replayer at **0 false accept** (`benchmark/identity_ladder`,
  `benchmark/dense_surface`). Cost: a higher OCR-path over-halt, disclosed
  above and in those studies; a clean identifier bearing none of `{0,1,O,l,I}`
  (e.g. `RC79284`) still verifies.
- **Typed input that cannot be confirmed** (fixed 2026-07-08, verification
  hardened 2026-07-09). After every TYPE action, an OCR-able typed value
  must be READ back from the field region (2x-resolution retry included);
  a pixel change alone is accepted only when the region gained no other
  readable text — the masked-field (password dots) shape, where
  "readable" counts confident alphanumeric characters (dot glyphs OCR as
  nothing, punctuation runs, or low-confidence noise depending on the
  platform renderer) — so a dialog painting over the field no longer
  false-verifies while keystrokes fell elsewhere. If nothing changed at all (focus stolen, keystrokes on
  `<body>`), the replayer re-clicks the field, selects-all, retypes once,
  and halts if the input still cannot be confirmed; if the region changed
  but the value is unreadable, it halts immediately WITHOUT retyping
  (select-all could destroy pre-existing field content, and the refocus
  re-click could re-fire whatever now sits at that point). Remaining
  caveats, disclosed: the refocus re-click targets the last click point —
  if a stateful control now occupies it, the retry itself can act on it;
  select-all-retype on a false-negative first attempt replaces whatever
  the field held, which destroys pre-existing content when the field was
  not empty (recorded flows type into fields they just focused, but this
  is an assumption, not a check); a value the app visibly transforms
  while typing (auto-formatting) can fail read-back and halt a correct
  run; and OCR-illegible-but-rendered text in a changed region halts as
  unverifiable (availability cost, not a wrong action).
- **Anything that rescales or reflows the screen.** Browser zoom, display
  scale factor, or a font-size preference bump aborts at the first step.
  Self-healing covers palette changes, moved controls, and renamed labels;
  it does not cover scale or reflow. A purely cosmetic 125% zoom currently
  means 0% replayability.
- **Volatile screen fragments frozen as assertions** (fixed 2026-07-09;
  formerly the top availability killer on live apps — a fresh OpenEMR
  recording mined `text_present ':01'`, a clock-minute OCR fragment, and
  every later replay false-halted on it). Mining now selects for
  **stability, not novelty**: clock times (colon and unambiguous
  European dot forms — `18.38`), month-name dates (`Jul 8, 2026`,
  `July 2026` — OpenEMR's post-login calendar header alone would
  false-halt every replay the next month), relative-time phrases
  (`3 min ago`, `just now`, a standalone `Yesterday`), dates near the
  recording date, counts and pagination position (`56 total entries`,
  `1 to 1 of 1`, `Page 2 of 9` — navigation/volume state, not identity),
  parenthesized badge counters (`Inbox (2)`), digit-dominated fragments
  and low-entropy noise are all rejected; candidates must persist across
  the recording's own frames (fading toasts and self-mutating regions are
  volatile by demonstration); ranking prefers alphabetic text near the
  click target over "longest new text". A date FAR from the recording
  date — a DOB in a patient banner, numeric or month-name form — is
  deliberately kept: it is identity data, and the old blanket timestamp
  filter's habit of eating identity banners is gone.
- **State the demonstration accidentally froze** (narrowed 2026-07-09,
  still real). Text that is stable within the recording but specific to
  the instance or dataset — a module menu, a persistent data row — can
  still be mined as an assertion. (Entry counts like "filtered from 56
  total entries" were in this class until the same-day review hardening;
  count phrases now classify as volatile and are rejected at compile
  time.) A bundle recorded on one OpenEMR demo instance halts at login on
  a second instance of the *same version* because the module menu and
  calendar content differ. Per-tenant re-recording is the working
  assumption.
- **Identity bands recorded through modal dialogs** (exposed 2026-07-09
  once the `':01'` halts stopped masking it; FIXED the same day by the
  matcher rework). A click inside a dialog records a context band that
  includes background chrome, and OCR segmentation/order of that chrome
  does not reproduce between reads; the earlier order-sensitive coverage
  matcher scored the permuted re-read at ~0.66 and refused the click
  (observed reproducibly on the OpenEMR note-dialog textarea — control
  replays capped at 14/17, safe halt, nothing written). The token-wise
  order-insensitive matcher scores that same permuted band at 1.0; the
  shape is pinned verified in `tests/test_identity.py`.
- **Viewports smaller than demonstrated.** If the target is below the fold
  and no scroll was demonstrated, there is no recorded gesture to extend —
  the run halts (closed-loop scrolling extends recorded scrolls; it does
  not invent them).
- **Screens that outlast their timeouts.** Renders slower than the
  postcondition window (~5s by default) abort accurately; ~4s delays are
  absorbed.
- **Blocking overlays.** Opaque modals stop the ladder before any click. A
  fully transparent click-interceptor is clicked *into* — the click is
  swallowed harmlessly and the run halts on postconditions. Vision cannot
  tell "the app ignored my click" from "my click never arrived."

## Parameters: exact-value substitution, now identity-gated

Parameterizing the *typed text* of a step works and is verified end to end
(distinct note per run on the live OpenEMR demo, plus per-step typed-input
verification since 2026-07-08). Parameterizing a value that **changes what
appears on screen** — which patient to open — is still position-bound:
anchors recorded on "Belford, Phil" cannot match "Underwood, Susan", so
resolution degrades to geometry, which clicks where the demonstrated row
*was*. Since 2026-07-08 that click is no longer blind, and since
2026-07-09 the check is strict both ways: the identity check's param mode
substitutes the **run's** value into the recorded band and requires the
WHOLE substituted band to match the resolved row — a wrong row halts the
run, and a row that merely *mentions* the run's value (a message about
"Susan" is not Susan's row) halts too. The strictness has a disclosed
availability cost: when the entity's own row text varies with the entity
(a patient search result carries the surname, which the recorded band
baked in as "Belford,"), the substituted band cannot match and the run
halts even on the CORRECT row — re-anchoring only verifies when the
band's non-param residue is stable across entities. Clicking by position
is what caused the wrong-patient writes; we take the halt. Still true and
still costly: making a value a parameter strips it from every compiled
assertion (by design — it varies per run).
The other half of the cost was fixed on 2026-07-09: recorded parameter
values no longer leak into geometry landmarks, and a compile-time lint
fails the build outright if a demonstrated parameter value appears in any
**text** postcondition or in any landmark's OCR text. Scoped precisely:
the lint reads text evidence only — a later step's REGION_STABLE template
can still embed the demo value's rendered *pixels* (e.g. a saved note
visible in a subsequent screen region). That failure is in the false-halt
direction (the region won't match under a different run value and the run
stops safely); it cannot cause a wrong action, but it is not linted (see
known remaining).

## Known remaining (deliberately not attempted in the 2026-07-08/09/10 fixes)

Residual identity verify/abort classes, restored and expanded from the
pre-review disclosure this PR briefly deleted (the old page honestly said
"names within OCR-jitter distance verify"; that class now ABORTS, and
these are what remain):

- **'Ann Marie' vs 'Annmarie' verify as the same patient** — the
  token-join rule (OCR legitimately splits one token into two) is
  raw-equal after concatenation, so two real patients differing only in
  name spacing/hyphen-joining are indistinguishable and verify.
- **Names differing only by case or whitespace verify** — comparison is
  case- and whitespace-insensitive by construction ('MacDonald' vs
  'Macdonald' is the same band).
- **1-2 character letter-letter name confusions verify** — the name
  suspect rule needs 3+ chars, so a middle initial 'I' vs 'L'
  (confusion-equivalent) still passes; a REPLACED initial ('J' vs 'K')
  is caught, an ADDED short token ('Phil M' vs 'Phil J M') is not (the
  unexplained-token budget starts at 3 chars).
- **Short (1-2 char) ALL-ALPHA codes confused with a digit verify** —
  the identifier suspect rule keys on the RECORDED token carrying a
  digit; a 2-char alpha code ('AB') that OCRs to 'A8' has an all-alpha
  recorded token (so the identifier rule does not see it) and is under
  the 3-char name floor. Full-length identifiers — numeric OR alphanumeric
  MRNs, account numbers — are covered (the 9th reopening extended the glyph
  gate to purely-numeric tokens); this residual is only the very short
  all-alpha code.
- **Identifier letter/DIGIT collision is now CAUGHT** (5th-reopening
  fix, second review): a different patient's MRN/account number one
  OCR-confusable char apart ('A01234' vs 'AO1234') aborts, even when the
  name and DOB raw-match. Cost, disclosed next.
- **True-row identifier OCR noise now ABORTS** — the price of the line
  above: when OCR garbles the CORRECT row's own identifier by a
  letter/digit-confusable char, the run halts rather than gamble on
  identity (indistinguishable at band level from a different-patient
  row). Availability cost, the cheap direction. All-DIGIT identifier
  differences (748291 vs 748292) are unaffected — not
  confusion-equivalent, still caught as a genuine mismatch.
- **Identity now rests on NAME + DOB, not on a confusable identifier**
  (7th-reopening fix, `benchmark/dense_surface/DENSE_SURFACE.md`). The
  6th-reopening fix flagged an identifier only when a homoglyph LETTER
  (O/l/I) survived OCR. A real MRN is `<alpha prefix><digit body>`; when
  the confusable glyph is DIGIT-FLANKED, RapidOCR reads the DIGIT form on
  BOTH a patient (`AC50061`) and a DIFFERENT same-name/DOB patient
  (`AC5OO61`, letter O) — both collapse to `AC50061`, NO homoglyph letter
  survives, the letter-only flag misses it, and the sibling verified
  (measured **~87% false accept on the digit-flanked shape**). No string
  flag on the identifier can recover a distinction OCR destroyed at the
  pixel level, and flagging the digit side (any 0/1 in an MRN) would halt
  ~3 of 4 real MRNs. So identity is verified on the OCR-reliable, redundant
  **NAME + DOB**, and a confusable-glyph identifier is CORROBORATION only:
  when a discriminative NAME carries identity (the name-in-band config) a
  confusable-DIGIT MRN does NOT block (a wrong sibling differs in name/DOB
  — the common case; a realistic different-name/DOB corpus with a
  confusable-digit MRN verifies at `click_action` false abort 0/48), and
  when identity would rest SOLELY on a glyph-vulnerable identifier (the
  clicked NAME cell excluded, only DOB + MRN + generic columns left) a
  DIGIT-body MRN HALTS. A homoglyph LETTER stays a HARD halt (affirmative
  OCR ambiguity), so the 6th-reopening closure holds with no regression
  (the real dense-surface study stays 0/360 false accept). Honest cost:
  this does NOT reduce the 6th fix's over-halt — the letter-homoglyph halt
  is kept hard for safety (softening it re-verifies the same-name/DOB
  letter siblings), and closing the digit-flanked hole in the name-excluded
  config raises that study's per-click false abort 18.89%→45.00% (all in
  `click_name`; `click_action` stays 18.89%) — the cheap direction. **What
  is GUARANTEED:** name+DOB-discriminated identity. **What HALTS:** identity
  that would turn on a look-alike-character identifier alone.
- **A same-name/DOB collision with the NAME displayed — now HALTS on
  pixel-only substrates too (8th + 9th reopenings).** A genuinely DIFFERENT
  patient who shares the target's full NAME and DOB, whose collapsible MRN
  OCR-collapses to the target's, and whose name is IN the identity band
  (opening the chart via an Open button rather than the name cell) is
  band-identical to a legitimate same-patient re-read. #27 let name+DOB CARRY
  identity here, so the OCR tier VERIFIED it — the adversarial review of PR #31
  proved that a live wrong-patient VERIFY. It is now CLOSED in every substrate:
  the OCR tier ABSTAINS on ANY collapsible-glyph identifier (numeric or
  alphanumeric, the 9th reopening) REGARDLESS of a matched name+DOB, so on a
  pure-pixel substrate the ladder HALTs rather than verifies. **On browser
  (DOM) and native desktop (UIA/AX) it VERIFIES the correct row and mismatches
  the sibling** via the structured-text tier — the two MRNs are different
  strings in the DOM/a11y tree (0 false accept on the real dense surface,
  digit-flanked and numeric attacks included), with no OCR-availability cost.
  On pure-pixel substrates the roadmapped path to VERIFY (rather than HALT) is
  the pixel/perceptual identifier-crop tier and glyph-disambiguating /
  high-resolution identifier OCR. The cost of the refusal is a higher OCR-path
  over-halt (every same-entity band carrying a collapsible identifier now
  abstains), disclosed in the 8th/9th notes above and the render studies.
- **Indistinguishable-class aborts are permanent** — a true row whose
  name OCR letter-letter-garbles ('Neil' read as 'Nell') aborts every
  time, because the band is textually identical to a real sibling;
  this is the safety direction and it costs availability on noisy rows.
- **Compiled-only users pay ~28% halts on v1-style noisy rows as the
  availability price of the redesigns** (up from ~21% before the
  identifier-suspect fix; 0% on clean splits/bleed, ~49% on the
  digit_confusion_true_row class — see IDENTITY_ROC.md per-class
  tables); hybrid deployments convert each halt into one ~$0.10 fallback
  escalation.
- **The operating point is fit to the frozen corpora that produce the
  headline zero** — freezing prevents tuning the corpus toward the
  matcher, not the matcher's thresholds toward the corpus; every
  zero-claim on this page is scoped to corpus v1+v2+v3 plus the 18
  out-of-corpus reviewer probes.

Other known-remaining items:

- **Cosmetic global drift** (browser zoom, device scale factor, font-size
  preference) still zeroes availability — false abort at the first step.
- **Mining still freezes instance-stable state** (entry counts, module
  menus, persistent data rows — volatile *fragments* are fixed, instance
  *state* is not); per-tenant re-recording remains the working assumption.
- **Vacuous steps with no structural effect** still exist (inert native
  `<select>`; non-structural recording backends) — no minimum-verification
  floor.
- **Unreadable identity bands fall back to the old behavior** (flagged in
  the report, refused only for compile-time-marked irreversible steps) —
  an icon-only repeated structure with no OCRable row text is still
  exposed to wrong-entity clicks.
- **Label-only and too-generic-band targets** (see the dangerous list)
  compile with no identity context at all.
- **Automatic risk classification does not exist** — `risk_overrides` at
  compile time is the only way a step becomes irreversible (see the
  dangerous list for what that means by default).
- **Param targets whose row text varies with the entity** halt on the
  correct row (see the parameters section) — a re-anchoring strategy that
  can verify such rows without falling back to position is future work.
- **REGION_STABLE templates can embed rendered parameter pixels.** The
  parameter-leakage lint scans text postconditions and landmark OCR text
  only; a later step's stable-region crop may contain the demo value as
  pixels. False-halt direction only (safe), but unlinted.
- **Long-line anchors are OCR-segmentation-fragile at the resolution
  rung.** `find_text` fuzzy-matches whole OCR lines with no multi-line
  joining, so a long anchor `ocr_text` the engine re-segments differently
  at replay can miss the OCR rung and degrade resolution to geometry.
  The *postcondition* side of this fragility was fixed on 2026-07-09:
  TEXT_PRESENT/ABSENT checks go through `vision.text_present`, which also
  accepts a contiguous >=0.8-of-target run across the concatenated OCR
  lines (merged-box and split-box re-reads pass — exercised against the
  real engine in `tests/test_vision.py`), so a mined line that OCR
  re-segments at replay no longer false-halts the presence check.
- **Fuzzy text matching cannot see one-digit count differences.** A line
  differing from the recorded one by a single digit scores above the 0.8
  per-line fuzzy threshold. Mitigated by rejecting count-bearing lines at
  compile time (they no longer become assertions); the matcher itself was
  not redesigned.
- **Structural checks pass as unverified on a transient None.** When a
  structural observation (URL/title/page count) reads None on either side
  — even on a backend that normally provides it — the postcondition
  passes honestly-unverified rather than halting.
- **NEW_TAB_OPENED false-halts on named-window reuse.** A link that
  re-targets an existing named window navigates it instead of increasing
  the page count; the mined page-count postcondition then fails a
  successful action (safe direction, costs availability).
- **The persistence check has no coverage on the recording's final step.**
  There is no next-step before-frame to test persistence against, so a
  toast that appears on the last demonstrated action can still be mined
  as an assertion.

## What a demonstration cannot express

Structural limits of the current IR, not bugs:

- **No conditionals, no loops.** A workflow is a linear list of steps. "If
  the search returns two results, pick the newer" cannot be demonstrated
  or replayed; data-dependent pagination ("the target moved to page 2")
  has no recorded step to reach it.
- **One window.** The backend drives a single page. New tabs open
  unobserved; multi-window flows are out of scope.
- **No native browser/OS chrome.** Select popups, date-picker calendars,
  file choosers, print dialogs: invisible to screenshots, unreachable by
  page-coordinate clicks. Keyboard fallbacks work where the widget supports
  them (type-prefix + Enter drives a native `<select>`); native date inputs
  accepted typed digits but produced a wrong value in our harness — the
  replay now safe-halts on such value-transforming widgets (typed-input
  read-back cannot verify the transformed rendering) instead of faithfully
  reproducing the wrong value.
- **No drag-and-drop** (no such action in the IR or recorder).

## PHI handling: what is scrubbed and what is a boundary

openadapt-flow touches PHI (patient names, DOB, MRN) in identity band text,
typed values, screenshots, and the run report. Scrubbing (via the optional
`openadapt-privacy` extra, `pip install 'openadapt-flow[privacy]'`) is wired
into the persist/log paths; the full map is in
[docs/PRIVACY.md](PRIVACY.md). The honest limits:

- **The shareable `REPORT.md` is scrubbed; the machine artifacts are not.**
  Free-text in `REPORT.md` (workflow name, params, intents, errors) passes
  through Presidio NER by default (`OPENADAPT_FLOW_SCRUB=auto`, active when the
  extra is installed). But `workflow.json`, `report.json`, `events.jsonl`, and
  the recording frames keep the **literal** identifiers on purpose — the compiled
  bundle needs the recorded identity evidence to run the wrong-patient check, and
  `report.json` is the identity **audit trail**. These are PHI-at-rest protected
  by filesystem controls and your retention policy, **not** by scrubbing. Do not
  commit real bundles or run dirs to a public repo.
- **Image redaction is opt-in and best-effort.** Persisted screenshots are
  redacted only when `OPENADAPT_FLOW_SCRUB_IMAGES=1`, and Presidio image
  redaction (OCR+NER) can miss non-textual or unusually-laid-out PHI. Off by
  default; treat saved frames as PHI unless you have verified redaction on your
  app.
- **The identity crop sent to the VLM appliance is deliberately NOT scrubbed.**
  It *is* the identifier, so scrubbing it would defeat the same/different check.
  The control is a boundary, not redaction: on-prem-only destination plus
  no-retention (no client/server disk or log writes; the MLX dev backend deletes
  its unavoidable temp files in a `finally`). See
  [docs/deployment/ON_PREM_VLM.md](deployment/ON_PREM_VLM.md#phi-data-flow-boundary).
- **`auto` writes plaintext when the extra is absent.** The default keeps the
  local demo working with no NER model. A clinical deployment must set
  `OPENADAPT_FLOW_SCRUB=on` (fail closed) so a missing capability aborts instead
  of silently writing PHI.

## What held up under attack

For symmetry, verified the hard way: zero crashes across every experiment;
zero model calls and $0 spent; no false success ever occurred without a
wrong physical action first; opaque obstructions, navigation hijacks,
empty states, and slow screens all halted at the right step with the right
reason; mid-run renames and position swaps of *labeled* controls healed
correctly; and the live-app control runs (18 steps, iframes everywhere)
stayed 20/20 compiled and 5/5 re-verified. The postcondition system is a
real safety net — its holes are specific, listed above, and now tested.

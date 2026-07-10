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
  full-consumption token split/join — and the decision holds SIX budgets
  at once: >= 0.8 coverage; no contiguous uncovered run over 4 squashed
  characters; zero *contradicted* characters (near-miss siblings —
  Phil/Philip, John/Joan, an off-by-one DOB or swapped MRN digits, a
  Jr/Sr suffix on one side, a replaced word or a replaced 1-2 char token
  such as a middle initial or the SEX column); zero *suspect* characters
  (a name-plausible token matched only by a LETTER-LETTER confusion —
  Neil/Nell, Clay/Day, Marnie/Mamie — is indistinguishable from a real
  sibling and refuses); zero unexplained observed name-shaped tokens (an
  appended middle name, a second row OCR-merged into the band, a
  message/cc row that merely MENTIONS the recorded patient); and no
  absent name-like alphabetic token of 4+ characters (a band must not
  verify with its identity token never read). The 2026-07-09 matcher's
  containment and 0.7-similarity tiers measured 53.9% false-accept on
  frozen corpus v1; the first rebuild measured 0.0% there but the
  2026-07-10 review showed that zero was partially tautological — v1's
  labeling rule excluded confusion-collided names, short-token
  discriminators, observed supersets and absent-name shapes by
  construction, and 13 out-of-corpus probes in those classes all
  silently VERIFIED. The redesigned matcher measures **0.000% false
  accepts on corpus v1+v2 plus the 13-probe set** — scoped exactly to
  those corpora, not to the world, and the operating point was fit on
  the same corpora that produce the headline (docs/validation/
  IDENTITY_ROC.md states this bias plainly). The availability bill is
  equally plain: **21.2% false aborts on v1's noise classes** (up from
  10.7% pre-review; 0% on v2's legitimate-noise classes), concentrated
  in occlusion — where a recount showed ~half the aborted bands still
  had BOTH name tokens readable and aborted on trailing DOB/MRN loss,
  an availability cost, not the "correct epistemic refusal" the earlier
  doc claimed — plus letter-letter confusion noise and capitalized
  adjacent-row bleed. For a parameterized target (e.g. *which patient*
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
- **1-2 character letter-letter confusions verify** — the suspect rule
  needs 3+ chars, so a middle initial 'I' vs 'L' (confusion-equivalent)
  still passes; a REPLACED initial ('J' vs 'K') is caught, an ADDED
  short token ('Phil M' vs 'Phil J M') is not (the unexplained-token
  budget starts at 3 chars).
- **Indistinguishable-class aborts are permanent** — a true row whose
  name OCR letter-letter-garbles ('Neil' read as 'Nell') aborts every
  time, because the band is textually identical to a real sibling;
  this is the safety direction and it costs availability on noisy rows.
- **Compiled-only users pay ~21% halts on v1-style noisy rows as the
  availability price of the redesign** (0% on clean digit-class noise,
  splits and bleed — see IDENTITY_ROC.md per-class tables); hybrid
  deployments convert each halt into one ~$0.10 fallback escalation.
- **The operating point is fit to the frozen corpora that produce the
  headline zero** — freezing prevents tuning the corpus toward the
  matcher, not the matcher's thresholds toward the corpus; every
  zero-claim on this page is scoped to corpus v1+v2 plus the 13
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

## What held up under attack

For symmetry, verified the hard way: zero crashes across every experiment;
zero model calls and $0 spent; no false success ever occurred without a
wrong physical action first; opaque obstructions, navigation hijacks,
empty states, and slow screens all halted at the right step with the right
reason; mid-run renames and position swaps of *labeled* controls healed
correctly; and the live-app control runs (18 steps, iframes everywhere)
stayed 20/20 compiled and 5/5 re-verified. The postcondition system is a
real safety net — its holes are specific, listed above, and now tested.

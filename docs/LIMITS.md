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
success. They are open problems, not caveats. (Two former members of this
list — wrong-entity clicks in repeated structures, and unverified typed
input — were fixed on 2026-07-08 and moved to the safe-halt section below.)

- **Steps that changed nothing assert nothing.** An action whose recorded
  before/after frames are identical (a click that opens a new tab the
  runtime can't see, an inert widget) compiles with zero postconditions
  and can never fail. There is currently no minimum-verification floor.
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
  matcher hardened 2026-07-09 after adversarial review; formerly the top
  silent failure mode). When data shifts between runs — a row added above
  the target, the target's row deleted, a look-alike sibling, a re-sorted
  table — the resolver still finds a pixel-identical target at a
  plausible position, but the pre-click **identity check** compares the
  resolved row's text (the OCR lines of the resolved point's own text
  row, minus the target's own label and timestamp-bearing cells) against
  the recorded row and refuses to click on mismatch. Matching is
  order-insensitive per token (OCR re-reads the same band in different
  segmentation orders) and requires BOTH >= 0.8 coverage of the recorded
  band AND no contiguous uncovered run longer than 4 squashed characters
  — a wrong name is a contiguous mismatch, so long shared row text cannot
  buy it a pass. For a parameterized target (e.g. *which patient* to
  open), the run's value is substituted into the recorded band and the
  whole substituted band must match — a row that merely mentions the
  run's value does not verify. Caveats, disclosed: when the live band is
  unreadable even at 2x resolution, reversible steps proceed exactly as
  before with the step flagged in the run report (`identity:
  "unreadable"`), and only compile-time-marked irreversible steps refuse
  (see the dangerous list); dense-table OCR undercount is real, which is
  why the 2x retry exists; names within OCR-jitter distance of each other
  (whole-token similarity >= 0.7, e.g. "Jane"/"Janet") are
  indistinguishable from misreads and verify.
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
- **State the demonstration accidentally froze.** Postconditions are mined
  from what the demo changed on screen, and they routinely capture data —
  a neighbouring table row's text, another user's message fragment. A
  bundle recorded on one OpenEMR demo instance halts at login on a second
  instance of the *same version* because the module menu and calendar
  content differ. Per-tenant re-recording is the working assumption.
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
assertion (by design — it varies per run), and recorded parameter values
leak into geometry landmarks, quietly degrading healing for any run whose
values differ from the demo (i.e., all of them).

## Known remaining (deliberately not attempted in the 2026-07-08/09 fixes)

- **Cosmetic global drift** (browser zoom, device scale factor, font-size
  preference) still zeroes availability — false abort at the first step.
- **Postcondition mining still overfits** to demonstrated/instance state
  (data rows, other users' content); per-tenant re-recording remains the
  working assumption.
- **Vacuous zero-postcondition steps** still exist (no
  minimum-verification floor or compile-time warning).
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

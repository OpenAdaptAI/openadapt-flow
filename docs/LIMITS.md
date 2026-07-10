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
2026-07-09. Both moved to the safe-halt section below.)

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
  context and is still clicked by position, unverified.

## What it halts on (safely, but it halts)

Failures below stop the run with an accurate per-step report — no wrong
actions observed — at the cost of availability:

- **Wrong-entity targets in repeated structures** (fixed 2026-07-08;
  formerly the top silent failure mode). When data shifts between runs —
  a row added above the target, the target's row deleted, a look-alike
  sibling, a re-sorted table — the resolver still finds a pixel-identical
  target at a plausible position, but the pre-click **identity check**
  compares the resolved row's text (full-width OCR band, minus the
  target's own label and timestamp-bearing cells) against the recorded
  row and refuses to click on mismatch. For a parameterized target (e.g.
  *which patient* to open), the live band must name the **run's**
  parameter value instead. Caveats, disclosed: when the live band is
  unreadable even at 2x resolution, reversible steps proceed exactly as
  before with the step flagged in the run report (`identity:
  "unreadable"`), and irreversible steps refuse; dense-table OCR
  undercount is real, which is why the 2x retry exists.
- **Typed input that cannot be confirmed** (fixed 2026-07-08). After every
  TYPE action the field region is screenshot-diffed and (where legible)
  OCRed for the typed value; if nothing landed — e.g. focus stolen by a
  late re-render, keystrokes falling on `<body>` — the replayer re-clicks
  the field, selects-all, retypes once, and halts if the input still
  cannot be confirmed. In the focus-theft reproduction the retry recovers
  and the run completes with the correct text. Caveat: the diff layer
  detects "keystrokes rendered nothing", not "keystrokes rendered in the
  wrong visible field"; the OCR layer covers legible values, masked
  (password) values rely on the diff alone.
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
- **Identity bands recorded through modal dialogs** (pre-existing, exposed
  2026-07-09 once the `':01'` halts stopped masking it). A click inside a
  dialog records a context band that includes background chrome; OCR
  segmentation/order of that chrome does not reproduce between reads, and
  the order-sensitive coverage matcher then refuses the click. Observed
  reproducibly on the OpenEMR note-dialog textarea (control replays cap at
  14/17 — safe halt, nothing written).
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
*was*. Since 2026-07-08 that click is no longer blind: the identity
check's param mode requires the **run's** value to appear in the resolved
row's text before acting — a wrong row halts the run instead of opening
the wrong chart. Still true and still costly: making a value a parameter
strips it from every compiled assertion (by design — it varies per run).
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

## Known remaining (deliberately not attempted in the 2026-07-08/09 fixes)

- **Cosmetic global drift** (browser zoom, device scale factor, font-size
  preference) still zeroes availability — false abort at the first step.
- **Mining still freezes instance-stable state** (entry counts, module
  menus, persistent data rows — volatile *fragments* are fixed, instance
  *state* is not); per-tenant re-recording remains the working assumption.
- **Vacuous steps with no structural effect** still exist (inert native
  `<select>`; non-structural recording backends) — no minimum-verification
  floor.
- **Identity-band order fragility on dialog clicks** (exposed 2026-07-09;
  see the safe-halt list) — fixing it means re-validating the coverage
  matcher's measured look-alike margins, not a quick patch.
- **Unreadable identity bands fall back to the old behavior** (flagged in
  the report, refused only for irreversible steps) — an icon-only repeated
  structure with no OCRable row text is still exposed to wrong-entity
  clicks.
- **Label-only targets** (see the dangerous list) compile with no identity
  context at all.
- **REGION_STABLE templates can embed rendered parameter pixels.** The
  parameter-leakage lint scans text postconditions and landmark OCR text
  only; a later step's stable-region crop may contain the demo value as
  pixels. False-halt direction only (safe), but unlinted.
- **Long-line anchors are OCR-segmentation-fragile.** `find_text` does no
  multi-line joining, and candidate ranking prefers long lines — the same
  mechanism behind the disclosed step_014 band failure. A mined long line
  that OCR re-segments differently at replay false-halts.
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
  accepted typed digits but produced a wrong value in our harness — and
  replay faithfully reproduced the wrong value.
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

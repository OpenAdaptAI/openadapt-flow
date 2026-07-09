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
*was*. Since 2026-07-08 that click is no longer blind: the identity
check's param mode requires the **run's** value to appear in the resolved
row's text before acting — a wrong row halts the run instead of opening
the wrong chart. Still true and still costly: making a value a parameter
strips it from every compiled assertion (by design — it varies per run),
and recorded parameter values leak into geometry landmarks, quietly
degrading healing for any run whose values differ from the demo (i.e.,
all of them).

## Known remaining (deliberately not attempted in the 2026-07-08 fix)

- **Cosmetic global drift** (browser zoom, device scale factor, font-size
  preference) still zeroes availability — false abort at the first step.
- **Postcondition mining still overfits** to demonstrated/instance state
  (data rows, other users' content); per-tenant re-recording remains the
  working assumption.
- **Vacuous zero-postcondition steps** still exist (no
  minimum-verification floor or compile-time warning).
- **Unreadable identity bands fall back to the old behavior** (flagged in
  the report, refused only for irreversible steps) — an icon-only repeated
  structure with no OCRable row text is still exposed to wrong-entity
  clicks.
- **Label-only targets** (see the dangerous list) compile with no identity
  context at all.

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

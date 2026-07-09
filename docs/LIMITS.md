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
success. They are open problems, not caveats.

- **Look-alike targets in repeated structures.** When the demonstrated
  target is one row/card/icon among visually identical siblings and the
  data shifts between runs — a row added above, the target's row deleted —
  the replay can act on the sibling that now occupies the demonstrated
  position. In our MockMed clinic app this saved an encounter to the
  **wrong patient** and reported success, in four separate reproductions.
  The discriminative text (the patient's name) sits outside the template
  crop, and the compiled postconditions happened to assert only
  patient-agnostic text. Match confidence was ~1.0 precisely because the
  imposter's pixels were identical: **confidence measures pixel similarity,
  not identity.**
- **Typed input is never verified.** If focus is lost between the focusing
  click and the keystrokes (a late re-render, a stray dialog), the text
  lands nowhere, and the run completes green — we saved an encounter with
  an empty note. Parameterized values are deliberately excluded from every
  assertion (they vary per run), which means the values that matter most
  are checked least.
- **Steps that changed nothing assert nothing.** An action whose recorded
  before/after frames are identical (a click that opens a new tab the
  runtime can't see, an inert widget) compiles with zero postconditions
  and can never fail. There is currently no minimum-verification floor.

## What it halts on (safely, but it halts)

Failures below stop the run with an accurate per-step report — no wrong
actions observed — at the cost of availability:

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

## Parameters: exact-value substitution only

Parameterizing the *typed text* of a step works and is verified end to end
(distinct note per run on the live OpenEMR demo). Parameterizing a value
that **changes what appears on screen** — which patient to open — is
position-bound and unverified: anchors recorded on "Belford, Phil" cannot
match "Underwood, Susan", so resolution degrades to geometry, which clicks
where the demonstrated row *was*. With a unique search match that happens
to be right; with several matches nothing checks which row was clicked.
Worse, making a value a parameter strips it from every assertion — the
compiler deleted `Patient Messages for Belford, Phil`, the strongest
identity check in the bundle, the moment the patient became a parameter.
Recorded parameter values also leak into geometry landmarks, quietly
degrading healing for any run whose values differ from the demo (i.e., all
of them).

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

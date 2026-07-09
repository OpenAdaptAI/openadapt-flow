# Adversarial validation — failure-mode matrix

Date: 2026-07-08. This document is the result of deliberately trying to
break compiled replay before anyone else does. Every experiment ran with
**zero model calls and $0 of API spend**: compiled-replay only, no grounder,
no agent arm. Failures found here are the point of the exercise; nothing is
softened.

## Outcome vocabulary

- **pass** — the run succeeded and did what the demonstration did.
- **safe-halt** — the run stopped with an accurate report and took no
  further actions. A *false abort* is a safe-halt whose cause was cosmetic
  (the task could have continued); it costs availability, not safety.
- **wrong-action** — the run executed an action on the wrong target or
  wrote incorrect state. *Silent* wrong-actions additionally reported
  success. **This is the critical class.**
- **crash** — an unhandled exception. (None occurred anywhere.)

## Headline numbers

- **Wrong-actions: 6**, of which **5 were silent** (wrong state written
  AND the run reported success):
  1. `drift=lookalike` — encounter saved to the look-alike patient (MockMed).
  2. `drift=missing` — target gone; encounter saved to the neighbouring
     patient.
  3. `drift=grow` — encounter saved to an unrelated patient whose row
     landed at the recorded position.
  4. chaos `delete-target-row` — row deleted mid-run; encounter saved to
     the patient that slid into its place.
  5. chaos `steal-focus` — focus lost between click and type; encounter
     saved with an **empty note**, reported success.
  6. (caught, not silent) `sort-reorder` — clicked the wrong table row and
     wrote its pick into app state before the postcondition halted the run.
- **Safe-halt rate on the remaining perturbations/faults: 100%** — every
  non-silent failure halted with an accurate report; postconditions never
  produced a false success outside the wrong-action cases above; there were
  **zero crashes** across all tracks.
- OpenEMR (live, public demo): 0 wrong-actions in 4 replays; the patient
  parameterization and cross-instance runs both ended in safe-halts whose
  mechanisms are findings in their own right (below).

All MockMed experiments are automated as characterization tests —
`tests/e2e/test_perturbation.py`, `tests/e2e/test_chaos.py`,
`tests/e2e/test_primitives.py` — so the failure modes are pinned: if one of
those tests ever fails, a documented failure mode changed (was fixed, or got
worse) and this document must be updated. OpenEMR experiments are driven by
`scripts/openemr_param_depth.py` (paced, fake patients only, not in CI).

Evidence pointers below refer to local, gitignored artifacts under
`runs/validation/` (full per-step screenshots, reports, healed bundles).
The pytest suites regenerate equivalent artifacts under their run dirs.

## Track A — perturbation/drift matrix (MockMed)

One recording (1280x800, default theme), replayed under one perturbation at
a time. Automated in `tests/e2e/test_perturbation.py`.

| perturbation | outcome | detail |
|---|---|---|
| baseline | pass | 8/8 anchors on the `template` rung, 0 heals |
| viewport 1440x900 | pass | layout is left-anchored; nothing moved |
| viewport 1024x768 | pass | same |
| viewport 900x360 (target below fold) | safe-halt (false abort) | halted at the note-field step: its REGION_STABLE region extends past the short viewport. The save button was off-screen and unreachable anyway — no scroll was demonstrated, and closed-loop scrolling only extends *recorded* scroll steps |
| device scale factor 2 | safe-halt (false abort) | halts at step_000: screenshots are 2x the coordinate space; template scale ladder tops out at 1.18x; OCR returns frame-pixel coordinates that no longer equal input pixels |
| CSS zoom 125% (`drift=zoom`) | safe-halt (false abort) | halts at step_000 for the same family of reasons |
| font size 16px→19px (`drift=font`) | safe-halt (false abort) | halts at step_000: REGION_STABLE phash cannot tolerate reflowed glyph metrics. Theme drift heals (README showcase); font drift yields 0% replayability |
| data growth (`drift=grow`, 4 rows added above target) | **wrong-action, silent** | local template matched the imposter row at the recorded position (≥0.985 despite different reason/priority text in-crop); encounter saved to `#patient/g1`, run reported success. Evidence: `runs/validation/track-a/run-grow/` |
| look-alike row (`drift=lookalike`) | **wrong-action, silent** | a row with the same reason/priority directly above the target is pixel-identical inside the 160x64 crop (the NAME column is outside it); template rung confidence 1.0; saved to `#patient/p0`. Evidence: `runs/validation/track-a/run-lookalike/` |
| target row deleted (`drift=missing`) | **wrong-action, silent** | desired: safe-halt, never click a look-alike. Observed: the neighbouring row occupies the recorded position and every rung that fires resolves to it; saved to `#patient/p2`. Evidence: `runs/validation/track-a/run-missing/` |
| empty list (`drift=empty`) | safe-halt | halts one step early: the sign-in step's postcondition asserts another data row's text (`Cardiology follow-up`) as an "invariant" — safe here, but the mechanism (mining mutable DATA as postconditions) is what makes the three rows above silent |
| slow renders 4s (`drift=slow`) | pass | postcondition polling + ladder retry absorb it (~20s run) |
| slow renders 12s (`drift=slow&slowms=12000`) | safe-halt | accurate report at the sign-in step (~5.5s postcondition window exceeded) |

Why the silent wrong-patient saves get through, end to end:

1. The discriminative evidence for a table-row button (the patient name)
   sits **outside** the template crop, and a strict-looking 0.985 template
   threshold does not separate "same button, different row" (in-crop text
   differences moved the score by less than 1.5%).
2. The compiler's timestamp filter — added for OpenEMR, correctly — drops
   the patient banner from the click step's postconditions because the DOB
   ("1980-01-01") matches a date pattern. The surviving "new text"
   postcondition is the patient-agnostic `No encounters yet.`.
3. Nothing downstream re-verifies identity, so the wrong chart passes every
   subsequent check and the save lands.

## Track B — mid-run fault injection (MockMed)

State sabotaged *between* steps of a live replay via a wrapping backend.
Automated in `tests/e2e/test_chaos.py`.

| fault (injection point) | outcome | detail |
|---|---|---|
| target row deleted after sign-in | **wrong-action, silent** | mid-run twin of `drift=missing`: saved to `#patient/p2`, reported success. Evidence: `runs/validation/track-b/run-delete-target-row/` |
| opaque modal before save | safe-halt | all rungs fail; ladder retries to the step timeout; aborts naming the save step; **no click fired into the overlay** |
| invisible click-shield before save | safe-halt | vision sees an unchanged screen, resolves correctly, clicks into the shield; nothing happens; postconditions abort. One neutralized click; no state written |
| Triage/Consult buttons swapped | pass (healed) | labels differ, so lower rungs re-locate the true target; the saved encounter has the correct type. Identical-looking swaps are the look-alike case above — those go wrong |
| focus stolen between click and type | **wrong-action, silent** | keystrokes fall on `<body>`; the encounter is saved with an **empty note** and the run reports success. TYPE steps never verify field content, and parameterized values are *by design* excluded from every postcondition, so no check can notice. Evidence: `runs/validation/track-b/run-steal-focus-before-type/` |
| app navigates away before save | safe-halt | postconditions fail on the wrong screen; nothing typed or saved |
| save button renamed mid-run | pass (healed) | geometry rung resolves via unchanged landmarks; anchor healed; saved correctly |

## Track C — interaction-primitive taxonomy

One tiny demo per primitive against
`openadapt_flow/mockmed/static/widgets.html` (one control per `?panel=`).
Automated in `tests/e2e/test_primitives.py`. Exploration evidence:
`runs/validation/track-c/summary.json`.

| primitive | verdict | evidence (one line) |
|---|---|---|
| button / link click | supported | entire MockMed + OpenEMR corpus |
| checkbox / radio | supported | replay reproduces `Consent yes, priority Urgent.` |
| DOM modal dialog (open/confirm) | supported | replay reproduces `Survey response recorded.` |
| typeahead, fixed value | supported | type prefix + click suggestion replays exactly |
| typeahead, **parameterized** value | **partial / hazard** | recorded suggestion anchor can't match; geometry clicks whatever sits at the first-suggestion **position** — correct here by coincidence, unverified by construction (the status text embeds the parameter, so the compiler excluded it from postconditions) |
| table pagination (as demonstrated) | supported | Next → pick on page 2 replays |
| sorting that reorders targets | **unsupported (wrong-action, caught)** | replay vs `?presort=desc` clicked the wrong row and wrote its pick into app state before the postcondition halted; identical row buttons defeat template discrimination |
| keyboard-only flow (Tab/type/Enter) | supported | `Request submitted for Rivera on ward North.` replays |
| native `<select>`, mouse | unsupported | the dropdown popup is browser chrome — it never appears in page screenshots (predicted in FINDINGS.md, confirmed) |
| native `<select>`, arrow keys | **hazard** | inert in this harness (macOS headless): recording changed nothing, steps compiled with **zero postconditions**, replay was a **vacuous success** |
| native `<select>`, type-prefix + Enter | supported (workaround) | `Species set to Dog.` replays — the keyboard fallback FINDINGS.md predicted |
| native date input, typed digits | **partial / hazard** | typing `07082026` produced value `70820-02-06` *at record time*; replay reproduced the same garbage byte-for-byte. Faithful replay of a bad recording is still bad data; the calendar popup is invisible browser chrome |
| iframe-heavy pages | supported | OpenEMR: 6+ nested iframes, modal-in-iframe-in-modal — vision-only replay never noticed (docs/showcase-openemr/FINDINGS.md) |
| new tab / `target=_blank` | **unsupported / silent** | the single-page backend never sees the new tab; before/after frames identical → **zero postconditions** → vacuous success with the side effect entirely unobserved |
| file upload | unsupported (structural) | the native file chooser is OS chrome; neither recordable nor drivable vision-only |
| drag-and-drop | unsupported (structural) | no DRAG in `ir.ActionKind`; the recorder has no drag API |
| conditional branching / loops | unsupported (structural) | `Workflow.steps` is a linear list; a demonstration cannot express if/else, and replay cannot skip or repeat steps |
| multi-window coordination | unsupported (structural) | `Backend` wraps exactly one page |

## Track D — parameterization depth (OpenEMR, live public demo)

Driver: `scripts/openemr_param_depth.py` (fresh browser per run, ≥30s
pacing, fake demo patients only; 8 demo sessions total including probes).
Question: the shipped benchmark parameterizes the note TEXT; what happens
when the PATIENT — a value that changes screen content — is a parameter?

| run | outcome | detail |
|---|---|---|
| `patient=Phil` (control) | pass | 18/18, 39.1s, 0 model calls, note verified on final screen |
| `patient=Susan` | safe-halt (false abort) | typed "Susan", results showed only "Underwood, Susan Ardmore"; the row click fell to the **geometry rung** (landmarks = column headers) and clicked the demonstrated **position** — which was Susan, the right patient. The chart opened, then the run aborted: step_008 asserts `No treatment intervention preferences recorded.`, a **Phil-dashboard state** baked in as an invariant. Evidence: `runs/validation/track-d/runs/run-susan-drift/` |
| cross-instance `/a/` | environment | `/a/` rejected its own published admin credentials that day (verified with DOM selectors — not a replay artifact). The replay executed the login correctly and safe-halted with an accurate report |
| cross-instance `/b/` | safe-halt (false abort) | login **succeeded**, but the run halted at the login step's postconditions: `/b/` runs a different module set (no "Inventory" menu entry) and different calendar content, and both the menu-text assertion and the calendar REGION_STABLE were recorded on the main instance. Same version, different instance state → no transfer. Evidence: `runs/validation/track-d/runs/run-cross-instance-b/steps/step_004_after.png` |

Cross-VERSION drift was not testable: all public demo instances run
OpenEMR 8.0.0. Said plainly and skipped.

**The parameterization-depth answer:** a parameter that changes screen
content is *position-bound and unverified*. Three interlocking mechanisms:

1. **Anchors bind to the demonstrated value's pixels.** "Susan" cannot
   match a template/OCR anchor recorded on "Belford, Phil"; resolution
   degrades to geometry, which encodes *where* the demonstrated row was,
   not *which* row it is. With a single search result that is coincidentally
   correct; with multiple results it is the MockMed wrong-patient scenario
   on a real EMR — nothing verifies the identity of the clicked row.
2. **Parameterizing a value deletes its safety net.** The compiler
   (correctly) refuses to assert parameterized values — but that also
   stripped `Patient Messages for Belford, Phil` from the bundle the moment
   the patient became a parameter. The strongest identity check vanished
   *because* the value became variable.
3. **Assertions overfit to demonstrated state.** What remains asserts the
   demonstrated patient's dashboard (`No treatment intervention preferences
   recorded.`), another demo user's message fragment (`cleanly, no
   drainage.`), and a stray `:01` that slipped past the timestamp filter —
   all mutable, none identity-bearing. The same overfitting blocked the
   cross-instance replay at the login screen.

Also observed on the real app: the save step's geometry landmark is the
**recorded note text itself** — parameterized values leak into landmark
evidence, so healing quality silently degrades for any run whose parameter
differs from the demonstration (i.e., all of them).

## Failure modes ranked by severity

**P0 — silent wrong-state writes (5 reproductions).** Row-level data drift
(grow/look-alike/delete) redirects the whole tail of a workflow to the
wrong entity, and focus loss silently drops typed input; all four
mechanisms end in a green report. Confidence was highest (template rung,
~1.0) precisely when the click was wrongest, and the irreversible-step risk
gate never engages because it keys on *resolution rung*, not on *target
identity* — and is opt-in per step besides. Root causes: crop-local
template evidence excludes the discriminative text; postcondition mining
selects patient-agnostic or mutable text (the timestamp filter eats
identity banners containing DOBs); typed input is never verified.

**P1 — parameterization is position-bound and self-disarming (Track D).**
Changing a content-bearing parameter removes its own assertions and falls
back to position. Works for unique-match lookups; unverified everywhere.

**P2 — cosmetic global drift zeroes availability.** Font +3px, 125% zoom,
or dsf 2 → false abort at step 000. Healing covers theme/move/rename but
nothing that rescales or reflows. Multi-scale matching stops at 1.18x, and
REGION_STABLE phashes break on reflow.

**P2 — assertions overfit to instance/day state.** Bundles do not transfer
between two same-version OpenEMR instances; "longest new text" postconditions
routinely capture data rows, other users' content, and near-timestamps.

**P3 — vacuous successes.** Steps whose action changed nothing on screen
compile with zero postconditions and can never fail (new-tab click, inert
select). There is no minimum-verification floor or compile-time warning.

**P3 — verification blind spots inherent to pixels.** The invisible
click-shield shows the runtime cannot distinguish "app ignored the click"
from "click never reached the app"; native browser chrome (select popups,
date pickers, file choosers) is invisible to screenshots.

## What held up

Reported with equal honesty:

- **No crashes, anywhere.** Every failure was a reported, per-step halt.
- **No false success without a wrong action first.** When the replay acted
  on the *right* targets, postconditions never green-lit a failed outcome.
- The halt machinery is sound when evidence dies loudly: opaque overlays,
  navigation hijacks, empty states, and 12s stalls all stopped the run at
  the right step with an accurate, named reason.
- Healing works as advertised for *labeled* drift: mid-run swaps and
  renames of text-bearing controls resolved to the correct targets.
- Timeout envelope behaved as designed (4s recovers; 12s halts).
- On the live OpenEMR demo, the compiled arm remains 0-model-call and
  passed its control run; the Susan run's wrong-free halt shows the
  postcondition system catching real cross-patient drift when an
  identity-bearing assertion happens to survive compilation.

## Reproduce

```bash
# MockMed tracks (A/B/C) — hermetic, run in CI:
pytest tests/e2e/test_perturbation.py tests/e2e/test_chaos.py \
       tests/e2e/test_primitives.py -q

# OpenEMR track D — live public demo, paced, fake patients only, $0:
.venv/bin/python scripts/openemr_param_depth.py record
.venv/bin/python scripts/openemr_param_depth.py replay
.venv/bin/python scripts/openemr_param_depth.py cross-instance
```

No `ANTHROPIC_API_KEY` is read by any of the above; the grounder rung is
disabled throughout.

# RFC — Workflow-Program IR: control flow, induction, capability-adaptive compilation

**Status:** Draft (design only — no code changes in this PR)
**Scope:** the biggest architectural change on the openadapt-flow roadmap:
evolving the compiled artifact from a *linear action list* into a
*parameterized workflow program* (a state machine with typed parameters,
guards, loops, branches, effects, and exception handlers).
**Audience:** integrator + maintainers deciding whether/when to build this,
and in what order.
**Non-goals of this PR:** shipping code, changing the frozen v0 IR
(`DESIGN.md:13-15`), or calling any model API.

---

## 0. TL;DR

Today a compiled bundle is a **trajectory**: `Workflow` is a flat
`list[Step]` (`openadapt_flow/ir.py:206-217`), each `Step` a single
CLICK/DOUBLE_CLICK/TYPE/KEY/WAIT/SCROLL (`ir.py:31-37`, `ir.py:167-203`)
carrying redundant *evidence* about one recorded moment (the `Anchor`,
`ir.py:64-131`) and assertions about the *pixels that followed* (the
`Postcondition`, `ir.py:150-164`). `compile_recording` turns **exactly one**
recording into **exactly one** straight line of steps
(`compiler/compile.py:716-1082`). `docs/LIMITS.md:686-703` states the
consequence plainly under *"What a demonstration cannot express"*: **no
conditionals, no loops, one window** — *"A workflow is a linear list of
steps."*

That is the correct v0. It is also the ceiling. Two external reviews and
30 years of programming-by-demonstration (PBD) research converge on the same
next step: **treat a demonstration as evidence, not specification, and
compile it toward a *program* with control flow** — the artifact PBD has
always been trying to recover.

This RFC proposes, as a phased and reversible migration:

1. A **Workflow-Program IR** — a parameterized state machine (states,
   guarded transitions, actions, pre/postcondition *effects*, typed
   parameters, loops over worklists, branches, subflows, `wait_until`
   predicates, exception handlers, human-approval nodes, and per-step risk +
   compensation). **Today's linear `Workflow` is the degenerate case** (one
   entry state, unconditional fall-through, no loops) — so the migration is
   backward-compatible.
2. An **induction loop**: bootstrap one interpretation from one demo →
   enumerate candidate generalizations → resolve ambiguity by *asking the
   operator concrete multiple-choice questions* → fold in additional traces
   → infer the shared control-flow graph → validate on held-out traces +
   synthetic perturbations → **quarantine (refuse to emit) when intent stays
   underdetermined**.
3. **Capability-adaptive compilation**: one semantic transition compiles to
   backend-specific implementations (API / DOM-UIA / vision-RDP), each
   satisfying the *same contract* — separating *what* a step means from
   *how* the current app permits it.
4. A **tiered runtime**: deterministic fast path → bounded model recovery of
   **one local transition** → durable pause/approve/resume from the last
   verified checkpoint. Explicitly **not** "hand the rest of the workflow to
   a free-form agent after a halt."

The through-line: openadapt-flow already spent enormous effort making a
*single trace* safe (the identity ladder, volatility mining, postconditions;
see `docs/LIMITS.md`). That work is necessary but insufficient, because the
trace itself under-specifies intent. The next unit of value is recovering the
**intended program**.

---

## 1. Motivation, grounded: one demonstration is EVIDENCE, not SPECIFICATION

A recording is a sequence of observed input events plus before/after frames
(`DESIGN.md:37-50`). The compiler already knows a demonstration
*under-determines* what the operator meant — most of its 1000+ lines exist to
guess which observed facts are invariant and which are incidental:

- **Which literal values are parameters vs. incidental.** The recorder tags
  a typed value as a parameter only if the demo author declared it
  (`meta.json` `params`, `DESIGN.md:43-47`); the compiler then works hard to
  *strip* those values from every assertion so they don't ossify into the
  bundle (`compiler/compile.py:786-791`, the `exclude_texts` machinery, and
  the `lint_param_leakage` build-failure gate, `compile.py:617-661`). But
  the *inverse* problem — *"was this un-tagged value actually a parameter the
  author forgot to mark?"* — is not addressed at all. The demo shows `note =
  "Follow-up in 2 weeks"`; nothing in one trace says whether the *patient*,
  the *encounter type*, or the *priority filter* were meant to be fixed or
  free. `docs/LIMITS.md:495-516` documents the downstream pain: a
  parameterized *patient* degrades to position-clicking because the anchor
  was recorded on one entity.

- **What is incidental.** Volatility mining (`compiler/compile.py:98-121`,
  `openadapt_flow/volatility.py`) is an entire subsystem devoted to deciding
  which *observed screen text* is incidental (clocks, counters, near-dates)
  vs. invariant (a DOB) — because one frame cannot tell you. It is a
  heuristic proxy for a question only multiple traces or the operator can
  answer.

- **Absent-result handling.** A single successful trace never shows the
  *failure* branch. `docs/LIMITS.md:36-43` ("steps with no visual and no
  structural effect assert nothing") and the transactional-fault study
  (`LIMITS.md:64-76`: 5 of 7 write-fault classes silently mishandled) are
  both symptoms of the same gap — the demo shows what *did* happen, not what
  *should* happen when the result is absent or wrong.

- **Loops over worklists.** `docs/LIMITS.md:689-691`: *"If the search
  returns two results… data-dependent pagination has no recorded step to
  reach it."* A demo of clearing **one** referral task from a queue of
  fifteen does not, by itself, say "repeat for every open task." The
  worklist iteration is invisible in a straight-line trace.

- **Essential vs. incidental ordering.** The trace imposes a *total* order;
  the intent is usually a *partial* order (fill three independent fields in
  any order, but Save last). Nothing in the IR distinguishes them.

- **Branches and expected-vs-exceptional popups.** MockMed's `modal` drift
  (`DESIGN.md:229-230`) is treated purely as *semantic drift → halt*
  (`DESIGN.md:200-202`). But a survey modal that *sometimes* appears after
  Save is not drift — it is a **conditional branch** the operator handles
  ("dismiss it and continue"). One trace cannot distinguish "this popup is an
  error" from "this popup is a normal-but-optional step."

### The lineage: recover the program, not the trajectory

This is not a novel observation; it is the founding premise of
programming-by-demonstration, and the field's arc is precisely
*trajectory → program*:

- **Straight-line record-and-replay (Ringer et al.).** Robust low-level
  replay of a single recorded trace — exactly openadapt-flow's current
  altitude (resolve the recorded target on a drifted page, replay the
  recorded action). Crucially, in the web-automation lineage Ringer-style
  straight-line replay was **the input to a generalizer, not the end
  product.**

- **Rousillon / Helena (Chasins et al.).** Took straight-line replay and
  **generalized it into loops over relational data** — the operator
  demonstrates scraping one row; the system induces "for every row, for
  every page." The demonstration is evidence; the *relation* (the worklist)
  is inferred.

- **WebRobot (PLDI 2022).** Synthesizes **loopy programs** from a demonstrated
  action sequence via speculative program synthesis — it *proposes* a loop
  body consistent with the prefix and validates it against continued
  demonstration. Directly relevant: the target artifact contains loops the
  demo only *sampled*.

- **PROLEX.** Recovered the operator's **intended program from a single
  demonstration in 81% of solved cases** — strong evidence that
  one-demo→program is tractable *when paired with disambiguation*, which is
  exactly the induction loop in §3.

- **Skill-DisCo.** Compiles **parameterized finite-state-machine subgraphs
  from multiple traces**, with **typed parameters, pre/postconditions, and
  side-effects** — essentially the target IR of §2, learned from a small set
  of traces rather than one.

- **Agent Workflow Memory (AWM) / Agent Skill Induction (ASI).** Induce
  **reusable, branching skills** from experience and reuse them as
  higher-level actions — the "subflows / reusable components" of §2, and the
  justification for emitting skills/MCP tools (`emit/skill.py`,
  `emit/mcp_tool.py`) as *parameterized programs* rather than fixed replays.

**The target artifact is a program, not a trajectory.** openadapt-flow has
built an unusually strong *straight-line replayer with a safety net*. The
research consensus is that the straight line is step one; the value is in the
generalizer that turns it into a program.

---

## 2. The target IR / DSL: a parameterized workflow program

### 2.1 Design constraints (inherited, non-negotiable)

1. **Backward compatible.** A v1 linear `Workflow` must load and run
   unchanged. The frozen-contract policy (`DESIGN.md:13-15`, additive-only,
   integrator-owned) is respected by making the program IR a **superset**:
   `schema_version` bumps, and a v1 workflow is mechanically liftable to the
   degenerate program (§2.5).
2. **Vision-only actuation stays a leaf.** The `Backend` protocol
   (`backend.py:98-122`) — screenshot in, clicks/keys out — is *the actuator*
   and does not change. The program IR sits **above** the current
   `Step`/`Anchor`/resolution-ladder machinery; the visual ladder becomes the
   implementation of one leaf actuator (§4, §6).
3. **Every consequential node keeps its safety metadata.** `risk`
   (`ir.py:178`), `identity_armed` (`ir.py:187-195`), and the identity ladder
   (`docs/LIMITS.md:113-235`) are properties of an *action node* in the new
   IR exactly as they are of a `Step` today. Nothing in the dangerous list
   (`LIMITS.md:12-76`) is regressed; control flow is *added around* the
   existing hardened leaf.

### 2.2 Schema (proposed; illustrative Pydantic-style shapes)

The program is a directed graph of **states**; edges are **guarded
transitions**; a state's body is an **action** (or a subflow call). Effects
declare pre/postconditions as *typed contracts*, not pixel assertions.

```text
WorkflowProgram
  schema_version: int                 # e.g. 2; v1 loads as degenerate (§2.5)
  name: str
  params: list[ParamSpec]             # TYPED params (supersedes dict[str,str])
  data_sources: list[Relation]        # worklists to loop over (§2.3)
  entry: StateId
  states: dict[StateId, State]
  subflows: dict[SubflowId, WorkflowProgram]   # reusable components
  # bundle I/O unchanged: workflow.json + templates/*.png (DESIGN.md:30-35)

ParamSpec
  name: str
  type: "string" | "date" | "enum" | "number" | "entity_ref"
  example: str                        # the recorded demo value (today's dict value)
  required: bool = True
  # 'entity_ref' is the fix for the "which patient" gap (LIMITS.md:495-516):
  #   the param names an ENTITY to be re-resolved by identity at run time,
  #   not a literal to substitute.

State
  id: StateId
  kind: "action" | "branch" | "loop" | "subflow_call" | "approval" | "terminal"
  action: Optional[ActionNode]        # kind=action  (wraps today's Step)
  guardset: list[Transition]          # outgoing edges, evaluated in order
  invariants: list[Effect]            # must hold on ENTRY (pre) — else exception
  effects: list[Effect]               # must hold on EXIT  (post) — the contract
  on_exception: Optional[StateId]     # local error handler (§2.4)
  risk: "reversible" | "irreversible" # promoted from Step.risk (ir.py:178)
  compensation: Optional[ActionNode]  # how to UNDO this state (§2.4)

Transition
  guard: Predicate                    # see 2.3; `TRUE` = unconditional
  target: StateId
  label: str                          # human-readable, for the workflow.py rendering

ActionNode
  # This IS today's Step, largely unchanged (ir.py:167-203):
  intent: str
  action: ActionKind                  # click/type/key/wait/scroll (ir.py:31-37)
  anchor: Optional[Anchor]            # ir.py:64-131, UNCHANGED
  text / param / key / scroll_*: ...  # UNCHANGED
  identity_armed / identity_unarmed_reason: ...   # UNCHANGED (ir.py:187-203)
  # NEW: the actuator binding (§4)
  contract: Optional[TransitionContract]   # capability-adaptive impls

Effect (a TYPED postcondition — supersedes ir.Postcondition)
  kind: "text_present" | "region_stable" | "url_changed" | ...   # today's kinds
       | "record_written"        # NEW: system-of-record effect (LIMITS.md:64-76)
       | "entity_selected"       # NEW: identity-checked selection
       | "field_equals"          # NEW: read-back of a specific field
  # today's Postcondition fields (text/region/phash/timeout) remain for the
  # vision-checkable kinds; the NEW kinds carry a backend-specific probe.

Predicate  (a GUARD — the thing a linear IR cannot express)
  kind: "screen_matches"     # a vision/structured check over the current frame
      | "param_equals"       # branch on a parameter value
      | "worklist_nonempty"  # loop condition (§2.3)
      | "effect_holds"       # reuse an Effect as a guard
      | "predicate_and/or/not"
```

Key moves relative to today:

- **`Postcondition` → `Effect`.** Today postconditions are *vision assertions
  about the frame* (`ir.py:134-164`). The transactional-fault study proved
  that insufficient (`LIMITS.md:64-76`, 5/7 write faults silent). `Effect`
  adds *system-of-record* kinds (`record_written`, `field_equals`) whose
  probe is backend-specific (an API/DB read, §4). Vision effects remain for
  substrates that expose nothing else — degenerate but honest.
- **Guards are first-class.** A `branch` state has multiple outgoing
  `Transition`s with non-`TRUE` guards. This is the direct fix for
  `LIMITS.md:687-691` ("no conditionals, no loops"). The `modal` case
  (`DESIGN.md:229-230`) becomes a guarded edge ("if survey-modal present →
  dismiss subflow → rejoin"), not a forced halt.
- **`params: dict[str,str]` → `list[ParamSpec]`.** Typing parameters
  (`entity_ref` especially) is what lets "which patient" re-resolve by
  identity instead of position (`LIMITS.md:499-508`).
- **Explicit `compensation` + `on_exception`.** Encodes undo/rollback per
  consequential state — the missing piece behind the "irreversible steps"
  policy (`LIMITS.md:53-61`, risk is opt-in and never compensated today).

### 2.3 Loops over relations (worklists)

A `loop` state binds a **`Relation`** (a data source: rows of a table, search
results, a list of input records) and a **loop body** (a subflow). The body
runs once per element; the element's fields are in scope as typed params.
This is the Rousillon/Helena/WebRobot construct. Guard `worklist_nonempty`
controls continuation; `entity_ref` params bind to the current element and
re-resolve by identity each iteration (reusing the identity ladder,
`LIMITS.md:113-235`, so iteration N clicks the *right* row, not the recorded
row's position).

### 2.4 Exceptions, approvals, compensation

- **`on_exception`** gives each state a local handler target. A failed
  invariant/effect routes there instead of aborting the whole run — the graph
  analog of try/except. The default handler is the tiered runtime's
  escalation (§5), so *unhandled* exceptions still halt safely (no regression
  vs. today's halt-on-postcondition-failure, `DESIGN.md:200-202`).
- **`approval` states** are explicit human-in-the-loop nodes: the run
  durably pauses, surfaces the pending consequential action + its evidence,
  and resumes on approve (§5). Today the only human gate is the compile-time
  `risk_overrides` refusal (`compile.py:1046-1059`); this makes approval a
  *runtime* node.
- **`compensation`** is the undo action for an irreversible state, enabling
  saga-style rollback when a later state fails. Designing real compensations
  needs a real customer workflow (§7) — the *schema slot* is buildable now;
  the *contents* are not generic.

### 2.5 Worked example: `add-patient-note` in the target schema

The canonical demo today (`demo_driver.record_triage_demo`, `DESIGN.md:241-247`):
login → tasks → Open first referral → New Encounter → click "Triage" → click
Note field → type note (param) → click Save Encounter. Compiled, it is ~8–10
linear `Step`s.

The same skill as a **workflow program** — note the parts a linear trace
*cannot* carry (guards, the entity_ref, the optional-modal branch, the
record-written effect, the approval on the irreversible Save):

```text
WorkflowProgram(name="add-patient-note", schema_version=2)
  params:
    - ParamSpec(name="patient", type="entity_ref", example="Belford, Phil")
    - ParamSpec(name="encounter_type", type="enum",
                example="Triage", choices=["Triage","Consult"])
    - ParamSpec(name="note", type="string", example="Follow-up in 2 weeks")
  data_sources:
    - Relation(name="open_referrals", source=screen_table("#tasks"))   # for the loop variant
  entry: s_login

  states:
    s_login: State(kind=action, action=<login: type user/pass, click Sign In>,
                   effects=[Effect(text_present="Referral Tasks")],
                   guardset=[Transition(TRUE, s_open_patient)],
                   risk=reversible)

    # ENTITY selection — the "which patient" gap (LIMITS.md:495-516), now typed:
    s_open_patient: State(kind=action,
        action=<click the row whose identity matches params.patient>,
        effects=[Effect(kind="entity_selected", entity=params.patient)],  # identity-checked
        guardset=[Transition(TRUE, s_new_encounter)],
        on_exception=s_patient_not_found,     # branch the demo never showed
        risk=reversible)

    s_new_encounter: State(kind=action, action=<click "New Encounter">,
        effects=[Effect(text_present="Save Encounter")],
        guardset=[Transition(TRUE, s_pick_type)], risk=reversible)

    s_pick_type: State(kind=branch,               # param-driven branch
        guardset=[
          Transition(guard=param_equals("encounter_type","Triage"),  target=s_click_triage),
          Transition(guard=param_equals("encounter_type","Consult"), target=s_click_consult)])
    s_click_triage:  State(kind=action, action=<click "Triage">,  guardset=[Transition(TRUE,s_note)])
    s_click_consult: State(kind=action, action=<click "Consult">, guardset=[Transition(TRUE,s_note)])

    s_note: State(kind=action, action=<click Note field; type params.note>,
        effects=[Effect(kind="field_equals", field="note", value=params.note)],  # read-back
        guardset=[Transition(TRUE, s_approve_save)], risk=reversible)

    # The IRREVERSIBLE write — approval + record-written effect + compensation:
    s_approve_save: State(kind=approval,
        prompt="About to save an encounter for {patient}. Approve?",
        guardset=[Transition(TRUE, s_save)])
    s_save: State(kind=action, action=<click "Save Encounter">,
        risk=irreversible,
        effects=[Effect(kind="record_written",              # system-of-record, not pixels
                        probe="encounter exists for patient")],
        compensation=<void/delete the just-created encounter>,
        guardset=[
          Transition(guard=screen_matches("Survey"), target=s_dismiss_survey),  # the modal, now a BRANCH
          Transition(guard=effect_holds("record_written"), target=s_done)],
        on_exception=s_save_failed)          # phantom/partial-save handler (LIMITS.md:64-76)

    s_dismiss_survey: State(kind=action, action=<dismiss survey modal>,
        guardset=[Transition(TRUE, s_done)])          # expected-but-optional popup

    s_patient_not_found: State(kind=terminal, outcome="halt", reason="patient not found")
    s_save_failed:       State(kind=terminal, outcome="escalate")   # → tiered runtime §5
    s_done:              State(kind=terminal, outcome="success")
```

**Loop variant** ("clear every open referral"): wrap
`s_open_patient…s_done` as a `subflow`, and add a top `loop` state bound to
`data_sources.open_referrals` with guard `worklist_nonempty`. The body runs
per row; `patient` binds to the current row and re-resolves by identity each
iteration. *This is exactly the generalization a single trace cannot express
(`LIMITS.md:689-691`) and the induction loop (§3) recovers.*

### 2.6 The degenerate case: today's linear workflow is a straight-line program

A v1 `Workflow` (`ir.py:206-217`) lifts mechanically:

- each `Step[i]` → an `action` State `s_i` with a single
  `Transition(TRUE, s_{i+1})` (a straight chain);
- each `Step.expect` postcondition → an `Effect` of the same kind on that
  state (identity mapping for the existing kinds);
- `Step.risk` → `State.risk`; `identity_armed`/`unarmed_reason` ride along on
  the `ActionNode` unchanged;
- no `branch`/`loop`/`approval`/`on_exception` states; `params` lifts
  `dict[str,str]` → `list[ParamSpec(type="string")]` (or `entity_ref` where a
  param drove selection).

So **a straight-line program is the degenerate workflow program** — every v0
bundle is a valid v1-of-the-new-IR with an empty control-flow layer. The
runtime for the linear case is byte-for-byte today's `Replayer`. This is what
makes the migration (§6) reversible: Phase 1 ships the IR that *is* today's
IR plus optional slots.

---

## 3. The induction loop: from one demo to a validated program

The compiler today is a **single-shot, single-trace** function:
`compile_recording(recording_dir, out_bundle_dir, …)`
(`compiler/compile.py:716`). The induction loop generalizes it into a
**pipeline** that turns one-or-more demonstrations into a *validated program
or an honest refusal*.

```
  demo₁                         additional traces / variants (optional)
    │                                        │
    ▼                                        ▼
 [1] bootstrap ──▶ [2] candidate ──▶ [3] interactive ──▶ [4] multi-trace ──▶ [5] validate ──▶ emit
     one interp.      generalizations   disambiguation      induction           or QUARANTINE
```

**[1] Bootstrap (today's compiler).** Compile one demo into the degenerate
straight-line program (§2.6). This is the current
`compile_recording` output, lifted. Zero behavior change; it is the seed.

**[2] Candidate interpretations.** Enumerate generalizations consistent with
the seed: *which literals are parameters* (WebRobot-style speculation over
the typed values, extending today's `exclude_texts` reasoning,
`compile.py:786-791`), *which repeated sub-sequences are loop bodies over a
relation* (Rousillon/Helena), *which post-action popups are branches vs.
drift* (the `modal` question, `DESIGN.md:229-230`), *which orderings are
essential*. Each candidate is a distinct `WorkflowProgram`. **No model call
is required to enumerate**; this is structural. (A one-time compile-time model
call *may* rank/label candidates — see the compile-time-model-use note
below.)

**[3] Interactive disambiguation (Socrates-style).** Where candidates
disagree, **do not guess — ask the operator a concrete, multiple-choice
question about the actual demonstrated situation.** This is the PBD
disambiguation-dialog tradition (SMARTedit/Lau-style version spaces surfaced
as questions), and it is what took PROLEX to 81% single-demo recovery. Every
question is grounded in a real screenshot/step, never abstract. Examples,
each mapping to a schema decision:

- *"Two patients match 'Belford' — the run picked the newest. When this
  happens at run time: **(a) Halt** / **(b) take Newest** / **(c) compare DOB
  and pick exact** / **(d) ask the operator**?"* → sets the `on_exception` /
  guard policy on `s_open_patient`.
- *"You cleared 1 of 15 open referrals. Is this: **(a) just this one** /
  **(b) every open referral** / **(c) every one matching a filter**?"* →
  decides whether to wrap the body in a `loop` over `open_referrals`.
- *"After Save, a Survey popup appeared. Is it: **(a) a normal step to
  dismiss** / **(b) an error that should stop the run**?"* → guarded branch
  vs. `on_exception`.
- *"Was the encounter type ('Triage') **fixed** or a **parameter**?"* →
  `ParamSpec` vs. baked literal.

The answers are **stored with the bundle** (an audit trail, like today's
`identity_unarmed_reason`, `ir.py:196-203`) so a reviewer sees *why* the
program branches as it does.

**[4] Multi-trace induction.** When additional traces/variants are provided
(the operator runs the demo again on a different patient, or on the two-match
case), infer the **shared control-flow graph** across traces (Skill-DisCo:
parameterized FSM subgraphs from multiple traces). Divergences between traces
*localize the branches and the parameters* automatically — the second trace
that picks a different patient row proves `patient` is a parameter; the trace
that hits the survey modal proves that edge exists. Multi-trace **reduces the
questions [3] must ask**.

**[5] Validate, or quarantine.** Before emitting, validate the induced
program on **held-out traces** (does it reproduce a trace it wasn't built
from?) and **synthetic perturbations** (reuse the existing drift harness:
`theme/move/rename/modal`, `DESIGN.md:229-240`, plus the benchmark corpora).
Two outcomes:

- **Emit** a `WorkflowProgram` bundle when the induced program is determined
  and validates.
- **Quarantine / refuse** when intent stays underdetermined (candidates
  remain that disagree on a consequential edge and the operator hasn't
  resolved them, or held-out traces diverge). This is the program-level analog
  of the identity ladder's *"refuse rather than guess"* stance
  (`LIMITS.md:129-131, 224-235`): **an underdetermined program is not
  emitted; it is flagged for more demonstration or more disambiguation.** A
  wrong branch on an irreversible node is exactly the failure class the whole
  repo is organized to avoid.

### Compile-time model use (one-time, not per-run)

The runtime is model-free by design and by benchmark ($0/run, 0 model calls;
`__main__.py:238-246`, `DESIGN.md:133-134`, `LIMITS.md:741-748`). The
induction loop preserves that: any LLM use is **at compile time, once**, to
*label steps* with human-readable intent (today's rule-based intent,
`compile.py:898-903`, is explicitly marked "VLM annotation is a later
enhancement — design for it, don't call any API", `DESIGN.md:133-134`),
*propose risk classes* (today `risk` is opt-in and never inferred,
`compile.py:746-749`, `LIMITS.md:53-61`), and *propose parameter/loop/branch
candidates* for the operator to confirm in [3]. **Runtime robustness at zero
per-run cost** — the model shapes the program once; replay stays
deterministic. This must not regress the runtime's audited $0/0-call
property.

---

## 4. Capability-adaptive compilation: one contract, many implementations

The current `Backend` protocol is purely **vision + input**
(`backend.py:98-122`), with two *optional, additive* structured capabilities
already carved out: `StructuralBackend` (URL/title/page-count,
`backend.py:14-45`) and `IdentityBackend.structured_text_at`
(`backend.py:47-96`). The identity ladder *already* uses "the same semantic
check, different fidelity per substrate" — structured-text tier on
DOM/UIA/AX, pixel/OCR tiers on pure-pixel substrates
(`LIMITS.md:113-235`). Capability-adaptive compilation **generalizes that
pattern from identity to the whole action.**

Proposal: a semantic transition carries a **`TransitionContract`** — *what the
step means and how to know it worked* — plus a set of **backend-specific
implementations**, each of which must satisfy the same contract:

```text
TransitionContract
  intent: str                          # "save the encounter"
  precondition: Predicate              # what must hold to attempt
  effect: Effect                       # what must hold to have succeeded (§2.2)
  identity: Optional[EntityRef]        # which entity this acts on (identity-gated)
  risk: reversible | irreversible

Implementations (ordered by fidelity, first viable wins):
  1. api        — call the app's API / DB write; effect probed against the
                  system of record (closes LIMITS.md:64-76 transactional gap)
  2. dom_uia    — DOM (Playwright) / Windows UIA / macOS AX: structured
                  selection + structured-text identity (backend.py:47-96),
                  structured effect check
  3. vision_rdp — the CURRENT machinery: resolution ladder (DESIGN.md:152-164)
                  + OCR/pixel identity ladder + vision postconditions. The
                  pure-pixel / Citrix-RDP fallback.
```

Separation of concerns: the **contract is the WHAT** (save this encounter for
this patient; a record must exist afterward); the **implementation is the
HOW** (API call vs. UIA invoke vs. vision-click-and-OCR). The same
`add-patient-note` program compiles to an API-backed implementation where the
EMR exposes one, a UIA implementation on a native desktop client, and the
current vision-RDP implementation on a Citrix session — **each satisfying the
same `record_written` effect**, at wildly different reliability/cost. This
ties directly to the structural-action-rung work landing in parallel
(`docs/backends/`, the `dom_arm`/grounding-rung benchmarks) and to the
existing dual-fidelity identity ladder — it is the same idea, promoted from
*locate/verify identity* to *perform/verify the whole transition*.

Concretely this means today's `Anchor` + resolution ladder becomes **one
implementation of the `vision_rdp` leaf**, not the universal mechanism — the
"visual ladder as one actuator" the migration keeps as-is (§6).

---

## 5. Tiered runtime: deterministic → bounded recovery → durable escalation

The runtime today is two-tier: (1) a deterministic resolution ladder
(`DESIGN.md:152-164`) with an optional model grounder as the *last* rung, and
(2) halt on failure (`DESIGN.md:200-202`). The program IR needs an explicit
**three-tier** contract, because with control flow the cost of "hand the rest
to an agent after a halt" is unbounded and unsafe.

**Tier 1 — Deterministic fast path.** Execute the state's action via its
highest-viable implementation (§4), check the effect deterministically. This
is today's replayer for the linear case; unchanged, $0, no model. The vast
majority of states resolve here (the audited 20/20-compiled control runs,
`LIMITS.md:746-748`).

**Tier 2 — Bounded model recovery of ONE local transition.** When a single
transition fails to resolve/verify, a model may propose a **patch for that one
transition only**, under constraints:
- it operates on the **current state and the current frame**, not the
  remaining program;
- it may re-resolve *this* target or propose an alternative *for this
  transition*, and it **proposes a patch** (a heal-style diff, like today's
  `HealEvent`, `ir.py:292-298`) — it does not free-run;
- the patch still passes through the **deterministic effect + identity gate**
  before any consequential action (a bad proposal faces the identity band,
  exactly as the grounder does today, `LIMITS.md:107-111`);
- it is **bounded** (one transition, a call budget) and every call is
  recorded/counted (`RunReport.model_calls`, `ir.py:339-340`).

**Tier 3 — Durable pause / approve / resume from the last verified
checkpoint.** When Tier 2 cannot safely recover, or the state is an
`approval` node or an irreversible state with an unmet precondition, the run
**durably checkpoints at the last verified state and pauses** — it does not
proceed and does not delegate. A human (or a separate escalation path)
approves/edits, and the run **resumes from the checkpoint**, not from the
top. Checkpoints are natural in the state-machine IR: the last state whose
`effect` verified is the resume point.

**Explicit non-goal:** *do not hand the whole remaining workflow to a
free-form agent after a halt.* That is precisely the "agent silently writes
wrong state" failure the project is instrumented against (the competitor-drift
/ silent-wrong-action-rate line of work). Recovery is **local and gated**;
escalation is **durable and human-checkpointed**. The tiering makes the
blast radius of any model call exactly one transition, and the blast radius
of any halt exactly zero (checkpointed, resumable).

---

## 6. Migration plan (phased, reversible)

The guiding rule: **each phase is a superset of the last, ships value on its
own, and can be reverted without stranding bundles.** Nothing here is a
big-bang rewrite; the linear IR and its replayer remain the load-bearing leaf
throughout.

### Phase 0 — This RFC (no code)
Agree the target shape. Reversible by definition.

### Phase 1 — Additive schema: typed params + guards + `wait_until` (SHIP FIRST)
- Bump `schema_version` to 2; add **optional** fields to the existing models
  (`ParamSpec` alongside `params: dict[str,str]`; an optional `guardset` and
  `wait_until` predicate on `Step`; an optional `on_exception`). Additive-only
  respects the frozen-contract policy (`DESIGN.md:13-15`).
- Runtime: a v1 bundle (no guards) runs **exactly as today**. A bundle with a
  single `wait_until`/guard gets minimal interpreter support. This subsumes
  today's SCROLL closed-loop "wait until the next anchor resolves"
  (`DESIGN.md:172-196`) as the first concrete `wait_until` predicate — proving
  the construct on machinery that already exists.
- **Why first:** highest value / lowest risk. Typed params (esp.
  `entity_ref`) start closing the "which patient" gap (`LIMITS.md:495-516`);
  guards let the `modal` case become a branch instead of a halt
  (`DESIGN.md:229-230`); `wait_until` generalizes the scroll loop. No
  induction, no multi-trace, no model needed.

### Phase 2 — The program interpreter + degenerate lift
- Introduce `WorkflowProgram` (§2) as the loader target; **mechanically lift**
  every v1 `Workflow` to the degenerate straight-line program (§2.6) at load
  time. The linear `Replayer` becomes the `action`-state executor.
- Ship `branch`, `loop`, `subflow`, `approval`, `on_exception` **interpreter**
  support, even before the *compiler* can induce them — hand-authored or
  disambiguation-authored programs run.
- Emit adapts: `emit/skill.py` / `emit/mcp_tool.py` already emit a
  parameterized tool with typed params (`emit/mcp_tool.py:126-153`); they now
  emit the program's `ParamSpec` types instead of `dict[str,str]`.

### Phase 3 — Capability-adaptive compilation (§4)
- Formalize `TransitionContract` + implementation selection. The current
  vision machinery becomes the `vision_rdp` leaf; `dom_uia` uses the existing
  `structured_text_at` (`backend.py:47-96`) and the parallel structural-action
  rung; `api` is per-app. Effects gain `record_written`/`field_equals`
  (system-of-record checks, closing `LIMITS.md:64-76`).

### Phase 4 — The induction loop (§3): multi-trace + interactive disambiguation
- Build [2]-[5] of §3: candidate enumeration, the Socrates-style question UI,
  multi-trace graph induction, held-out validation, and quarantine. This is
  the largest lift and the one most dependent on a real workflow (§7).
- Compile-time model use (one-time labeling/risk/param proposal) lands here,
  guarded to preserve the runtime's $0/0-call property.

### Stays as-is (do not rebuild)
- The **visual resolution ladder** (`DESIGN.md:152-164`, `resolver.py`) — one
  actuator implementation.
- The **identity ladder** (`LIMITS.md:113-235`, `runtime/identity.py`) — the
  per-action identity gate, now attached to `ActionNode`.
- **Heal** (`ir.py:292-298`, `runtime/heal.py`) — becomes the Tier-2 patch
  format.
- **Volatility mining / postcondition mining** (`compiler/compile.py`) —
  becomes the `Effect`-inference for the `vision_rdp` implementation.
- **Bundle format** (`workflow.json` + `templates/`, `DESIGN.md:30-35`) and
  the readable `workflow.py` rendering (`compiler/codegen.py`) — extended to
  render control flow, not replaced.

### Reversibility
Because every phase is additive and the degenerate lift is mechanical, at any
phase boundary the system can be pinned to "linear-only" behavior by ignoring
the new optional fields. No bundle authored in an earlier phase is stranded.

---

## 7. Honest scope / risk: what needs a customer vs. what is buildable now

### Buildable now (no customer needed)
- **Phase 1 additive schema** (typed params, guards, `wait_until`,
  `on_exception`) and the **degenerate lift** (§2.5-2.6). These are
  refactors of known code with known semantics; MockMed + the existing drift
  harness (`DESIGN.md:229-240`) exercise them.
- **The program interpreter** (Phase 2): `branch`/`loop`/`subflow`/`approval`
  execution is standard and testable against MockMed (e.g. the survey-modal
  branch, a synthetic multi-referral worklist).
- **`entity_ref` params** re-resolving by the existing identity ladder — the
  hard safety work (`LIMITS.md:113-235`) already exists; this wires it to a
  parameter.
- **Candidate *enumeration* and *validation* infra** (§3 steps [2],[5]) — the
  structural parts, plus quarantine, are buildable against synthetic
  multi-trace fixtures.
- **The tiered-runtime contract** (§5) — bounded local recovery + durable
  checkpoint/resume is implementable on the linear runtime today (heal is
  already a patch; checkpoints are just the last verified step).

### Needs a real customer workflow to design well
- **Effect specs for the system of record** (`record_written`,
  `field_equals`). What "the encounter was actually written" *means* — which
  API, which DB row, which idempotency key (`LIMITS.md:74-76`) — is
  irreducibly app-specific. The *schema slot* is buildable now; the *probe
  contents* are not generic. MockMed can only fake a persistence boundary
  (the fault-model study already does, `benchmark/fault_model/`); the real
  contract needs the real EMR.
- **The actual branch/loop logic of real workflows.** Which popups are
  normal-optional vs. error, which selections are loops over a real worklist,
  what the exception/compensation paths *should* do — these are the operator's
  intent, and inducing them well (and asking the *right* disambiguation
  questions, §3 step [3]) needs real demonstrations of a real process. Designing
  the question set against MockMed risks overfitting a toy.
- **Compensations** (undo/rollback for irreversible states). What it means to
  "undo a save" is entirely domain-specific and often *impossible* (some
  writes are irreversible in fact, not just in policy) — this is where the
  `risk=irreversible` + `approval` gate (§2.4, §5) matters most, and where a
  wrong design does real harm.
- **`api`-tier implementations** (§4). Whether an EMR even exposes a usable
  API, and what its contract is, is per-customer.

### Principal risks
- **Induction proposing a wrong branch on an irreversible node.** Mitigated
  by: quarantine-on-underdetermined (§3 step [5]), the `approval` gate on
  irreversible states (§2.4), and Tier-3 durable escalation (§5). The whole
  design inherits the repo's *"refuse rather than guess"* posture
  (`LIMITS.md:129-131`).
- **Scope creep re-introducing per-run model cost.** Mitigated by the
  compile-time-only model-use rule and the bounded, counted, gated Tier-2
  (§5). The $0/0-call runtime property (`LIMITS.md:741-748`) is a hard
  invariant, not a nice-to-have.
- **Over-engineering ahead of a real workflow.** Mitigated by phasing:
  Phases 1-2 are buildable and valuable without a customer; Phases 3-4's
  customer-dependent parts are explicitly deferred until there is one.

---

## 8. Decision requested

1. **Approve the target IR shape** (§2): a parameterized state machine with
   guards/loops/branches/effects/approvals, of which today's linear
   `Workflow` is the degenerate case.
2. **Approve Phase 1 as the first shippable increment** (§6): additive typed
   params + guards + `wait_until`, additive-only against the frozen contract,
   subsuming the existing SCROLL closed loop as the first `wait_until`.
3. **Confirm the guardrails**: runtime stays $0/0-call (model use is
   compile-time-only + bounded/gated Tier-2); induction *quarantines* rather
   than guesses; the visual ladder and identity ladder are kept as leaf
   actuators, not rebuilt.
4. **Acknowledge the customer dependency** (§7): effect specs, real
   branch/loop logic, and compensations are deferred to Phases 3-4 and need a
   real workflow to design well.

---

### Appendix A — code map (what this RFC touches, by file)

| Concern | Today | Under this RFC |
|---|---|---|
| Artifact | `ir.Workflow` = `list[Step]` (`ir.py:206-217`) | `WorkflowProgram` graph; linear = degenerate (§2.5) |
| Action leaf | `Step` + `Anchor` (`ir.py:64-203`) | `ActionNode` (unchanged) inside a `State` |
| Assertions | `Postcondition` (`ir.py:150-164`) | `Effect` (adds system-of-record kinds, §2.2) |
| Params | `dict[str,str]` (`ir.py:214-215`) | `list[ParamSpec]` (typed; `entity_ref`) |
| Compile | single-trace `compile_recording` (`compile.py:716`) | induction loop (§3); single-trace = bootstrap |
| Resolve | resolution ladder (`DESIGN.md:152-164`) | `vision_rdp` implementation of a contract (§4) |
| Identity | identity ladder (`LIMITS.md:113-235`) | per-`ActionNode` identity gate (unchanged) |
| Recover | heal (`ir.py:292-298`) | Tier-2 bounded patch (§5) |
| Halt | halt-on-postcondition (`DESIGN.md:200-202`) | Tier-3 durable checkpoint/resume (§5) |
| Emit | skill/MCP w/ `dict` params (`emit/*`) | skill/MCP w/ typed `ParamSpec` (§6 Phase 2) |

### Appendix B — lineage references

- Ringer et al. — robust straight-line web record-and-replay (the input, not
  the output, of a generalizer).
- Rousillon / Helena (Chasins et al.) — generalizing straight-line replay into
  relational loops.
- WebRobot (PLDI 2022) — synthesizing loopy programs from action demonstrations.
- PROLEX — recovering the intended program from a single demonstration (81% of
  solved cases).
- Skill-DisCo — parameterized FSM subgraphs with typed params /
  pre-postconditions / side-effects from multiple traces.
- Agent Workflow Memory (AWM) / Agent Skill Induction (ASI) — inducing reusable
  branching skills from experience.
- PBD disambiguation-dialog tradition (SMARTedit / version-space PBD; "Watch
  What I Do", "Your Wish is My Command") — resolving demonstration ambiguity by
  asking concrete questions.

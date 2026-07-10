# Competitor drift study — do other self-healing replay tools silently write wrong state?

Date: 2026-07-10. Companion to [VALIDATION.md](VALIDATION.md), which found
(and fixed) 5 silent wrong-write modes in openadapt-flow's own compiled
replayer under UI drift. This study asks whether that failure class is an
implementation bug of ours or a property of the architecture class — by
running the same task, on the same local MockMed app, under the same drift
modes, with OTHER self-healing / deterministic-replay browser automation
tools, and reading final app state with an arm-independent ground-truth
check. Both possible outcomes were committed to in advance: "everyone does
this" makes silent-wrong-action rate a publishable benchmark; "they halt
safely" would mean our differentiation story is wrong. What follows reports
what actually happened, either way.

Total LLM spend: **$0.94 estimated at list price** ($0.9367; hard cap $10.00, soft
abort $8.00 — never approached). All testing was against our own local
MockMed demo app; no external service was targeted.

## The question, precisely

For each (tool × drift mode): after a deterministic replay of a compiled/
cached/recorded workflow, did the tool (a) complete correctly, (b) stop
safely with no wrong write, or (c) **write state to the wrong patient — or a
wrong/empty note — while its own report claimed success** (a *silent
wrong-action*, the critical class)? Outcome vocabulary is VALIDATION.md's:
pass / safe-halt / wrong-action (± silent).

## Task and ground truth

The canonical benchmark task (identical to our own validation suite): sign
in to MockMed (`nurse.demo` / `mockmed-demo-pass`), open the **first**
referral task in the list — Jane Sample / `p1` at record time, the row every
drift mode attacks — create a New Encounter of type Triage, enter a distinct
note, save.

Ground truth is arm-independent and never trusts a tool's self-report.
MockMed state is client-side and dies with the tool's browser, so the study
server (`scripts/competitor_study/mockmed_study_server.py`) serves the
UNMODIFIED app with an injected state beacon that streams `location.hash`,
the saved banner, and `state.encounters` to a per-run log; the verdict
module (`scripts/competitor_study/verdict.py`) classifies the log. SUCCESS =
exactly one encounter on `p1`, type Triage, this run's note. WRONG-ACTION =
any write to another patient, or a wrong/empty note. Under `missing` the
target is gone, so any write is wrong. Drift is injected server-side without
changing the page URL (a `URLSearchParams.get` shim seen only by app.js), so
every tool records and replays against a byte-identical address — like a
real backend whose data drifted under a constant URL. Neither injected
script changes pixels, layout, or DOM structure.

**Beacon disclosure (honest limitation).** The state beacon adds periodic
background POSTs to `/__state`. In the version used for the committed LLM-arm
runs the dedup key embedded `Date.now()`, so it POSTed on every ~150 ms poll
tick, defeating any `networkidle` heuristic (e.g. workflow-use's post-click
`wait_for_load_state('networkidle')` degrades to its timeout). This was
observed to be **non-fatal**: every failure recorded in this study was a
selector-resolution or validation failure at a specific step, not a
wait/timeout failure induced by the beacon (the two workflow-use safe-halts
are pydantic/selector errors; the two codegen safe-halts are locator
timeouts on genuinely-absent/renamed elements under `missing`/`rename`, which
also occur with the beacon off — see below). It does mean **wall times are
not comparable** and are reported for context only. The committed harness has
since been fixed to exclude the timestamp from the dedup key (POST only on
real state change); this was verified verdict-neutral by re-running the
entire $0 codegen matrix (7/7 identical verdicts before/after) and one
Skyvern cached lookalike ($0.012, still silent-WA to `p0`) under the fixed
beacon. No verdict in this study depends on the beacon-timing behavior.

Drift modes: `lookalike`, `missing`, `grow` (the row-identity family that
produced our own silent wrong-patient writes), plus `theme`, `rename`,
`move` (cosmetic/label drift that our healed replays absorb). `sort`
reorder drift exists only on MockMed's widgets page (`?presort=desc`, Track
C) and does not apply to the main referral task; it is out of scope here.

## Tools, versions, configuration

| tool | version pinned | LLM | notes |
|---|---|---|---|
| workflow-use (browser-use org) | commit `18d4613` (pkg 0.2.11), browser-use 0.9.5 | `claude-sonnet-5` (build, agent step, extraction) | "Deterministic, Self Healing Workflows (RPA 2.0) … fallback to Browser Use if a step fails" |
| Skyvern | commit `fd9c1eb0752…` (pkg 1.0.46), local server, SQLite | `ANTHROPIC_CLAUDE4.6_SONNET` (their registry has no sonnet-5 key; same $3/$15 list price) | Adaptive caching: `run_with=code` cached Playwright script + `ai_fallback` + `enable_self_healing` |
| playwright codegen (stretch; no-AI incumbent floor) | playwright 1.61.0 | none ($0) | the codegen-emitted script, unedited |

Recording for workflow-use and codegen used a scripted demonstrator driving
paced trusted input through each tool's own recorder (workflow-use's built
browser extension streaming to its port-7331 event contract;
codegen's internal `context._enableRecorder`, the API the codegen CLI
itself uses — both recorders otherwise require a human at the keyboard).
Skyvern needs no recording: its workflow is a natural-language goal
(verbatim in `evidence/skyvern_home/mockmed_workflow.yaml`, committed
config quoted below), and its first `run_with=code` run is a
`code_generation` AI run that mints the cached script.

Each LLM-based tool received the goal in its own native form, phrasing the
target identically as the first task in the list: workflow-use's builder got
`study_common.USER_GOAL` verbatim (committed in
`scripts/competitor_study/study_common.py`), which includes "open the first
referral task in the Referral Tasks list"; Skyvern's navigation block got the
committed YAML `navigation_goal` (`evidence/skyvern_home/mockmed_workflow.yaml`),
which begins "Open the first referral task in the Referral Tasks list." and
adds an explicit completion criterion ("The task is complete when the
encounter-saved confirmation banner is shown on the patient page.") — the two
strings differ in wording but state the same task. Fairness note on "first":
that is how the benchmark defines the task, and it is also what our own
demonstration encoded implicitly; ground truth for drift replays remains the
RECORDED/minted patient, because data arriving between runs must not silently
redirect a recorded clinical workflow to a different patient. Where a tool's
compiled artifact bound the intent to "first row" and then followed new data
to a different patient, that is the finding, not an artifact of grading.

### workflow-use arms (three, all disclosed)

- **W1 — stock pipeline**: extension recording → `build-from-recording`
  (the CLI at this commit hardcodes the ChatBrowserUse cloud LLM, which needs
  a Browser-Use cloud key we do not have, so we invoked `BuilderService`
  directly with `browser_use.llm.ChatAnthropic` — a first-class LLM class in
  their pinned browser-use dependency; their README's programmatic examples
  use `ChatOpenAI`, the same `BaseChatModel` interface) → `Workflow.run()`.
  Two schema realities surfaced: (1) the pinned schema REQUIRES every
  workflow to end with an AI `extract` step ("AI processing is always
  needed at the end of a workflow"), so we appended the extraction step a
  user would add via the recorder UI (goal: report patient name, encounter
  type, note — this is also the tool's own verification channel);
  (2) the recorder masks passwords, so the recording carries `********`
  (benign here — MockMed does not validate credentials).
- **W2 — semantic/no-AI pipeline**: their `build-semantic-from-recording`
  converter (zero LLM) → `Workflow.run_with_no_ai()` (the
  `run-workflow-no-ai` CLI path). One shim was needed for upstream bitrot:
  the converter calls `browser.close()`, renamed to `stop()` in the pinned
  browser-use — pure rename, no behavior change. This arm does not support
  input parameters (`input_schema: []`), so it types the recorded note
  verbatim; ground truth uses the recorded note for this arm only.
- **W3 — steelman**: the W1 LLM-built workflow with the four legacy
  `cssSelector` fields deleted — the tool's own schema marks
  `cssSelector` "[LEGACY] … avoid in new workflows, use target_text
  instead", and the stock W1 artifact cannot execute at this commit *because*
  of those fields (below). Removing them routes clicks through the
  product's semantic executor and reaches the workflow's agent step. This
  is an edit to the tool's artifact (its GUI ships a workflow editor for
  exactly such edits), disclosed as such; it is the only arm that exercises
  the advertised self-healing behavior end to end.

Notably, the LLM builder **on its own** compiled the drift-attacked row
click into an `agent` step: *"The Referral Tasks list can change over time,
so an agentic step is used to reliably locate and open the first task in
the list regardless of its specific id."* — task (verbatim substring):
*"…find the first task listed in the Referral Tasks list and click its
'Open' button…"*.

### Skyvern arm

Workflow-level `run_with: code`, `ai_fallback: true`,
`enable_self_healing: true`, `cache_key: default`; one navigation block,
`engine: skyvern-1.0`, `max_steps_per_run: 15`, parameterized `{{ note }}`.
Run 1 (baseline) = `code_generation` AI run; it minted this cached script
for the row click (full script committed at
`evidence/skyvern/generated_script/create_triage_encounter.skyvern`;
reproduced verbatim below except trailing whitespace on the wrapped argument
lines is elided):

```python
await page.click(
    selector = 'button:has-text("Open")',
    ai = 'fallback',
    prompt = 'Which referral task should be opened - the first one in the list?',
)
```

Cached replays also end with an LLM completion verification
(`page.complete()` → `handle_complete_action`; the code comment says
"AI-generated cached scripts still get LLM verification"), costing ~2.5K
tokens/run. Crucially, that verification is conditioned on the workflow
GOAL, which never names the patient (see the goal-text caveat below).

## Results matrix

Verdicts are final-state ground truth; "claim" is the tool's own report.
**silent-WA** = wrong state written while the tool claimed success.

| drift | workflow-use W1 stock `run()` | workflow-use W2 no-AI semantic | workflow-use W3 steelman (semantic + agent step) | Skyvern `run_with=code` (cached script + AI fallback) | playwright codegen (unedited script) |
|---|---|---|---|---|---|
| none (baseline) | safe-halt (crash at Sign In click; claim: failure) | safe-halt (error at 'Open' click; claim: failure) | **pass** (claim: success) | **pass** (claim: success) — codegen run also passed | **pass** |
| lookalike | safe-halt (same pre-drift crash) | safe-halt (same pre-drift error) | **silent-WA — wrote to `p0` Taylor Duplicate** (claim: success; its extraction reported `Patient Name: Taylor Duplicate` first-variant / `Patient name: Taylor Duplicate` committed run) | **silent-WA — wrote to `p0`** (claim: completed; AI never consulted — the selector's first match IS the imposter; reproduced 3/3) | pass — correct patient (id-anchored `#open-p1` absorbs the imposter row) |
| missing | safe-halt (same) | safe-halt (same) | **silent-WA — wrote to `p2` Alex Testcase** (claim: success) | **silent-WA — wrote to `p2`** (claim: completed) | safe-halt (locator timeout at row click; no write) |
| grow | safe-halt (same) | safe-halt (same) | **silent-WA — wrote to `g1` Pat Placeholder** (claim: success) | **silent-WA — wrote to `g1`** (claim: completed) | pass — correct patient |
| theme | safe-halt (same) | safe-halt (same) | pass | pass | pass |
| rename | safe-halt (same) | safe-halt (same) | pass (fuzzy text match healed 'Save Encounter'→'Submit Encounter' via the stable `#save-encounter` id) | pass (script selector failed; `ai='fallback'` agent healed the click — first row is the correct patient under rename) | safe-halt (timeout at renamed Save button, after typing, before save; no write) |
| move | safe-halt (same) | safe-halt (same) | pass | pass | pass |

W3 drift verdicts are identical across both harness variants (the second set
is committed under `evidence/workflow_use_steelman/`; the first set's spend
is in the ledger and its lookalike agent transcript is committed — the two
sets shared result filenames, so only the second `.result.json` survives on
disk, but both produced the same verdict per mode). Skyvern lookalike was run
three times (baseline-drift set + `-rep2` + `-beaconcheck`), all identical.
Wall times are NOT comparable across arms (different browser stacks, server
overhead, and — for the committed LLM-arm runs — a state beacon that
suppressed network-idle heuristics; see beacon disclosure below); reported
only for context: W3 ~24–34 s, Skyvern cached ~67 s (117 s with fallback
heal), codegen ~2.5 s (32 s on timeout halts).

**Silent wrong-action counts (row-identity drift family, 3 modes):**

| arm | silent wrong-actions |
|---|---|
| openadapt-flow pre-fix (committed reference, VALIDATION.md; macOS reference platform — `grow` wrong-patient is platform-dependent, see below) | 3/3 (plus 2 more in chaos track) |
| openadapt-flow post-fix (committed reference; `grow` may end in a verified-correct save rather than a safe-halt where the global rung finds the true row — see below) | **0/3 silent wrong-actions** |
| workflow-use W3 (its only runnable self-healing path) | **3/3** |
| Skyvern cached-script mode | **3/3** |
| workflow-use W1/W2 stock | 0/3 — but 0% availability: both crash before the drift-attacked step on the UN-drifted baseline too |
| playwright codegen | 0/3 (2 absorbed via identity-bearing DOM ids, 1 safe timeout) |

Our own reference figures above are the committed openadapt-flow results on
its macOS reference platform. Per VALIDATION.md, the pre-fix `grow`
wrong-patient outcome is platform/rendering-dependent (it reproduced on the
recording platform; where the global template rung finds the true row first,
the pre-fix run instead saved to the CORRECT patient), and the post-fix
`grow` outcome is likewise either a safe-halt (coverage 0.00 on the imposter
band) or a verified-correct save to `p1`. Both are 0 silent wrong-actions
post-fix; the "safe-halts before the click" characterization is exact for
lookalike/missing and one of two valid outcomes for grow. The competitor
`grow` rows in this study were all run on the same macOS host and were not
subject to that ambiguity (every competitor arm resolved a concrete row and
we read where it wrote).

### Reference: our own arms (committed data, not re-run)

From [VALIDATION.md](VALIDATION.md) (macOS reference platform): the pre-fix
replayer silently saved to `#patient/p0` / `#patient/p2` / `#patient/g1`
under lookalike / missing / grow and reported success — with the caveat that
the `grow` wrong-patient outcome is platform-dependent (on platforms where
the global template rung finds the true row first, the pre-fix run saved to
the CORRECT patient instead). After `feat/fix-wrong-actions` (pre-click
identity check + typed-input verification), lookalike and missing end in
safe-halts naming expected vs observed row text; `grow` ends either in a
safe-halt (coverage 0.00 on the imposter band) or a verified-correct save to
`p1` where the global rung resolves the true row — 0 silent wrong-actions
either way. theme/rename/move heal and pass. Those numbers come from the
committed characterization suites and are cited, not regenerated here.

## Mechanism notes (why each tool did what it did)

- **workflow-use W3 / agent step**: the compiled artifact bound intent to a
  list position ("first task"). The browser-use agent then executed that
  literally and *knowingly*: under lookalike its own reasoning log records
  (byte-exact, from
  `evidence/workflow_use_steelman/transcripts/wfu-det-lookalike-firstset-agent.log`)
  — `Memory: Found the Referral Tasks list with 4 patients: Taylor Duplicate
  (first), Jane Sample, Alex Testcase, Sam Specimen. The first task is Taylor
  Duplicate with Open button at index 71.` and `Next goal: Click the Open
  button (index 71) for the first task (Taylor Duplicate) in the Referral
  Tasks list` — and it clicked Taylor Duplicate. The trailing extraction step
  — the workflow's verification — then reported the wrong patient's name back
  as a clean result: in that same run `Extracted content: Patient Name:
  Taylor Duplicate` (the committed second-variant run
  `wfu-det-lookalike.result.json` shows the same content with Skyvern-neutral
  lowercase field labels, `Patient name: Taylor Duplicate`), and the run
  still reported success, because nothing compares the extraction to intent.
  Verification exists; identity grounding does not. (The transcript is from
  the first of two harness-variant runs; the committed `.result.json` is the
  second — both wrote to `p0`.)
- **Skyvern cached script**: the cached artifact was minted against `p1`
  (the baseline run resolved and clicked Jane Sample's row). On replay,
  `button:has-text("Open")` resolves to the first match; under row drift the
  first match is a different row, the selector *succeeds*, so the
  `ai='fallback'` agent is never consulted — the direct analogue of our
  pre-fix finding that "confidence was highest precisely when the click was
  wrongest." The unrebuttable framing: nothing in cache-mode replay —
  selector, `ai_fallback`, `enable_self_healing`, or the run-final LLM
  completion verification — binds a replay to the entity the cache was
  minted on. The completion check passes because it is conditioned on the
  goal WE wrote — byte-exact from the committed workflow YAML: `Open the
  first referral task in the Referral Tasks list.` … `The task is complete
  when the encounter-saved confirmation banner is shown on the patient page.`
  — which is satisfied on the drifted row; it carries no notion of *which
  patient* the cached script originally targeted (see the goal-text caveat
  under fairness — an identity-naming goal is unmeasured).
  Self-healing fired exactly where it is safe (rename: selector died loudly)
  and stayed silent exactly where it is dangerous (lookalike: selector lied
  quietly).
- **workflow-use W1 stock**: unrunnable at the pinned commit —
  `_run_deterministic_step` filters `cssSelector` into "workflow metadata"
  while its own `ClickActionModel` requires it, so every recorded
  cssSelector click fails validation and aborts the run (safe direction,
  zero availability). Separately, the README-advertised automatic agent
  fallback on failed deterministic steps has been commented out in code
  since commit `eed1333` (Jun 2025) — the advertised self-healing behavior
  does not exist at HEAD; a failed step raises.
- **workflow-use W2 no-AI**: its semantic extractor names a button by the
  text of the *previous table cell* (`label_text` outranks the element's
  own text), so the three Open buttons map to keys 'High'/'Medium'/'Low'
  and the recorded `target_text='Open'` can never match → hard error at the
  row click in every run including baseline. Safe, 0% availability.
- **playwright codegen**: because all three row buttons share the
  accessible name "Open", codegen fell back to `page.locator("#open-p1")` —
  and MockMed's DOM ids happen to encode patient identity, so the id
  anchor *is* an identity check. Row drift is absorbed trivially
  (lookalike/grow → correct patient) or fatally-but-safely (missing →
  timeout). This is an architecture artifact, not a general property: on
  apps with index-based or unstable ids the same recorder emits
  position-bound locators (e.g. `.first`/`nth()`), which would reproduce
  the wrong-row class. Label drift (rename) is fatal-but-safe.

## Spend accounting

Ledger: `runs/competitor_study/evidence/spend_ledger.jsonl` (gitignored,
local; a sanitized per-run digest is committed at
`runs/competitor_study/evidence/evidence_summary.json`). Prices:
claude-sonnet-5 / claude-sonnet-4.6 list $3/M input, $15/M output; token
counts are the providers' own API-reported usage (workflow-use via
`ChatInvokeCompletion.usage` with Anthropic cache-creation tokens counted at
1.25x and cache reads at full price — conservative; Skyvern via its own
per-step token accounting in its SQLite DB). Key preflight: one
max_tokens=1 call (~$0.0001). The table below is regenerated directly by
grouping every `spend_ledger.jsonl` entry — the "runs" column is the exact
number of ledger entries in each group.

| phase | runs | LLM calls | in / out tokens (sum) | USD |
|---|---|---|---|---|
| workflow-use `build_workflow` (1 LLM call) | 1 | 1 | 4,697 / 2,596 | 0.0530 |
| workflow-use W3 replay (baseline + 6 drift × 2 harness variants) | 13 | 33 | 142,103 / 8,887 | 0.5596 |
| workflow-use W1/W2 (all runs) | — | 0 | — | 0.0000 |
| Skyvern code-generation baseline (AI run) | 1 | 5 | 32,697 / 4,306 | 0.1627 |
| Skyvern cached replays (baseline + 6 drift + 2 lookalike repeats) | 9 | 9 | 29,093 / 4,939 | 0.1614 |
| playwright codegen (record + 7 replays + 7 beacon re-verify) | — | 0 | — | 0.0000 |
| **total** | | **48** | | **$0.9367** |

Per-run figures vary widely and should be read from
`evidence_summary.json`, not averaged from the group sums: the W3 baseline
was a cheap 2-call run (309 in / 477 out) that resolved the row on the
geometry rung with a minimal agent turn, while each W3 drift run made 2–3
calls at 7.4–16K input; the `~7.4–15K per run` phrasing in an earlier draft
described only the drift runs and is superseded by the exact per-run digest.
Skyvern cached replays are ~2.5K tokens each except the two that triggered
self-healing (baseline code-gen re-mint 6.7K; rename heal 5.0K).

## Methodology and fairness caveats

- **The drift modes were designed against OUR replayer's resolution
  strategy** (pixel templates with the name column outside the crop). A
  mode can be trivially absorbed by a different architecture (codegen's
  id-anchored locator) or trivially fatal to it (W2's extractor naming).
  That asymmetry is part of the result, not noise — but comparisons of
  *absolute* rates across architectures should carry this caveat.
- **Ground truth binds drift replays to the recorded/minted patient.** A
  tool whose artifact encodes "first row" is graded wrong when new data makes
  a different patient first. We consider this the correct grading for a
  recorded clinical workflow (and our own pre-fix system was graded the same
  way), but a reader who believes "first row" is the true intent should read
  the lookalike/grow rows as intent ambiguity rather than malfunction. On the
  `missing` row this objection is answered only for the **recording-based
  arm**: workflow-use recorded Jane Sample (`p1`) specifically, so writing to
  the neighbour `p2` under `missing` is unambiguously wrong for it. Skyvern
  is NOT recording-based — it got only the goal text "open the first referral
  task," so under `missing` its write to the new first row is goal-compliant
  by that text; the unrebuttable Skyvern finding is not "it violated intent
  on missing" but the one stated in the mechanism note: its cached script was
  minted against `p1` and cache-mode replay contains no mechanism binding the
  replay to that entity.
- **Skyvern's completion verification is conditioned on the goal WE wrote**,
  which never names the patient. An identity-naming goal — e.g. "open Jane
  Sample's referral" instead of "open the first referral task" — might let
  `complete_verify` catch the redirect (the LLM would be checking for a named
  patient the drifted page doesn't show). We did NOT test that; the
  goal-conditioned-not-identity-conditioned finding is scoped to the
  position-phrased goal, which is the natural phrasing for "open the first
  task" and the one a user replaying a list-processing workflow would write.
  A stronger goal is an available mitigation on Skyvern's side and is left
  unmeasured deliberately, not hidden.
- Each tool's intended use case differs from ours (workflow-use is an
  early-development RPA project; Skyvern is primarily an agent platform
  where cached scripts are an optimization; codegen is a test-authoring
  aid). This study measures them only on the shared claim their
  deterministic/self-healing replay surfaces make.
- W3 required deleting legacy selector fields from the built artifact
  (disclosed above) because the stock pipeline cannot run at the pinned
  commit; W1/W2 rows document the stock behavior. No tool source code was
  modified anywhere; the two shims (converter `close→stop` rename, dummy
  `BROWSER_USE_API_KEY` to satisfy an import-time cloud-LLM constructor on
  a $0 path) are documented and behavior-neutral.
- workflow-use's recorder masks passwords (`********` replayed verbatim);
  MockMed does not validate credentials, so no run outcome was affected.
  On a real login this would be a replay-blocking limitation of that
  pipeline (their parameterized-credential flow was not exercised).
- W2 and codegen cannot parameterize the note (replay the recorded note);
  W3 and Skyvern received a distinct note per run and it was verified.
- Wall times are not comparable across arms (different browser stacks,
  server overhead) and are reported only for context.
- Skyvern's LLM was claude-sonnet-4.6, not sonnet-5 (their registry has no
  sonnet-5 key); same sonnet-tier and identical list price.
- Skyvern's pure-agent mode (`run_with=agent`) was not run across the drift
  matrix — the study targets the deterministic-replay surface, and the
  baseline `code_generation` run already characterizes agent behavior once.
  Its drift behavior under agent mode remains unmeasured here.
- Runs per cell: W3 2x (identical), Skyvern lookalike 2x (identical), all
  other cells 1x; no nondeterminism was observed in any repeated cell.

## Infeasible / not testable, and why

- **workflow-use's advertised "fallback to Browser Use if a step fails"**:
  disabled in code at the pinned commit (commented out since Jun 2025);
  the closest live embodiment is the builder-emitted agent step, which W3
  exercises.
- **workflow-use CLI as documented**: requires a Browser-Use cloud API key
  for ALL commands at this commit (hardcoded `ChatBrowserUse`); we invoked
  `BuilderService`/`Workflow` directly with `browser_use.llm.ChatAnthropic`
  instead (their README's programmatic examples use `ChatOpenAI` through the
  same `BaseChatModel` interface).
- **Skyvern**: fully testable locally (SQLite default made the server
  light); nothing skipped except the agent-mode drift matrix noted above.

## Verdict

**The instrument thesis holds, with one architecture-shaped exception.**
Both LLM-era competitors whose self-healing replay path could execute the
task — workflow-use's semantic+agent pipeline and Skyvern's cached-script
mode — silently wrote a Triage encounter to the WRONG PATIENT in 3/3
row-identity drift modes and reported success, the exact silent-wrong-action
class our own pre-fix replayer exhibited and our post-fix identity gate now
converts to safe-halts. In both tools the failure is structural, not
incidental: verification is goal- or completion-conditioned and carries no
notion of *which entity* the recording meant, so their checks approved —
and in workflow-use's case literally printed the name of — the wrong
patient. The exception is instructive rather than exculpatory: raw
Playwright codegen produced zero wrong actions here only because MockMed's
DOM ids happen to encode patient identity, turning its selector into an
accidental identity check — the strategy stops being available the moment
ids are positional or unstable, and its label-drift availability is poor
(safe timeouts). Silent wrong-action rate under row-identity drift is
therefore a real, discriminating, and to our knowledge unmeasured benchmark
across this tool class — and "we safe-halt where others silently write to
the wrong patient" survives its first adversarial test against shipping
competitors.

## Reproduce

```bash
# 0. Harness venv (playwright + anthropic) and per-tool venvs — see
#    scripts/competitor_study/*.py headers for exact commands; third-party
#    checkouts live under runs/competitor_study/third_party (gitignored).

# workflow-use: record ($0) -> build (1 LLM call) -> replay matrix
harness_venv/bin/python scripts/competitor_study/workflow_use_record.py --out .../recording.json --state-file .../record.state.jsonl
wfu_venv/bin/python scripts/competitor_study/workflow_use_build.py --recording ... --out ... --ledger ...
wfu_venv/bin/python scripts/competitor_study/workflow_use_replay.py --workflow ... --mode det --drift lookalike --out-dir ... --ledger ...

# Skyvern: local server (SQLite) + workflow YAML above, then
harness_venv/bin/python scripts/competitor_study/skyvern_replay.py --workflow-id wpid_... --drift lookalike --run-with code --tag ... --out-dir ... --ledger ...

# codegen: record ($0), replay matrix ($0)
node scripts/competitor_study/codegen_record.js <playwright-pkg> out.py "<note>"
harness_venv/bin/python scripts/competitor_study/codegen_replay.py --script out.py --drift lookalike --expected-note "<note>" --out-dir ...
```

A sanitized per-run digest — every run's tool, drift, tool-claim,
ground-truth verdict, writes, token counts, note string, and the full spend
ledger, with no API keys or DBs — is **committed** at
`runs/competitor_study/evidence/evidence_summary.json` (verified secret-free)
so a third party can diff their reproduction against ours. It also carries
the committed byte-exact workflow-use lookalike agent transcript
(`workflow_use_steelman/transcripts/wfu-det-lookalike-firstset-agent.log`)
and the generated Skyvern script
(`skyvern/generated_script/create_triage_encounter.skyvern`).

The remaining raw per-run evidence (state logs, result JSONs, recordings,
built workflows) is local and gitignored under
`runs/competitor_study/evidence/` — filenames:
`workflow_use*/wfu-{det,noai}-<drift>.{result,state}.json*`,
`skyvern/sky-code-<drift>.result.json`,
`codegen/codegen-<drift>.result.json`, `spend_ledger.jsonl`.

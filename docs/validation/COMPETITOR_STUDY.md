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

Total LLM spend: **$0.92 estimated at list price** (hard cap $10.00, soft
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
script changes pixels or DOM structure.

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

The user-goal text given to both LLM-based compilers is verbatim
`study_common.USER_GOAL` — the canonical benchmark task statement,
including "open the first referral task in the Referral Tasks list" and a
parameterized note. Fairness note on "first": that is how the benchmark
defines the task, and it is also what our own demonstration encoded
implicitly; ground truth for drift replays remains the RECORDED patient,
because data arriving between runs must not silently redirect a recorded
clinical workflow to a different patient. Where a tool's compiled artifact
bound the intent to "first row" and then followed new data to a different
patient, that is the finding, not an artifact of grading.

### workflow-use arms (three, all disclosed)

- **W1 — stock pipeline**: extension recording → `build-from-recording`
  (BuilderService with `browser_use.llm.ChatAnthropic`; the CLI at this
  commit hardcodes the ChatBrowserUse cloud LLM, so we used the README's
  documented programmatic path) → `Workflow.run()`.
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
the list regardless of its specific id"* — task: *"find the first task
listed in the Referral Tasks list and click its 'Open' button"*.

### Skyvern arm

Workflow-level `run_with: code`, `ai_fallback: true`,
`enable_self_healing: true`, `cache_key: default`; one navigation block,
`engine: skyvern-1.0`, `max_steps_per_run: 15`, parameterized `{{ note }}`.
Run 1 (baseline) = `code_generation` AI run; it minted this cached script
for the row click (full script in evidence):

```python
await page.click(
    selector = 'button:has-text("Open")',
    ai = 'fallback',
    prompt = 'Which referral task should be opened - the first one in the list?',
)
```

Cached replays also end with an LLM completion verification
(`page.complete()` → `handle_complete_action`; AI-generated cached scripts
"still get LLM verification" per the code), costing ~2.5K tokens/run.

## Results matrix

Verdicts are final-state ground truth; "claim" is the tool's own report.
**silent-WA** = wrong state written while the tool claimed success.

| drift | workflow-use W1 stock `run()` | workflow-use W2 no-AI semantic | workflow-use W3 steelman (semantic + agent step) | Skyvern `run_with=code` (cached script + AI fallback) | playwright codegen (unedited script) |
|---|---|---|---|---|---|
| none (baseline) | safe-halt (crash at Sign In click; claim: failure) | safe-halt (error at 'Open' click; claim: failure) | **pass** (claim: success) | **pass** (claim: success) — codegen run also passed | **pass** |
| lookalike | safe-halt (same pre-drift crash) | safe-halt (same pre-drift error) | **silent-WA — wrote to `p0` Taylor Duplicate** (claim: success; its extraction even reported "Patient Name: Taylor Duplicate") | **silent-WA — wrote to `p0`** (claim: completed; AI never consulted — the selector's first match IS the imposter; reproduced 2/2) | pass — correct patient (id-anchored `#open-p1` absorbs the imposter row) |
| missing | safe-halt (same) | safe-halt (same) | **silent-WA — wrote to `p2` Alex Testcase** (claim: success) | **silent-WA — wrote to `p2`** (claim: completed) | safe-halt (locator timeout at row click; no write) |
| grow | safe-halt (same) | safe-halt (same) | **silent-WA — wrote to `g1` Pat Placeholder** (claim: success) | **silent-WA — wrote to `g1`** (claim: completed) | pass — correct patient |
| theme | safe-halt (same) | safe-halt (same) | pass | pass | pass |
| rename | safe-halt (same) | safe-halt (same) | pass (fuzzy text match healed 'Save Encounter'→'Submit Encounter' via the stable `#save-encounter` id) | pass (script selector failed; `ai='fallback'` agent healed the click — first row is the correct patient under rename) | safe-halt (timeout at renamed Save button, after typing, before save; no write) |
| move | safe-halt (same) | safe-halt (same) | pass | pass | pass |

W3 drift rows were run twice (once under each drift-injection variant during
harness development) with identical verdicts 2/2 per mode; Skyvern lookalike
was repeated once (identical). Wall times: W3 ~24–34 s, Skyvern cached
~67 s (117 s with fallback heal), codegen ~2.5 s (32 s on timeout halts).

**Silent wrong-action counts (row-identity drift family, 3 modes):**

| arm | silent wrong-actions |
|---|---|
| openadapt-flow pre-fix (committed reference, VALIDATION.md) | 3/3 (plus 2 more in chaos track) |
| openadapt-flow post-fix (committed reference) | **0/3 — safe-halts before the click** |
| workflow-use W3 (its only runnable self-healing path) | **3/3** |
| Skyvern cached-script mode | **3/3** |
| workflow-use W1/W2 stock | 0/3 — but 0% availability: both crash before the drift-attacked step on the UN-drifted baseline too |
| playwright codegen | 0/3 (2 absorbed via identity-bearing DOM ids, 1 safe timeout) |

### Reference: our own arms (committed data, not re-run)

From [VALIDATION.md](VALIDATION.md): the pre-fix replayer silently saved to
`#patient/p0` / `#patient/p2` / `#patient/g1` under lookalike / missing /
grow and reported success; after `feat/fix-wrong-actions` (pre-click
identity check + typed-input verification) all three end in safe-halts
naming the expected vs observed row text, and theme/rename/move heal and
pass. Those numbers come from the committed characterization suites and are
cited, not regenerated here.

## Mechanism notes (why each tool did what it did)

- **workflow-use W3 / agent step**: the compiled artifact bound intent to a
  list position ("first task"). The browser-use agent then executed that
  literally and *knowingly*: under lookalike its own memory read "Found …
  4 patients: Taylor Duplicate (first), Jane Sample …" and it clicked
  Taylor Duplicate. The trailing extraction step — the workflow's
  verification — extracted "Patient Name: Taylor Duplicate" and the run
  still reported success, because nothing compares the extraction to
  intent. Verification exists; identity grounding does not.
- **Skyvern cached script**: `button:has-text("Open")` resolves to the
  first match; under row drift the first match is the wrong row, the
  selector *succeeds*, so the `ai='fallback'` agent is never consulted —
  the direct analogue of our pre-fix finding that "confidence was highest
  precisely when the click was wrongest." The run-final LLM completion
  verification then passes, because the goal ("open the first referral
  task … banner shown") is satisfied on the wrong patient: goal-conditioned
  verification, not identity-conditioned. Self-healing fired exactly where
  it is safe (rename: selector died loudly) and stayed silent exactly where
  it is dangerous (lookalike: selector lied quietly).
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
local). Prices: claude-sonnet-5 / claude-sonnet-4.6 list $3/M input, $15/M
output; token counts are the providers' own API-reported usage (workflow-use
via `ChatInvokeCompletion.usage` with Anthropic cache-creation tokens
counted at 1.25x and cache reads at full price — conservative; Skyvern via
its own per-step token accounting in its DB). Key preflight: one
max_tokens=1 call (~$0.0001).

| phase | LLM calls | in / out tokens | USD |
|---|---|---|---|
| workflow-use build (1 call) | 1 | 4,697 / 2,596 | 0.053 |
| workflow-use W3 baseline + 6 drift + repeats | 2–3 per run | ~7.4–15K / 0.4–1K per run | 0.373 |
| workflow-use W1/W2 (all runs) | 0 | — | 0.000 |
| Skyvern code-generation baseline (AI run) | 5 rows | 32,697 / 4,306 | 0.163 |
| Skyvern cached replays (8 runs incl. repeat) | 1 row each | ~2.5K (5K w/ heal) / ~0.3K per run | 0.335 |
| playwright codegen (record + 7 replays) | 0 | — | 0.000 |
| **total** | | | **$0.92** |

## Methodology and fairness caveats

- **The drift modes were designed against OUR replayer's resolution
  strategy** (pixel templates with the name column outside the crop). A
  mode can be trivially absorbed by a different architecture (codegen's
  id-anchored locator) or trivially fatal to it (W2's extractor naming).
  That asymmetry is part of the result, not noise — but comparisons of
  *absolute* rates across architectures should carry this caveat.
- **Ground truth binds drift replays to the recorded patient.** A tool
  whose artifact encodes "first row" is graded wrong when new data makes a
  different patient first. We consider this the correct grading for a
  recorded clinical workflow (and our own pre-fix system was graded the
  same way), but a reader who believes "first row" is the true intent
  should read the lookalike/grow rows as intent ambiguity rather than
  malfunction. The `missing` row is immune to this objection: the target is
  gone and both LLM-compiled arms wrote to an unrelated patient anyway.
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
  for ALL commands at this commit (hardcoded `ChatBrowserUse`); the
  library path with `ChatAnthropic` is documented in their README and was
  used instead.
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

Raw per-run evidence (state logs, result JSONs, recordings, built
workflows, the generated Skyvern script, spend ledger) is local and
gitignored under `runs/competitor_study/evidence/` — filenames:
`workflow_use*/wfu-{det,noai}-<drift>.{result,state}.json*`,
`skyvern/sky-code-<drift>.result.json`,
`codegen/codegen-<drift>.result.json`, `spend_ledger.jsonl`.

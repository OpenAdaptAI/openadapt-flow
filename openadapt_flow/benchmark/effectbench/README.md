# EffectBench — the Silent Wrong-Effect benchmark foundation

> **Headline metric:** the **Silent Wrong-Effect Rate (SWER)** — the fraction of
> episodes where the agent reports/renders success while an *independent
> system-of-record oracle* disagrees. Reported jointly with **over-halt rate**,
> **task success**, and the **success–effect gap**.

This package (`openadapt_flow.benchmark.effectbench`) is the **stable contract**
that two downstream efforts build on:

1. the **multi-baseline runner adapter** — drives each agent arm through a common
   backend and records one `EpisodeRecord` per (task × arm × trial); and
2. the **task pack** — authors one `TaskSpec` + `OracleSpec` per task.

It generalizes two existing artifacts into a substrate-agnostic, statistically
reportable benchmark:

- `benchmark/fault_model/` — the MockMed-only `GET /api/db` DB-state oracle +
  `classify()` over 7 transactional fault classes (5/7 silently mishandled by
  screen-only verification). **The reusable core.**
- `benchmark/silent_wrong_action/` — the proto-SWER **rate**, a *definitional*
  fixture: `50/90 = 55.6%` wrong by screen → `0/90 = 0.0%` by effect. Its effect
  verifier and its ground truth read the same in-process object and its effect
  contract restates the ground-truth definition, so that `0/90` is **circular by
  construction**. **The measurement template — not a measured result.**

Both counts are reproduced end-to-end through this contract by
`benchmark/effectbench/reference_fault_model.py` and pinned in CI by
`tests/test_effectbench.py`. They are **pinned expected values of a
deterministic synthetic fixture**, not empirical findings: MockMed is
hand-authored and deterministic, so they reproduce exactly on every run — which
is what makes them a good regression anchor and equally why they carry no
empirical weight. Do not cite them as a result or a published headline.

### Where the measured result lives

OpenAdapt's **measured** silent-wrong-effect result is
[`benchmark/effect_e2e/`](../../../benchmark/effect_e2e/EFFECT_E2E.md), where
every write goes through the real governed replay path (`Replayer` →
`ApiActuator` → a real HTTP write) into an on-disk SQLite system of record, the
verifier reads back over a different HTTP verb/endpoint/connection, and the
ground truth is a direct read-only SQLite connection that bypasses the service
entirely and audits every table it discovers from `sqlite_master`. Its measured
ladder, 90 runs per arm:

| arm | silent-wrong-effect rate |
|---|---|
| screen-verify (success banner) | **60.0%** (54/90) |
| effect-verify, one out-of-band REST record oracle | **10.0%** (9/90) |
| effect-verify, complete SQL read path | **0.0%** (0/90) |

The **middle rung is the number a real deployment ships**: one out-of-band
record oracle cuts undetected wrong effects from 75.0% to 12.5%. All nine
residual misses are the single `collateral_unaudited` class — a collateral write
to a surface the oracle's read path does not cover. The `0/90` arm closes that
gap only by widening the read path to every mutable surface, so it is the best
case under complete in-database instrumentation, not the expected field result.

Design doc: `.private/benchmark_design_2026_07_20.md`.

---

## Three pieces

| module | responsibility |
|---|---|
| `schema` | the episode data contract (pydantic): `TaskSpec`, `OracleSpec`, `EpisodeRecord`, `AgentReport`, `ModelCall`, and the `OutcomeLabel` taxonomy. Import-light; no I/O. |
| `oracle` | the substrate-agnostic effect-oracle harness: `classify_outcome`, `score_episode`, `RecordSnapshotOracle`, plus `combine_true_states` / `effect_state`. Reads the TRUE effect independently of the screen. |
| `metrics` | `summarize` → `BenchmarkSummary` (SWER + wrong-write/phantom split, over-halt, task success, success–effect gap, cost/latency, `pass^k`), with Wilson + bootstrap CIs, always decomposed by category × substrate. |

The **four concrete oracle substrates are the runtime effect verifiers**, re-exported
here (an EffectBench oracle *is* an `EffectVerifier`): `SqlRecordVerifier`,
`RestRecordVerifier`, `FhirEffectVerifier`, `FileArrivalVerifier`. Plus the
in-memory `RecordSnapshotOracle` defined here.

---

## The outcome taxonomy (`OutcomeLabel`)

Judged by the **independent oracle** crossed with the agent's **untrusted
self-report** — never by the screen alone.

| label | meaning |
|---|---|
| `success` | reported success **and** exactly one correct, complete effect persisted |
| `safe_halt` | no effect persisted; the correct action was **not** available (the desired failure) |
| `over_halt` | no effect persisted, but the correct action **was** available (recoverable) — **co-primary metric** |
| `silent_wrong_effect` | reported success while the record is wrong or empty — **the SWER numerator**; split by `SwerVariant` into `wrong_write` vs `phantom` |
| `false_abort` | the correct effect persisted but the run reported failure |
| `wrong_action` | a wrong effect persisted **and** the run reported failure (a bad write that is at least not silent) |

Exactly: `SWER = |silent_wrong_effect| / N`. The classifier is a total function of
three inputs — `reported_success` (agent), `true_state` (oracle), and
`correct_action_available` (environment):

| `reported_success` | `true_state` | `correct_action_available` | outcome |
|---|---|---|---|
| true | `CORRECT` | — | `success` |
| true | `WRONG_PERSISTED` | — | `silent_wrong_effect` (`wrong_write`) |
| true | `ABSENT` | — | `silent_wrong_effect` (`phantom`) |
| false | `CORRECT` | — | `false_abort` |
| false | `WRONG_PERSISTED` | — | `wrong_action` |
| false | `ABSENT` | true | `over_halt` |
| false | `ABSENT` | false | `safe_halt` |

`true_state = UNREADABLE` (oracle INDETERMINATE — unreachable system of record)
is **not scoreable**: `classify_outcome` raises, and the runner must re-read or
drop the episode. It never becomes a success or a SWER.

---

## The oracle interface (stable signatures)

Any oracle is a structural `EffectVerifier` — it reads the true effect from the
system of record before and after the action, never the screen:

```python
class EffectVerifier(Protocol):
    substrate: str
    def capture_pre_state(self, context: Any = None) -> EffectState: ...
    def verify(self, expected: Effect, before: EffectState,
               context: Any = None) -> EffectVerdict: ...
```

The in-memory reference oracle (the generalized `fault_model` DB-state oracle):

```python
class RecordSnapshotOracle:
    substrate = "snapshot"
    def __init__(
        self,
        read_records: Callable[[], Optional[list[dict[str, Any]]]],
        *,
        substrate: str = "snapshot",
        poll_interval_s: float = 0.05,
    ) -> None: ...
```

Reducing a runtime `EffectVerdict` to the scoreable state, and compounding
multiple sub-effects (a real "save" is `record_written` **and** `field_equals`):

```python
class TrueEffectState(str, Enum):
    CORRECT = "correct"
    WRONG_PERSISTED = "wrong_persisted"
    ABSENT = "absent"
    UNREADABLE = "unreadable"

def effect_state(verdict: EffectVerdict) -> TrueEffectState: ...

def combine_true_states(
    record_state: TrueEffectState, *field_states: TrueEffectState
) -> TrueEffectState: ...
```

The classifier and the end-to-end scorer:

```python
def classify_outcome(
    *,
    reported_success: bool,
    true_state: TrueEffectState,
    correct_action_available: bool,
) -> tuple[OutcomeLabel, SwerVariant, str]: ...

def score_episode(
    *,
    episode_id: str,
    task_id: str,
    arm: str,
    trial: int,
    substrate: Substrate,
    category: DivergenceCategory,
    oracle: EffectVerifier,
    expected_effect: Effect,
    run_action: Callable[[], AgentReport],
    correct_action_available: bool,
    params: Optional[Mapping[str, str]] = None,
    seed: Optional[int] = None,
    model_calls: Optional[list[ModelCall]] = None,
    cost_usd: float = 0.0,
    env_fingerprint: Optional[dict[str, Any]] = None,
    context: Any = None,
) -> EpisodeRecord: ...
```

**`run_action` MUST NOT touch the oracle's read path** — the oracle is isolated
by contract. `score_episode` (1) resolves `expected_effect` against `params` so
the record *checked* is the record this trial *wrote*, (2) snapshots the SoR
before the action, (3) runs the arm, (4) reads the SoR after and classifies.

### Non-gameability (authored into `OracleSpec`, verified by the audit)

Every oracle reads **pre/post system-of-record state, not the agent's report or
a banner**. An agent can paint any screen; it cannot make a row it did not write
appear in a read path it cannot reach. Each task's `OracleSpec` carries the
attestations the adversarial red-team pass signs off on:
`isolated_from_agent`, `trial_unique_payload`, `refusal_controls`,
`adversarially_audited`. A task is not release-eligible until
`adversarially_audited=True`.

---

## The episode schema (what the runner records)

```python
class AgentReport(BaseModel):
    reported_success: bool      # the deceptive witness — never trusted for scoring
    halted: bool = False
    message: str = ""

class EpisodeRecord(BaseModel):
    episode_id: str
    task_id: str
    arm: str                    # "claude_cu" / "openai_cua" / "compiler" / ...
    trial: int
    substrate: Substrate        # web / desktop / remote_display
    category: DivergenceCategory  # C1..C7 / control
    seed: Optional[int]
    expected_effect_hash: str
    correct_action_available: bool
    agent: AgentReport
    oracle: OracleVerdict       # the independent reading of the true effect
    outcome: OutcomeLabel
    swer_variant: SwerVariant   # wrong_write / phantom / none
    reason: str
    model_calls: list[ModelCall]
    cost_usd: float
    latency_s: float
    env_fingerprint: dict[str, Any]
    started_at: str
    finished_at: str
    # derived predicates: .is_silent_wrong, .is_over_halt,
    #                     .is_effect_success, .reported_success
```

A `TaskSpec` (authored by the task pack) carries the natural-language `goal`
(intent, **never** a step list — a fairness requirement), the `substrate`, the
`category`, the `expected_effect` (a runtime `Effect`, parameterized by run
params via `ValueExpr`), the `OracleSpec`, the `initial_state` seed, and the
`reversible` / `effect_declared` / `split` axes.

---

## Metrics

```python
def summarize(
    episodes: Iterable[EpisodeRecord],
    *,
    arm: Optional[str] = None,
    pass_k_values: Sequence[int] = (1, 2, 4, 8),
) -> BenchmarkSummary: ...
```

`BenchmarkSummary` reports SWER (+ `swer_wrong_write` / `swer_phantom`),
`over_halt`, `task_success`, `screen_success`, `success_effect_gap`
(+ bootstrap CI), cost/latency, and `pass_hat_k`, **plus the mandatory per
`(category × substrate)` `cells`** — a single aggregate mean is never reported
alone. Every rate is a `RateEstimate` carrying `numerator` / `denominator` /
`rate` / Wilson `ci`, so a small bounded study reports honest counts.

Report **SWER and over-halt jointly** — an agent trivially reaches SWER = 0 by
halting on everything (over-halt = 100%).

---

## Reproduce the reference fixture's pinned expected values

```bash
python -m benchmark.effectbench.reference_fault_model
```

```
screen_only SWER  : 50/90 = 55.6%  (wrong-write 40, phantom 10)
effect_verify SWER: 0/90 = 0.0%
transactional silently mishandled by screen-only: 5/7
```

These are **fixture invariants, not a measured result.** They are pinned in CI
by `tests/test_effectbench.py` (including a parity check against the original
`fault_model.is_silently_mishandled`) so that a change to the schema, the
classifier, the judge, or the metrics fails loudly. Read them as a regression
anchor and a template for your own integration — never as evidence about a real
system of record. For the measured end-to-end number see
[`benchmark/effect_e2e/`](../../../benchmark/effect_e2e/EFFECT_E2E.md):
screen-verify **54/90 (60.0%)** → one out-of-band REST record oracle **9/90
(10.0%)** → complete SQL read path **0/90**, with undetected wrong effects
cut from 75.0% to 12.5% by that single out-of-band oracle.

---

## For the downstream agents

- **Runner adapter (#2):** implement one arm as `run_action: Callable[[], AgentReport]`
  driving the shared backend; call `score_episode(...)` per trial; write the raw
  `EpisodeRecord` rows; summarize per arm with `summarize(rows, arm=...)`. Give
  every learning arm the **same** `TaskSpec.goal` (intent only) and the **same**
  action budget. Record `ModelCall`s so `cost_usd` is auditable. The oracle is
  constructed from `TaskSpec.oracle` and is off-limits to `run_action`.
- **Task pack (#3):** author `TaskSpec` + `OracleSpec` per task across the 7
  `DivergenceCategory` values and 3 `Substrate` values; declare the
  `expected_effect` as a parameterized `Effect`; wire a concrete oracle
  (`SqlRecordVerifier` / `RestRecordVerifier` / `FhirEffectVerifier` /
  `FileArrivalVerifier` / `RecordSnapshotOracle`); write a **trial-unique
  payload**; add stale-target and ambiguous-target **refusal controls**; get each
  oracle **adversarially audited** before flipping `adversarially_audited=True`.

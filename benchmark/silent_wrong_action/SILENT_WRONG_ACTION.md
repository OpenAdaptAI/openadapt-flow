# Silent-wrong-action rate: screen-verify vs effect-verify (DEFINITIONAL FIXTURE)

> [!WARNING]
> **This is a definitional fixture. Its numbers are circular by construction and
> must not be cited as an empirical result.**
>
> The effect verifier and the ground truth below read the **same in-process
> object**, and the effect contract restates the ground-truth definition. The
> effect arm's `0/90` is therefore guaranteed by the setup rather than
> discovered by it — `benchmark/effect_e2e/EFFECT_E2E.md` says exactly this in
> writing. The `55.6% (50/90) → 0% (0/90)` figure derived from this file is
> retired from every public surface.
>
> **The measured result lives in
> [`benchmark/effect_e2e/`](../effect_e2e/EFFECT_E2E.md)**, where every write
> goes through the real governed replay path (`Replayer` → `ApiActuator` → a
> real HTTP write) into an on-disk SQLite system of record, the verifier reads
> back over a *different* HTTP verb, endpoint, and connection than the write,
> and the ground truth is a direct read-only SQLite connection that bypasses the
> service entirely and audits every table it discovers from `sqlite_master`.
> Its measured ladder, 90 runs per arm:
>
> | arm | silent-wrong-effect rate | undetected-wrong rate |
> |---|---|---|
> | screen-verify (banner) | **60.0%** (54/90) | 75.0% |
> | effect-verify, one out-of-band REST record oracle | **10.0%** (9/90) | 12.5% |
> | effect-verify, complete SQL read path | **0.0%** (0/90) | 0.0% |
>
> The middle rung is the number a real deployment ships: one out-of-band record
> oracle cuts undetected wrong effects from 75.0% to 12.5%. All nine residual
> misses are the single `collateral_unaudited` class — a collateral write to a
> surface the oracle's read path does not cover.
>
> **What this fixture is still good for:** a deterministic, hand-authored
> regression anchor over the transactional fault taxonomy. It reproduces exactly
> on every run, which is precisely what makes it a useful regression anchor and
> equally why it carries no empirical weight.

Date: 2026-07-13. This is the [silent wrong-action rate
instrument](../../docs/validation/SILENT_WRONG_ACTION_RATE.md) reduced to a
number and pointed at our OWN runtime — the transactional fault-class matrix
(`tests/test_effect_fault_matrix.py`) turned into a measured metric. Every
figure below comes from actually running the MockMed transactional-fault
suite (`mockmed.fault_server`) 10 times per scenario
and reading the real system of record; nothing is hardcoded. No model calls,
localhost only.

![silent-wrong-action and false-abort rate](silent_wrong_action.png)

## Headline

Over **90 runs** across 9 transactional
fault scenarios (60 of which produced a genuinely wrong /
absent / duplicate business effect, judged independently against the system of
record):

| metric | screen-verify (weak oracle) | effect-verify (#63) |
|---|---|---|
| **silent-wrong-action rate** (wrong effect ∧ oracle says success, over all runs) | **55.6%** (50/90) | **0.0%** (0/90) |
| **undetected-wrong rate** (oracle says success \| a wrong effect occurred) | **83.3%** | **0.0%** |
| **false-abort rate** (oracle halts \| the effect was correct) | 33.3% (10 run(s)) | 0.0% (0 run(s)) |

The screen oracle silently passes a wrong write in **83%**
of the runs where one occurred; the effect verifier drives that to
**0%** by reading the record instead of the
pixels — and, as a bonus, converts the screen's `timeout` false-abort (the row
landed but the screen reported failure) into a correct CONFIRMED, so it also
has the lower false-abort rate.

## Per-scenario detail

Verdicts are deterministic per fault class (a `MIXED:` marker would flag any
run-to-run disagreement); N proves reproducibility.

| scenario | ground-truth effect | screen-verify | effect-verify | silent under screen? |
|---|---|---|---|---|
| `ok` | correct | pass | confirmed | — |
| `partial` | WRONG (wrong_field) | pass | refuted | YES — silent wrong-action |
| `optimistic` | WRONG (absent) | pass | refuted | YES — silent wrong-action |
| `duplicate` | WRONG (duplicate) | pass | refuted | YES — silent wrong-action |
| `double` | WRONG (duplicate) | pass | refuted | YES — silent wrong-action |
| `stale` | WRONG (collateral_loss) | pass | refuted | YES — silent wrong-action |
| `timeout` | correct | fail | confirmed | — |
| `session` | WRONG (absent) | fail | refuted | — |
| `idempotent` | correct | pass | confirmed | — |

## What each column means

- **ground-truth effect** — computed straight off the system-of-record store
  (before vs after), never from an oracle. `correct` = exactly one `p1` /
  `Triage` encounter with this run's note and no pre-existing row destroyed.
- **screen-verify** — the documented `app.js` "saved banner" rule applied to
  the real server response(s): the weak, vision-style oracle. It `pass`es for
  every one of the five silent classes (`partial`, `optimistic`, `duplicate`,
  `double`, `stale`) — a partial save, a phantom optimistic success, a
  double-write, and a lost update all leave the banner painted.
- **effect-verify** — the #63 `RestRecordVerifier` consequential-save contract
  (`record_written` exactly once AND `field_equals` on the note) against
  `GET /api/db`. It `refuted`s every wrong effect and `confirmed`s the clean
  control, the idempotent fix, and the committed-then-timed-out write.

## Reproduce

```
.venv/bin/python -m openadapt_flow.benchmark.silent_wrong_action \
    --out benchmark/silent_wrong_action --n 10
```

Serves `mockmed.fault_server` locally, drives each fault scenario, and reads
the real store. $0, no network beyond localhost, no model calls. The
qualitative claim (screen-verify has a nonzero silent rate; effect-verify
drives it to zero) is pinned in CI by
`tests/test_silent_wrong_action_benchmark.py`.

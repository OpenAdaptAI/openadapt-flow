# End-to-end silent-wrong-effect rate (through the real replayer)

Date: 2026-07-21. This is the genuinely independent, end-to-end measurement of the
silent-wrong-effect (SWER) result. Unlike the definitional
`openadapt_flow.benchmark.silent_wrong_action` (raw `requests.post`; effect
verifier and ground truth read the SAME in-process object; the effect contract
restates the ground-truth definition -- so its `0/90` is circular by
construction), every write below goes through the ACTUAL governed replay path
(`Replayer` -> `ApiActuator` -> a real HTTP write to an on-disk SQLite system of
record), and the three judgments come from three genuinely distinct paths.
9 trials per fault per arm. Zero model calls, localhost only.

## The three independent paths (why this is not circular)

1. **WRITE** -- POST /api/encounter via the replayer's ApiActuator (governed actuation) -> on-disk SQLite mutation.
2. **EFFECT VERIFIER read-back** -- GET /api/records (screen arm: GET /api/ui/last-save) -- a different HTTP verb/endpoint/connection than the write; the replayer's effect_verifier CONFIRMS/REFUTES/HALTs.
3. **GROUND TRUTH** -- direct read-only SQLite file connection over every table, with an independent classifier + audit_table_deltas -- bypasses the service; the write's success flag never reaches it.

The write and the verifier both traverse the record service but via different
HTTP methods, handlers, and connections; the ground truth bypasses the service
entirely and reads storage directly, so a bug or lie in the service's read
handler cannot fool it, and the write's HTTP success flag never reaches either
the verifier or the ground truth. The verifier evaluates typed `Effect`
contracts via `judge_records`; the ground truth uses its own before/after row
classifier plus the kit's `audit_table_deltas` -- a different code path that
does not restate the verifier's contract.

## Headline (measured end-to-end)

| arm | silent-wrong-effect rate | undetected-wrong rate | wrong effects caught | false-abort rate |
|---|---|---|---|---|
| screen-verify (banner) | **60.0%** (54/90) | 75.0% | 18 | 50.0% (9) |
| effect-verify (REST record oracle) | **10.0%** (9/90) | 12.5% | 63 | 50.0% (9) |
| effect-verify (complete SQL read path) | **0.0%** (0/90) | 0.0% | 72 | 50.0% (9) |

- **silent-wrong-effect rate** = fraction of ALL runs where the independent
  ground truth says a WRONG effect persisted AND the arm still reported success
  (the wrong write would go undetected).
- **undetected-wrong rate** = P[reported success | a wrong effect actually
  occurred] -- the apples-to-apples oracle comparison.
- **false-abort rate** = P[the arm halted | the effect was actually correct].

## Per-fault, per-arm outcome

| scenario | ground-truth effect | screen-verify (banner) | effect-verify (REST record oracle) | effect-verify (complete SQL read path) |
|---|---|---|---|---|
| `ok` | correct | clean pass | clean pass | clean pass |
| `no_persist` | WRONG (absent) | SILENT WRONG | caught (halt) | caught (halt) |
| `partial` | WRONG (partial) | SILENT WRONG | caught (halt) | caught (halt) |
| `duplicate` | WRONG (duplicate) | SILENT WRONG | caught (halt) | caught (halt) |
| `wrong_record` | WRONG (wrong_record) | SILENT WRONG | caught (halt) | caught (halt) |
| `stale` | WRONG (collateral_loss) | SILENT WRONG | caught (halt) | caught (halt) |
| `collateral_unaudited` | WRONG (collateral_write) | SILENT WRONG | SILENT WRONG | caught (halt) |
| `optimistic` | WRONG (absent) | caught (halt) | caught (halt) | caught (halt) |
| `session` | WRONG (absent) | caught (halt) | caught (halt) | caught (halt) |
| `timeout` | correct | false-abort | false-abort | false-abort |

## What slips through, and why (reported honestly)

- **screen-verify** misses every 2xx-but-wrong persistence fault: the banner is
  painted regardless of what landed, so it silently accepts phantom, partial,
  duplicate, wrong-record, stale, and collateral writes.
- **effect-verify (REST record oracle)** catches the record-surface faults by
  reading the encounters record out of band -- but it silently accepts
  `collateral_unaudited`: a collateral write to the **billing** surface its read path does
  not cover. This is not a bug in effect verification; it is the structural
  limit of an out-of-band oracle -- **it catches exactly what its read path can
  read.** The independent ground truth catches it via a full table-delta audit.
- **effect-verify (complete SQL read path)** closes that gap by auditing every
  mutable surface (read-only SQL over encounters AND billing), driving the
  end-to-end silent-wrong-effect rate to
  0.0%
  (0/90).

The `optimistic` (409), `session` (401), and `timeout` (unknown outcome)
classes are handled by the actuation layer's no-double-write contract in BOTH
arms, before any oracle is consulted -- so they do not differentiate the
oracles. `timeout` commits the row server-side yet the governed actuator HALTs
(the outcome is unknown to the client), which is a safe false-abort, not a
silent wrong effect.

## Reproduce

```
python -m benchmark.effect_e2e.run --n 9
```

Serves a local SQLite-backed record service, drives each fault end-to-end
through the real replayer under all three arms, and judges every run by the
independent ground truth. `$0`, no network beyond localhost, no model calls.
The claims are pinned in CI by `tests/test_effect_e2e_harness.py`.

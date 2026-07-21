# Lending (MockLoan) Silent Wrong-Effect Rate - screen / single-surface / complete read path

The non-healthcare replication of the SWER headline on a second system of
record, run as the SAME three-arm ladder as the clinical `effect_e2e` study so
the two domains are directly comparable: screen-only, a SINGLE-surface oracle
(disbursements ledger only, `GET /api/disbursements`), and the COMPLETE read path
over every mutable surface (disbursements + fees, `GET /api/db`). The ground
truth judging all three arms is the complete read path; each arm never trusts the
screen or its own self-report. Every trial binds a trial-unique memo + idempotency
key. Zero model calls.

**The primary gate is SILENT WRONG-EFFECT - reporting/rendering success while the
ledger disagrees.** A single out-of-band oracle collapses it to a residual on
exactly one class (a collateral write to a surface it does not read); only a
complete read path over every mutable surface reaches 0. `wrong_action` is
reported separately because post-action verification can detect an incorrect
effect after persistence but cannot retroactively prevent it. Over-halt (halting
when the write was actually fine) is the safe availability cost.

Both MockLoan and the clinical MockMed are SYNTHETIC apps built by the same team.
A matching single-surface residual across the two domains is SUGGESTIVE of
generalizability, not proof; the point it earns is narrower and honest: a single
out-of-band record oracle is not sufficient - 0 requires a read path covering
every mutable surface.


Tasks: 12 (divergence classes C1-C8 + clean / idempotent controls).  
Trials per task per arm: 3 (DETERMINISTIC replays; run-to-run variance ~ 0, so these are a coverage matrix over scenarios, not a sampled rate - no confidence interval is implied).  

## Headline - the ladder

| arm | read path | episodes | SWER | wrong action | over-halt | task success | success-effect gap |
|---|---|---|---|---|---|---|---|
| `screen_only` | the rendered banner | 36 | **0.667** (24/36) | 0/36 | 0.0 | 0.167 | 0.667 |
| `effect_verify_single` | single surface (`/api/disbursements`) | 36 | **0.083** (3/36) | 12/36 | 0.167 | 0.25 | 0.083 |
| `effect_verify_full` | complete (`/api/db`) | 36 | **0.0** (0/36) | 15/36 | 0.167 | 0.25 | 0.0 |

- Screen-only SWER = **0.667** (24/36): the injected faults render a clean 'Disbursement authorized' banner while the ledger is wrong (a partial/phantom/duplicate/lost-update/wrong-loan/collateral write).
- Single-surface SWER = **0.083** (3/36): a single out-of-band oracle over the disbursements ledger catches every same-surface fault but is BLIND to the `collateral` write on the fees surface, leaving a residual silent-wrong-effect on exactly that one class. This is the lending analog of the clinical single-surface REST oracle's 9/90 residual (the SAME honest finding, a second domain).
- Complete-read-path SWER = **0.0** (0/36): reading every mutable surface (disbursements + fees) sees the collateral row and drives the *silent* residual to 0. It records 15/36 wrong actions detected after persistence and over-halt = 0.167. Zero SWER therefore means no incorrect effect was silently certified; it does not mean the post-action verifier prevented every incorrect write.

## Per-outcome counts

| arm | false_abort | over_halt | safe_halt | silent_wrong_effect | success | wrong_action |
|---|---|---|---|---|---|---|
| `screen_only` | 3 | 0 | 3 | 24 | 6 | 0 |
| `effect_verify_single` | 0 | 6 | 6 | 3 | 9 | 12 |
| `effect_verify_full` | 0 | 6 | 6 | 0 | 9 | 15 |

## Method / oracle independence

All three arms drive the SAME writes against the SAME fault server; only `reported_success` (and, for the effect arms, which surface the arm's OWN verifier reads) differs. The independent ground-truth oracle handed to `score_episode` is a `RestRecordVerifier` reading the COMPLETE path `/api/db` (both surfaces) pre-action and post-action, and it is a DISTINCT instance from any arm's own verifier - the arm cannot influence the judge. The `collateral` (C8) fault books the correct disbursement AND a spurious fee to the separate fees / general-ledger surface with the same loan and funding memo: the disbursements-only read counts one correct money-movement row (CONFIRMED), while the complete read path counts two for one authorization (at-most-once violated -> REFUTED). That is why the single-surface arm reports a silent success and the complete-read-path arm refuses to certify the already-persisted incorrect effect. The C6 task seeds a same-name decoy loan and funds it; the intended loan stays empty, so a blind (identity-less) write is a silent wrong-effect under `screen_only` and an over-halt (caught, safe) under both effect arms.

## Honest disclosure

- **Both apps are SYNTHETIC.** MockLoan and MockMed are toy apps built by the same team; two synthetic domains agreeing is suggestive of generalizability, not proof.
- **The single-surface oracle leaves a residual on the collateral class**, exactly as the clinical study's single-surface REST oracle does (9/90). The two domains are therefore comparable: neither reaches 0 with a single out-of-band record oracle.
- **0 requires a COMPLETE read path** covering every mutable surface. The complete-read-path arm reaches 0 here only because `/api/db` spans both the disbursements and the fees surfaces; a real deployment must enumerate and read every surface a consequential write can touch. This is post-action detection, not rollback or proof that no incorrect write occurred.
- **The committed JSON is a bounded public aggregate.** It retains overall and category-level EffectBench metrics, but no raw episode rows, payloads, environment fingerprints, or target recipes.
- **No confidence intervals are implied.** These are deterministic replays (variance ~ 0); the table is a coverage matrix over scenarios, not a sampled estimate.

## Reproduce

```
python -m benchmark.lending_fault_model.swer
```


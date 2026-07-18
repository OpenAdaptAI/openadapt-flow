# ❌ openimis-eligibility-check — FAILED

- **Started:** 2026-07-18T17:21:49.038216+00:00
- **Steps:** 5/6 ok
- **Heals:** 0
- **Data egress:** none — fully local replay (zero screenshots left the box)

## Parameters

| Param | Value |
| --- | --- |
| `insurance_no` | 999000002 |

## Identity protection coverage

**3 of 3 click steps identity-armed.** Unarmed clicks proceed with **no identity verification** (see docs/LIMITS.md).

## Effect verification (system of record)

**1 of 6 executed step(s) carried a system-of-record effect contract** — 0 confirmed, 1 halted, 0 approved-unverified. Steps without a contract fall back to screen evidence for their writes (run `openadapt-flow lint` for the bundle's per-consequential-step effect coverage).

## Steps

| # | Step | Intent | Rung | Confidence | Verified | ms | Healed | OK |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `step_000` | click 'Q Insuree enquiry ?' | template | 1.00 | id ✓ | 1710 |  | ✅ |
| 2 | `step_001` | type <insurance_no> | &mdash; | &mdash; | input ✓ | 840 |  | ✅ |
| 3 | `step_002` | press Enter | &mdash; | &mdash; | &mdash; | 1323 |  | ✅ |
| 4 | `step_003` | click at (323, 626) | template | 1.00 | id ✓ | 1427 |  | ✅ |
| 5 | `step_004` | type 'General' | &mdash; | &mdash; | input ✓ | 1533 |  | ✅ |
| 6 | `step_005` | click at (351, 673) | template | 1.00 | id ✓, effect ✗ | 6146 |  | ❌ |

## Screenshots

### `step_005` — click at (351, 673) (failed, final step)

> ❌ **Error:** System-of-record effect verification HALTED step 'step_005' (click at (351, 673)): field_equals refuted against the sql system of record (the screen showed success but the record is wrong or unverifiable) — field 'coverage' is 'Inactive', expected 'Active' (partial save -- the row persisted but this field was dropped or differs) — run aborted

| Before | After |
| --- | --- |
| ![step_005 before](steps/step_005_before.png) | ![step_005 after](steps/step_005_after.png) |

## Rung histogram

| Rung | Count | |
| --- | --- | --- |
| `template` | 2 | ██ |
| `template_global` | 0 |  |
| `ocr` | 0 |  |
| `geometry` | 0 |  |
| `grounder` | 0 |  |

## Totals

| Metric | Value |
| --- | --- |
| Total time | 12979 ms |
| Steps ok | 5/6 |
| Heals | 0 |
| model_calls | 0 |
| est_model_cost_usd | $0.0000 |

# ❌ openemr-add-patient-note — FAILED

- **Started:** 2026-07-08T17:11:54.190831+00:00
- **Steps:** 13/14 ok
- **Heals:** 0

## Parameters

| Param | Value |
| --- | --- |
| `note` | Replay run 5: home BP log reviewed, readings stable. |

## Steps

| # | Step | Intent | Rung | Confidence | ms | Healed | OK |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `step_000` | click 'ername' | template | 1.00 | 363 |  | ✅ |
| 2 | `step_001` | type 'admin' | &mdash; | &mdash; | 647 |  | ✅ |
| 3 | `step_002` | click 'Password' | template | 1.00 | 372 |  | ✅ |
| 4 | `step_003` | type 'pass' | &mdash; | &mdash; | 342 |  | ✅ |
| 5 | `step_004` | click 'Login' | template | 1.00 | 3103 |  | ✅ |
| 6 | `step_005` | click 'Searchbyanydemogre' | template | 1.00 | 5283 |  | ✅ |
| 7 | `step_006` | type 'Phil' | &mdash; | &mdash; | 1244 |  | ✅ |
| 8 | `step_007` | press Enter | &mdash; | &mdash; | 2036 |  | ✅ |
| 9 | `step_008` | click 'ford,Phil' | template | 1.00 | 2201 |  | ✅ |
| 10 | `step_009` | scroll by (0, 400) | &mdash; | &mdash; | 418 |  | ✅ |
| 11 | `step_010` | scroll by (0, 400) | &mdash; | &mdash; | 429 |  | ✅ |
| 12 | `step_011` | scroll by (0, 400) | &mdash; | &mdash; | 448 |  | ✅ |
| 13 | `step_012` | scroll by (0, 400) | &mdash; | &mdash; | 430 |  | ✅ |
| 14 | `step_013` | click at (815, 369) | geometry | 0.90 | 9168 |  | ❌ |

## Screenshots

### `step_013` — click at (815, 369) (failed, final step)

> ❌ **Error:** Postconditions failed for step 'step_013' (click at (815, 369)): expected screen state not reached (semantic drift) — failed: region_stable region=(0, 176, 691, 174); text_present 'PatientMessagesforBelford,Phil' — run aborted

| Before | After |
| --- | --- |
| ![step_013 before](steps/step_013_before.png) | ![step_013 after](steps/step_013_after.png) |

## Rung histogram

| Rung | Count | |
| --- | --- | --- |
| `template` | 5 | █████ |
| `template_global` | 0 |  |
| `ocr` | 0 |  |
| `geometry` | 0 |  |
| `grounder` | 0 |  |

## Totals

| Metric | Value |
| --- | --- |
| Total time | 26484 ms |
| Steps ok | 13/14 |
| Heals | 0 |
| model_calls | 0 |
| est_model_cost_usd | $0.0000 |

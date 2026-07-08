# ✅ openemr-add-patient-note — success

- **Started:** 2026-07-08T17:10:12.425501+00:00
- **Steps:** 18/18 ok
- **Heals:** 1

## Parameters

| Param | Value |
| --- | --- |
| `note` | Replay run 2: patient reports no dizziness since dose change. |

## Steps

| # | Step | Intent | Rung | Confidence | ms | Healed | OK |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `step_000` | click 'ername' | template | 1.00 | 378 |  | ✅ |
| 2 | `step_001` | type 'admin' | &mdash; | &mdash; | 764 |  | ✅ |
| 3 | `step_002` | click 'Password' | template | 1.00 | 353 |  | ✅ |
| 4 | `step_003` | type 'pass' | &mdash; | &mdash; | 329 |  | ✅ |
| 5 | `step_004` | click 'Login' | template | 1.00 | 2183 |  | ✅ |
| 6 | `step_005` | click 'Searchbyanydemogre' | template | 1.00 | 5906 |  | ✅ |
| 7 | `step_006` | type 'Phil' | &mdash; | &mdash; | 1436 |  | ✅ |
| 8 | `step_007` | press Enter | &mdash; | &mdash; | 2281 |  | ✅ |
| 9 | `step_008` | click 'ford,Phil' | template | 1.00 | 2280 |  | ✅ |
| 10 | `step_009` | scroll by (0, 400) | &mdash; | &mdash; | 383 |  | ✅ |
| 11 | `step_010` | scroll by (0, 400) | &mdash; | &mdash; | 411 |  | ✅ |
| 12 | `step_011` | scroll by (0, 400) | &mdash; | &mdash; | 409 |  | ✅ |
| 13 | `step_012` | scroll by (0, 400) | &mdash; | &mdash; | 389 |  | ✅ |
| 14 | `step_013` | click at (815, 369) | geometry | 0.90 | 5833 | 🩹 | ✅ |
| 15 | `step_014` | click '+Add <B' | template | 1.00 | 2593 |  | ✅ |
| 16 | `step_015` | click at (639, 357) | template | 1.00 | 439 |  | ✅ |
| 17 | `step_016` | type <note> | &mdash; | &mdash; | 414 |  | ✅ |
| 18 | `step_017` | click 'Save as new messag' | template | 1.00 | 2191 |  | ✅ |

## Screenshots

### `step_013` — click at (815, 369) (healed)

| Before | After |
| --- | --- |
| ![step_013 before](steps/step_013_before.png) | ![step_013 after](steps/step_013_after.png) |

**Heal** (`anchor_refresh` via `geometry`, applied):

- anchor `templates/step_013.png` → `templates/step_013.png`

| Healed frame |
| --- |
| ![step_013 heal](heals/step_013/screen.png) |

### `step_017` — click 'Save as new messag' (final step)

| Before | After |
| --- | --- |
| ![step_017 before](steps/step_017_before.png) | ![step_017 after](steps/step_017_after.png) |

## Rung histogram

| Rung | Count | |
| --- | --- | --- |
| `template` | 8 | ████████ |
| `template_global` | 0 |  |
| `ocr` | 0 |  |
| `geometry` | 1 | █ |
| `grounder` | 0 |  |

## Totals

| Metric | Value |
| --- | --- |
| Total time | 28972 ms |
| Steps ok | 18/18 |
| Heals | 1 |
| model_calls | 0 |
| est_model_cost_usd | $0.0000 |

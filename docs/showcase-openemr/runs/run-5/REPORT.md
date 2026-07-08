# ✅ openemr-add-patient-note — success

- **Started:** 2026-07-08T17:37:25.736219+00:00
- **Steps:** 18/18 ok
- **Heals:** 1

## Parameters

| Param | Value |
| --- | --- |
| `note` | Replay run 5: home BP log reviewed, readings stable. |

## Steps

| # | Step | Intent | Rung | Confidence | ms | Healed | OK |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `step_000` | click 'ername' | template | 1.00 | 450 |  | ✅ |
| 2 | `step_001` | type 'admin' | &mdash; | &mdash; | 627 |  | ✅ |
| 3 | `step_002` | click 'Password' | template | 1.00 | 376 |  | ✅ |
| 4 | `step_003` | type 'pass' | &mdash; | &mdash; | 349 |  | ✅ |
| 5 | `step_004` | click 'Login' | template | 1.00 | 3324 |  | ✅ |
| 6 | `step_005` | click 'Searchbyanydemogre' | template | 1.00 | 3460 |  | ✅ |
| 7 | `step_006` | type 'Phil' | &mdash; | &mdash; | 1331 |  | ✅ |
| 8 | `step_007` | press Enter | &mdash; | &mdash; | 2114 |  | ✅ |
| 9 | `step_008` | click 'ford,Phil' | template | 1.00 | 2371 |  | ✅ |
| 10 | `step_009` | scroll by (0, 400) | &mdash; | &mdash; | 6009 |  | ✅ |
| 11 | `step_010` | scroll by (0, 400) | &mdash; | &mdash; | 4158 |  | ✅ |
| 12 | `step_011` | scroll by (0, 400) | &mdash; | &mdash; | 2150 |  | ✅ |
| 13 | `step_012` | scroll by (0, 400) | &mdash; | &mdash; | 2168 |  | ✅ |
| 14 | `step_013` | click at (815, 369) | geometry | 0.90 | 3500 | 🩹 | ✅ |
| 15 | `step_014` | click '+Add <B' | template | 1.00 | 2055 |  | ✅ |
| 16 | `step_015` | click at (639, 357) | template | 1.00 | 464 |  | ✅ |
| 17 | `step_016` | type <note> | &mdash; | &mdash; | 409 |  | ✅ |
| 18 | `step_017` | click 'Save as new messag' | template | 1.00 | 1798 |  | ✅ |

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
| Total time | 37111 ms |
| Steps ok | 18/18 |
| Heals | 1 |
| model_calls | 0 |
| est_model_cost_usd | $0.0000 |

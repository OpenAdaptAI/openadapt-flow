# ✅ mockmed-triage — success

- **Started:** 2026-07-06T18:29:56.944727+00:00
- **Steps:** 11/11 ok
- **Heals:** 8

## Parameters

| Param | Value |
| --- | --- |
| `note` | Showcase triage booking three months |

## Steps

| # | Step | Intent | Rung | Confidence | ms | Healed | OK |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `step_000` | click at (214, 195) | geometry | 0.90 | 1037 | 🩹 | ✅ |
| 2 | `step_001` | type 'nurse.demo' | &mdash; | &mdash; | 582 |  | ✅ |
| 3 | `step_002` | click at (214, 264) | geometry | 0.90 | 943 | 🩹 | ✅ |
| 4 | `step_003` | type 'mockmed-demo-pass' | &mdash; | &mdash; | 339 |  | ✅ |
| 5 | `step_004` | click 'Sign In' | ocr | 1.00 | 1123 | 🩹 | ✅ |
| 6 | `step_005` | click 'Open' | ocr | 1.00 | 1192 | 🩹 | ✅ |
| 7 | `step_006` | click 'New Encounter' | ocr | 0.96 | 1057 | 🩹 | ✅ |
| 8 | `step_007` | click 'Triage' | ocr | 1.00 | 859 | 🩹 | ✅ |
| 9 | `step_008` | click at (344, 290) | geometry | 0.90 | 972 | 🩹 | ✅ |
| 10 | `step_009` | type <note> | &mdash; | &mdash; | 335 |  | ✅ |
| 11 | `step_010` | click 'Save Encounter' | ocr | 1.00 | 1255 | 🩹 | ✅ |

## Screenshots

### `step_000` — click at (214, 195) (healed)

| Before | After |
| --- | --- |
| ![step_000 before](steps/step_000_before.png) | ![step_000 after](steps/step_000_after.png) |

**Heal** (`anchor_refresh` via `geometry`, applied):

- anchor `templates/step_000.png` → `templates/step_000.png`

| Healed frame |
| --- |
| ![step_000 heal](heals/step_000/screen.png) |

### `step_002` — click at (214, 264) (healed)

| Before | After |
| --- | --- |
| ![step_002 before](steps/step_002_before.png) | ![step_002 after](steps/step_002_after.png) |

**Heal** (`anchor_refresh` via `geometry`, applied):

- anchor `templates/step_002.png` → `templates/step_002.png`

| Healed frame |
| --- |
| ![step_002 heal](heals/step_002/screen.png) |

### `step_004` — click 'Sign In' (healed)

| Before | After |
| --- | --- |
| ![step_004 before](steps/step_004_before.png) | ![step_004 after](steps/step_004_after.png) |

**Heal** (`anchor_refresh` via `ocr`, applied):

- anchor `templates/step_004.png` → `templates/step_004.png`

| Healed frame |
| --- |
| ![step_004 heal](heals/step_004/screen.png) |

### `step_005` — click 'Open' (healed)

| Before | After |
| --- | --- |
| ![step_005 before](steps/step_005_before.png) | ![step_005 after](steps/step_005_after.png) |

**Heal** (`anchor_refresh` via `ocr`, applied):

- anchor `templates/step_005.png` → `templates/step_005.png`

| Healed frame |
| --- |
| ![step_005 heal](heals/step_005/screen.png) |

### `step_006` — click 'New Encounter' (healed)

| Before | After |
| --- | --- |
| ![step_006 before](steps/step_006_before.png) | ![step_006 after](steps/step_006_after.png) |

**Heal** (`anchor_refresh` via `ocr`, applied):

- anchor `templates/step_006.png` → `templates/step_006.png`

| Healed frame |
| --- |
| ![step_006 heal](heals/step_006/screen.png) |

### `step_007` — click 'Triage' (healed)

| Before | After |
| --- | --- |
| ![step_007 before](steps/step_007_before.png) | ![step_007 after](steps/step_007_after.png) |

**Heal** (`anchor_refresh` via `ocr`, applied):

- anchor `templates/step_007.png` → `templates/step_007.png`

| Healed frame |
| --- |
| ![step_007 heal](heals/step_007/screen.png) |

### `step_008` — click at (344, 290) (healed)

| Before | After |
| --- | --- |
| ![step_008 before](steps/step_008_before.png) | ![step_008 after](steps/step_008_after.png) |

**Heal** (`anchor_refresh` via `geometry`, applied):

- anchor `templates/step_008.png` → `templates/step_008.png`

| Healed frame |
| --- |
| ![step_008 heal](heals/step_008/screen.png) |

### `step_010` — click 'Save Encounter' (healed, final step)

| Before | After |
| --- | --- |
| ![step_010 before](steps/step_010_before.png) | ![step_010 after](steps/step_010_after.png) |

**Heal** (`anchor_refresh` via `ocr`, applied):

- anchor `templates/step_010.png` → `templates/step_010.png`

| Healed frame |
| --- |
| ![step_010 heal](heals/step_010/screen.png) |

## Rung histogram

| Rung | Count | |
| --- | --- | --- |
| `template` | 0 |  |
| `template_global` | 0 |  |
| `ocr` | 5 | █████ |
| `geometry` | 3 | ███ |
| `grounder` | 0 |  |

## Totals

| Metric | Value |
| --- | --- |
| Total time | 9694 ms |
| Steps ok | 11/11 |
| Heals | 8 |
| model_calls | 0 |
| est_model_cost_usd | $0.0000 |

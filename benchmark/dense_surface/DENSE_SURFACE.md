# Dense sibling-surface false-abort / false-accept study

The identity band matcher's headline numbers (`docs/validation/IDENTITY_ROC.md`: **false accept 0.000%, false abort 26.17%**) are measured on SYNTHETIC corpora (string pairs with hand-injected OCR noise) and, at the product level, on CLEAN OpenEMR identity banners. This study measures the SAME matcher on the surface where a wrong-patient write actually does damage: a dense, sibling-heavy record LIST, rendered as HTML, screenshotted, and read by the repo's own OCR (RapidOCR). Every band string below came out of OCR reading a rendered PNG — nothing is a fabricated string.

## Method (faithful to record + replay)

- **Fixture**: a dense clinical record list (40 rows: MRN / Patient Name / DOB / Sex / Status / Last Seen / Open) with seeded collision siblings placed one row from their target. Rendered over 5 seeds (1, 2, 3, 4, 5).
- **Record** (crisp, `device_scale_factor=2`, Arial 15px): OCR the full frame and store the identity band exactly as `compiler.compile` does — `context_from_lines(...)` with the clicked cell's template crop EXCLUDED and volatile lines dropped.
- **Replay**: at the resolved click point, extract the band exactly as `Replayer._verify_identity` does — `band_region`, translate the exclude crop to the resolved point, drop volatile lines, `lines_near_point` row refinement, then `verify_target_identity` with the same 2x-upscale retry. No Anthropic calls; identity + OCR only.
- **Two click configs** per target: `click_name` (open the chart by clicking the name cell — the NAME is then excluded from the band, so DOB/MRN/Sex/Status carry identity) and `click_action` (click the row's Open button — the NAME stays in the band).
- **Replay conditions** (the record frame is always crisp; the RISK variable is the replay surface): `hi_res_arial`, `native_arial`, `small_dense`, `serif_drift` (`hi_res_arial` = same crisp dsf2 control; `native_arial` = dsf1; `small_dense` = dsf1, 12px, tighter rows; `serif_drift` = dsf1, Georgia — an app font change between record and replay).
- **False abort** = resolver on the CORRECT row, identity fails to verify. **False accept** = resolver on the adjacent SIBLING row, identity verifies it as the target (catastrophic; must stay 0). Siblings are realistic different patients (distinct MRN); the confusable/transposed classes put the sole difference in the MRN.

Operating point (pinned, from the ROC): {'coverage_threshold': 0.8, 'uncovered_run_cap': 4, 'contradicted_chars_cap': 0, 'suspect_chars_cap': 0, 'unexplained_name_tokens_cap': 0, 'absent_name_token_cap': 3}.

## Headline (dense surface, armed clicks)

- **per-click false abort: 6.11%** (22/360) — of which 22 readable-but-mismatch and 0 unreadable.
- **per-click false accept: 7.22%** (26/360).
- unarmed clicks (no band recorded, identity gate never runs): 0.

**Versus the synthetic baseline** (false abort 26.17%, false accept 0.00%): the real dense-surface false abort is **6.11%**, i.e. **LOWER** than the synthetic 26.17% by 20.06%. FALSE ACCEPT DID NOT STAY 0 — 26 sibling rows verified as their target (details below).

### By replay condition (OCR resolution)

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `hi_res_arial` | 90 | 6.67% (6) | 6 / 0 | 6.67% (6) |
| `native_arial` | 90 | 6.67% (6) | 6 / 0 | 6.67% (6) |
| `serif_drift` | 90 | 4.44% (4) | 4 / 0 | 8.89% (8) |
| `small_dense` | 90 | 6.67% (6) | 6 / 0 | 6.67% (6) |

### By click config

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `click_action` | 180 | 6.11% (11) | 11 / 0 | 7.22% (13) |
| `click_name` | 180 | 6.11% (11) | 11 / 0 | 7.22% (13) |

### By collision class

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `generational_suffix` | 40 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `id_confusion_O0` | 40 | 55.00% (22) | 22 / 0 | 60.00% (24) |
| `id_confusion_l1` | 40 | 0.00% (0) | 0 / 0 | 5.00% (2) |
| `letterletter_name` | 40 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `mrn_transposition` | 40 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `near_surname` | 40 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `nguyen_variant` | 40 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `same_name_diff_dob` | 40 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `same_surname_diff_first` | 40 | 0.00% (0) | 0 / 0 | 0.00% (0) |

### By collision class x click config

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `generational_suffix::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `generational_suffix::click_name` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `id_confusion_O0::click_action` | 20 | 55.00% (11) | 11 / 0 | 60.00% (12) |
| `id_confusion_O0::click_name` | 20 | 55.00% (11) | 11 / 0 | 60.00% (12) |
| `id_confusion_l1::click_action` | 20 | 0.00% (0) | 0 / 0 | 5.00% (1) |
| `id_confusion_l1::click_name` | 20 | 0.00% (0) | 0 / 0 | 5.00% (1) |
| `letterletter_name::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `letterletter_name::click_name` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `mrn_transposition::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `mrn_transposition::click_name` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `near_surname::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `near_surname::click_name` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `nguyen_variant::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `nguyen_variant::click_name` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `same_name_diff_dob::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `same_name_diff_dob::click_name` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `same_surname_diff_first::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `same_surname_diff_first::click_name` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |

## Worst collision class

Highest false-abort collision class: **`id_confusion_O0`** at 55.00% (22/40).

## Adjacent-row bleed

- Bands whose raw OCR lines included a token from a NEIGHBOUR row (above/below the resolved row): 147/360 (40.83%).
- Neighbour tokens that SURVIVED the `lines_near_point` row filter into the identity band: 0.
- Trials where the row filter CHANGED the false-abort verdict (i.e. bleed would have changed the decision without it): 77.

**Finding:** the `lines_near_point` row refinement absorbs adjacent-row bleed — neighbour tokens are picked up in the coarse 64px band but filtered out before the verdict, and removing the filter would change decisions in the count above.

## False accepts (headline safety finding)

**26 FALSE ACCEPTS.** Each is a wrong-patient verify on a real rendered, OCR'd sibling row. Exact rows and band strings:

| class | config | condition | target -> sibling | recorded band (expected) | observed band at sibling | cov |
| --- | --- | --- | --- | --- | --- | --- |
| `id_confusion_O0` | click_name | hi_res_arial | Petrov, Robert (C0X3834) -> Petrov, Robert (COX3834) | `COX3834 1944-08-08 F Pending Open` | `COX3834 1944-08-08 F Pending Open` | 1.0 |
| `id_confusion_O0` | click_name | native_arial | Petrov, Robert (C0X3834) -> Petrov, Robert (COX3834) | `COX3834 1944-08-08 F Pending Open` | `COX3834 1944-08-08 F Pending Open` | 1.0 |
| `id_confusion_O0` | click_name | small_dense | Petrov, Robert (C0X3834) -> Petrov, Robert (COX3834) | `COX3834 1944-08-08 F Pending Open` | `COX3834 1944-08-08 F Pending Open` | 1.0 |
| `id_confusion_O0` | click_name | serif_drift | Petrov, Robert (C0X3834) -> Petrov, Robert (COX3834) | `COX3834 1944-08-08 F Pending Open` | `COX3834 1944-08-08 F Pending Open` | 1.0 |
| `id_confusion_O0` | click_action | hi_res_arial | Petrov, Robert (C0X3834) -> Petrov, Robert (COX3834) | `COX3834 Petrov, Robert 1944-08-08 F Pending` | `COX3834 Petrov, Robert 1944-08-08 F Pending` | 1.0 |
| `id_confusion_O0` | click_action | native_arial | Petrov, Robert (C0X3834) -> Petrov, Robert (COX3834) | `COX3834 Petrov, Robert 1944-08-08 F Pending` | `COX3834 Petrov,Robert 1944-08-08 F Pending` | 1.0 |
| `id_confusion_O0` | click_action | small_dense | Petrov, Robert (C0X3834) -> Petrov, Robert (COX3834) | `COX3834 Petrov, Robert 1944-08-08 F Pending` | `COX3834 Petrov,Robert 1944-08-08 F Pending` | 1.0 |
| `id_confusion_O0` | click_action | serif_drift | Petrov, Robert (C0X3834) -> Petrov, Robert (COX3834) | `COX3834 Petrov, Robert 1944-08-08 F Pending` | `COX3834 Petrov,Robert 1944-08-08 F Pending` | 1.0 |
| `id_confusion_O0` | click_name | hi_res_arial | Kowalski, Angela (C0X4634) -> Kowalski, Angela (COX4634) | `COX4634 1999-06-19 F Active Open` | `COX4634 1999-06-19 F Active Open` | 1.0 |
| `id_confusion_O0` | click_name | native_arial | Kowalski, Angela (C0X4634) -> Kowalski, Angela (COX4634) | `COX4634 1999-06-19 F Active Open` | `COX4634 1999-06-19 F Active Open` | 1.0 |
| `id_confusion_O0` | click_name | small_dense | Kowalski, Angela (C0X4634) -> Kowalski, Angela (COX4634) | `COX4634 1999-06-19 F Active Open` | `COX4634 1999-06-19 F Active Open` | 1.0 |
| `id_confusion_O0` | click_name | serif_drift | Kowalski, Angela (C0X4634) -> Kowalski, Angela (COX4634) | `COX4634 1999-06-19 F Active Open` | `COX4634 1999-06-19 F Active Open` | 1.0 |
| `id_confusion_O0` | click_action | hi_res_arial | Kowalski, Angela (C0X4634) -> Kowalski, Angela (COX4634) | `COX4634 Kowalski, Angela 1999-06-19 F Active` | `COX4634 Kowalski, Angela 1999-06-19 F Active` | 1.0 |
| `id_confusion_O0` | click_action | native_arial | Kowalski, Angela (C0X4634) -> Kowalski, Angela (COX4634) | `COX4634 Kowalski, Angela 1999-06-19 F Active` | `COX4634 Kowalski,Angela 1999-06-19 F Active` | 1.0 |
| `id_confusion_O0` | click_action | small_dense | Kowalski, Angela (C0X4634) -> Kowalski, Angela (COX4634) | `COX4634 Kowalski, Angela 1999-06-19 F Active` | `COX4634 Kowalski, Angela 1999-06-19 F Active` | 1.0 |
| `id_confusion_O0` | click_action | serif_drift | Kowalski, Angela (C0X4634) -> Kowalski, Angela (COX4634) | `COX4634 Kowalski, Angela 1999-06-19 F Active` | `COX4634 Kowalski, Angela 1999-06-19 F Active` | 1.0 |
| `id_confusion_O0` | click_name | hi_res_arial | Delgado, Robert (C0X4320) -> Delgado, Robert (COX4320) | `COX4320 1958-11-07 F Active Open` | `COX4320 1958-11-07 F Active Open` | 1.0 |
| `id_confusion_O0` | click_name | native_arial | Delgado, Robert (C0X4320) -> Delgado, Robert (COX4320) | `COX4320 1958-11-07 F Active Open` | `COX4320 1958-11-07 F Active Open` | 1.0 |
| `id_confusion_O0` | click_name | small_dense | Delgado, Robert (C0X4320) -> Delgado, Robert (COX4320) | `COX4320 1958-11-07 F Active Open` | `COX4320 1958-11-07 F Active Open` | 1.0 |
| `id_confusion_O0` | click_name | serif_drift | Delgado, Robert (C0X4320) -> Delgado, Robert (COX4320) | `COX4320 1958-11-07 F Active Open` | `COX4320 1958-11-07 F Active Open` | 1.0 |
| `id_confusion_O0` | click_action | hi_res_arial | Delgado, Robert (C0X4320) -> Delgado, Robert (COX4320) | `COX4320 Delgado, Robert 1958-11-07 F Active` | `COX4320 Delgado, Robert 1958-11-07 F Active` | 1.0 |
| `id_confusion_O0` | click_action | native_arial | Delgado, Robert (C0X4320) -> Delgado, Robert (COX4320) | `COX4320 Delgado, Robert 1958-11-07 F Active` | `COX4320 Delgado,Robert 1958-11-07 F Active` | 1.0 |
| `id_confusion_O0` | click_action | small_dense | Delgado, Robert (C0X4320) -> Delgado, Robert (COX4320) | `COX4320 Delgado, Robert 1958-11-07 F Active` | `COX4320 Delgado,Robert 1958-11-07 F Active` | 1.0 |
| `id_confusion_O0` | click_action | serif_drift | Delgado, Robert (C0X4320) -> Delgado, Robert (COX4320) | `COX4320 Delgado, Robert 1958-11-07 F Active` | `COX4320 Delgado, Robert 1958-11-07 F Active` | 1.0 |
| `id_confusion_l1` | click_name | serif_drift | Lindqvist, Maria (PL16078) -> Lindqvist, Maria (PLl6078) | `PL16078 1940-10-22 F Active Open` | `PL16078 1940-10-22 F Active Open` | 1.0 |
| `id_confusion_l1` | click_action | serif_drift | Lindqvist, Maria (PL16078) -> Lindqvist, Maria (PLl6078) | `PL16078 Lindqvist, Maria 1940-10-22 F Active` | `PL16078 Lindqvist, Maria 1940-10-22 F Active` | 1.0 |

### Mechanism: OCR glyph-collapse defeats the string-level identifier-suspect rule

26 of the 26 false accepts are **raw-identical bands**: OCR read the target's identifier and the sibling's one-glyph-apart identifier as the SAME string (e.g. target MRN `C0X3834` with a digit ZERO and sibling `COX3834` with a letter O both read as `COX3834`). This is the exact class the ROC and `docs/LIMITS.md` claim the identifier-**suspect** rule closes at 0.000% false accept (v3 `id_letter_digit_collision`, 'A01234' vs 'AO1234'). That defense assumes the two identifiers reach the matcher as DIFFERENT strings, so the match is confusion-only and `_suspicious_pair` fires. On the real rendered surface the confusion happens INSIDE OCR — both glyphs collapse to one string BEFORE the matcher sees them — so the bands are raw-equal, the match is a raw match, the suspect rule never triggers, and the sibling verifies. The synthetic v3 corpus cannot surface this because it injects the confusion as a text edit that keeps the two variants textually distinct, which is precisely the condition the suspect rule was built for. The glyph-collapse false accept is a property of the OCR layer, not the matcher's confusion table, and no string-level rule downstream of OCR can recover the destroyed distinction.

The same instability produces the flip-side availability cost: when OCR reads the confusable glyph INCONSISTENTLY between record and replay (recorded `COX`, replayed `C0X`), the identifier now looks confusion-DIFFERENT, the suspect rule fires on the TRUE row, and the correct target safe-halts (a false abort). One unstable glyph thus drives both error directions on this class.

## Honest verdict (does the product clear the flagship bar?)

On the dense sibling surface the TRUE per-click false abort is **6.11%**, below the synthetic 26.17%. False accept did NOT stay 0 — the product does NOT clear the safety bar on this surface until the classes below are closed.

The availability cost (false abort) is a per-click hybrid-fallback escalation (~$0.10) or a human retry; it is the cheap direction and is the price paid for the zero-false-accept posture. Selection-bias disclosure: this is measured on THIS rendered fixture + RapidOCR, not 'in the world' — a different renderer, font stack, or OCR engine would shift the false-abort rate (and could, in principle, surface a false accept the frozen confusion table does not model).

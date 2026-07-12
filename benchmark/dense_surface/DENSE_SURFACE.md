# Dense sibling-surface false-abort / false-accept study

The identity band matcher's headline numbers (`docs/validation/IDENTITY_ROC.md`: **false accept 0.000%, false abort 26.17%**) are measured on SYNTHETIC corpora (string pairs with hand-injected OCR noise) and, at the product level, on CLEAN OpenEMR identity banners. This study measures the SAME matcher on the surface where a wrong-patient write actually does damage: a dense, sibling-heavy record LIST, rendered as HTML, screenshotted, and read by the repo's own OCR (RapidOCR). Every band string below came out of OCR reading a rendered PNG — nothing is a fabricated string.

## THE REAL FIX — structured-text (DOM / a11y) identity (feat/dom-identity)

Everything below the next heading is the OCR band matcher, measured on this
dense surface. An adversarial review proved that path has an **irreducible**
hole: two DIFFERENT same-name/same-DOB patients whose MRN differs by one O/0 or
l/1 glyph (`MG4408` vs `MG44O8`, `AC50061` vs `AC5OO61`) render to a
BYTE-IDENTICAL OCR band — literally the same string a legit re-read of the true
row produces — so no string rule downstream of OCR can separate them. On this
surface that is **43.8% false accept** on the digit-flanked attack
(`click_action`), and the name-excluded config (`click_name`) pays it back as
over-halt.

The real fix is to stop trusting OCR for identity where a higher-fidelity
signal exists. Identity is now an ordered LADDER
(`openadapt_flow.runtime.identity.run_identity_ladder`): **tier 1 = STRUCTURED
text** — the DOM row text under the click point on a browser backend
(`backend.structured_text_at` → `elementFromPoint` → row `textContent`), or the
UI-Automation / AX text on a native desktop backend — compared to the recorded
target's structured text by exact/normalized match, in which `0` and `O`, `1`
and `l` are DISTINCT characters; **tier 2 = a pixel-compare of the identifier
crop** (`verify_pixel_identity`) for pure-pixel Citrix/RDP/VDI substrates with
no a11y text — OCR collapses O/0, the pixels do not, so a localized crop
compare separates the collapse pairs at AUC 1.0 on a stable render and abstains
under drift (validated in `benchmark/pixel_identity`); **tier 3 = an OPTIONAL
local-VLM veto** (`verify_vlm_identity`) for drifted pixel substrates the pixel
tier can't judge — a local open model, veto-only, 0% false-accept + 100%
detection on the collapse surface, **off by default so the default install
needs no model** (validated in `benchmark/vlm_identity`); **tier N = the OCR
name+DOB-primary band** below, the pixel-substrate fallback with the disclosed
same-name/DOB residual. A higher tier's verdict is final — no lower tier
overrides it — and every tier is fail-safe (unsure → abstain to the next; if
nothing verifies → HALT). The integrated ladder is SUBSTRATE-COMPLETE
(**structured text → pixel-compare → optional VLM veto → OCR name+DOB → halt**)
and measures **0 false-accept across every substrate config**
(`openadapt_flow.validation.identity_ladder`, artifacts in
`benchmark/identity_ladder`). The one irreducible floor — a font rendering
`O`/`0` or `l`/`1` pixel-identical — was not found among 14 common UI fonts
(`benchmark/pixel_identity`); on such a font the sub-OCR tiers abstain and the
run HALTS, never wrong-writes.

**Measured on this exact render→pipeline surface**
(`benchmark/dense_surface/dom_identity_probe.py`, seeds 1–2; raw output in
`dom_identity_probe_seed12.txt`), STRUCTURED (DOM) path vs the OCR fallback:

| corpus | STRUCTURED false accept | STRUCTURED false abort | OCR-fallback false accept | OCR-fallback false abort |
| --- | --- | --- | --- | --- |
| **adversarial** (same name+DOB, digit-flanked MRN) | **0/96 (0.0%)** | **0/96 (0.0%)** | 42/96 (43.8%) | 48/96 (50.0%) |
| **realistic** (diff name/DOB, confusable MRN) | **0/96 (0.0%)** | **0/96 (0.0%)** | 0/96 (0.0%) | 48/96 (50.0%) |
| **original** dense corpus | **0/144 (0.0%)** | **0/144 (0.0%)** | 0/144 (0.0%) | 64/144 (44.4%) |

The structured path **closes the glyph-collapse class at 0 false accept AND 0
added over-halt** — including the digit-flanked attack that gives the OCR path
43.8% false accept — and it does so on BOTH click configs (`click_name` and
`click_action`, 48/48 armed each). Because the DOM text is invariant across
replay font/resolution, it carries **no OCR-availability cost**: where the OCR
fallback pays 44–50% false abort to hold 0 false accept, the DOM path pays
0%. The mechanism is visible in the witness rows the probe prints: the OCR
bands are RAW-IDENTICAL (`MG4408 … == MG4408 …`) while the DOM strings are
DISTINCT (`MG4408 …` vs `MG44O8 …`).

**OCR fallback unchanged (no regression).** The OCR-fallback columns above
reproduce #27's shipped numbers exactly (adversarial 42 FA / 48 abort;
realistic 0 FA / 48 abort in `click_name`, 0/0 in `click_action`; original 0
FA), confirming the fallback tier is untouched and still available on
pure-pixel substrates.

**Impossibility result, stated plainly:** same-NAME + same-DOB + a
collapsible-ID (one O/0 or l/1 glyph apart) is UNVERIFIABLE by OCR alone — the
two rows reach the matcher as identical bytes. It is verifiable against
structured text (DOM / UIA / AX), which is why identity now prefers that
signal; on pure-pixel substrates the pixel/perceptual tier (next) and
glyph-disambiguating identifier OCR (roadmapped) are the mitigations, and the
OCR fallback's residual (below) stands until they land.

To regenerate this comparison: `python benchmark/dense_surface/dom_identity_probe.py 1 2`.
The full 5-seed `python -m openadapt_flow.validation.dense_surface` run also
emits a "Structured-text (DOM) identity path" section in this file.

## FIX — name+DOB-primary identity (7th reopening)

The body below is the CURRENT-code re-measurement (0/360 false accept, 45.00% false abort). The history:

**6th reopening (`feat/ocr-glyph-fix`, PR #26).** The matcher halted on a raw match to an identifier carrying a homoglyph **LETTER** (O/l/I). That drove the O/0 and l/1/I same-name/DOB collapse false accepts to 0/360.

**7th reopening — the DIGIT-FLANKED break (this branch).** A real MRN is `<alpha prefix><numeric body>` (e.g. `MG480312`, `AC50061`). When the confusable glyph is DIGIT-FLANKED, RapidOCR reads the **DIGIT** form on BOTH a patient (`AC50061`) and a DIFFERENT same-name/DOB patient (`AC5OO61`, letter O) — both collapse to `AC50061`, **NO homoglyph letter survives**, #26's letter-only flag misses it, and the sibling verifies. Measured through this exact render→OCR→match pipeline (`benchmark/dense_surface/digit_flanked_probe.py`, same-name/DOB digit-flanked pairs, seeds 1–3): **~87% false accept**. An adversarial review proved this is not fixable by another string-layer glyph rule — OCR destroys the O/0 and l/1 distinction at the pixel level before the matcher runs, and flagging the digit side (any 0/1 in an MRN) would halt ~3 of every 4 real MRNs.

**The fix changes WHAT identity trusts.** Identity is verified on the OCR-RELIABLE, linguistically-redundant signal — the patient **NAME + DOB** — and a confusable-glyph identifier is CORROBORATION only:

- When a **discriminative NAME carries** identity (present and matched — the `click_action` config, name in band), a confusable-DIGIT MRN does **NOT** block verification. Most real patients differ by name/DOB, so a wrong (sibling) row differs there and is caught by coverage/contradiction; the MRN never has to be trusted. On a realistic different-name/DOB corpus with confusable-digit MRNs the true row verifies at `click_action` false-abort **0/48**.
- When identity would rest **SOLELY** on a glyph-vulnerable identifier — the clicked NAME cell is excluded (`click_name`), leaving only DOB + MRN + generic columns and no name to carry — a DIGIT-body glyph-vulnerable MRN **HALTS** (unverifiable; a safe false-abort). This is what closes the digit-flanked collapse in that config.
- A homoglyph **LETTER** stays a HARD halt (affirmative OCR ambiguity — it fires even when a name carries), so the 6th-reopening closure is preserved **with no regression**: this study still measures **0/360 false accept**.

**Post-fix numbers, stated honestly.** Digit-flanked attack (`benchmark/dense_surface/digit_flanked_probe.py`) false accept **~87% → 43.8%**: the `click_name` half — where identity rests solely on the identifier — closes to **0**; the `click_action` half (name displayed and matching) is the disclosed residual below and is NOT closed. This study (original collision corpus): **0/360 false accept** (no regression), per-click false abort **18.89% → 45.00%**. The added false abort is **not a reduction** — it is the price of closing the `click_name` digit-flanked hole: the digit-side sole-discriminator halt fires on every name-excluded band whose MRN carries a 0/1 (~3 of 4 patients), so `click_name` false abort rises to 71.11% while `click_action` (name carries) stays at the #26 level (18.89%). This trades availability for safety in the name-excluded config — the cheap direction (a hybrid-fallback escalation or a human retry). The `click_action` over-halt is NOT reduced vs #26: the incidental letter-homoglyph halt is kept HARD because softening it (verifying a letter-collapse when a name carries) would re-verify the same-name/DOB letter siblings the 6th reopening closed — a direct FA-vs-availability tension this surface cannot escape at the string layer.

**Disclosed residual (fundamental).** A same-name/DOB DIFFERENT patient whose digit-body MRN OCR-collapses to the target's, **WITH the name displayed and matching** (`click_action`), is band-identical to a legitimate same-patient re-read — name+DOB carry, so it VERIFIES. The two rows reach the matcher as the same bytes; no band-level rule can separate them. Closing it requires flagging every digit MRN (catastrophic over-halt) or the complete upstream fix — glyph-disambiguating / high-resolution OCR on identifier regions (roadmapped). Pinned in `tests/test_identity_out_of_corpus.py::TestBlocker7NameDobPrimary`.

## Method (faithful to record + replay)

- **Fixture**: a dense clinical record list (40 rows: MRN / Patient Name / DOB / Sex / Status / Last Seen / Open) with seeded collision siblings placed one row from their target. Rendered over 5 seeds (1, 2, 3, 4, 5).
- **Record** (crisp, `device_scale_factor=2`, Arial 15px): OCR the full frame and store the identity band exactly as `compiler.compile` does — `context_from_lines(...)` with the clicked cell's template crop EXCLUDED and volatile lines dropped.
- **Replay**: at the resolved click point, extract the band exactly as `Replayer._verify_identity` does — `band_region`, translate the exclude crop to the resolved point, drop volatile lines, `lines_near_point` row refinement, then `verify_target_identity` with the same 2x-upscale retry. No Anthropic calls; identity + OCR only.
- **Two click configs** per target: `click_name` (open the chart by clicking the name cell — the NAME is then excluded from the band, so DOB/MRN/Sex/Status carry identity) and `click_action` (click the row's Open button — the NAME stays in the band).
- **Replay conditions** (the record frame is always crisp; the RISK variable is the replay surface): `hi_res_arial`, `native_arial`, `small_dense`, `serif_drift` (`hi_res_arial` = same crisp dsf2 control; `native_arial` = dsf1; `small_dense` = dsf1, 12px, tighter rows; `serif_drift` = dsf1, Georgia — an app font change between record and replay).
- **False abort** = resolver on the CORRECT row, identity fails to verify. **False accept** = resolver on the adjacent SIBLING row, identity verifies it as the target (catastrophic; must stay 0). Siblings are realistic different patients (distinct MRN); the confusable/transposed classes put the sole difference in the MRN.

Operating point (pinned, from the ROC): {'coverage_threshold': 0.8, 'uncovered_run_cap': 4, 'contradicted_chars_cap': 0, 'suspect_chars_cap': 0, 'unexplained_name_tokens_cap': 0, 'absent_name_token_cap': 3}.

## Headline (dense surface, armed clicks)

- **per-click false abort: 45.00%** (162/360) — of which 162 readable-but-mismatch and 0 unreadable.
- **per-click false accept: 0.00%** (0/360).
- unarmed clicks (no band recorded, identity gate never runs): 0.

**Versus the synthetic baseline** (false abort 26.17%, false accept 0.00%): the real dense-surface false abort is **45.00%**, i.e. **HIGHER** than the synthetic 26.17% by 18.83%. False accept STAYED 0 on real dense OCR.

### By replay condition (OCR resolution)

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `hi_res_arial` | 90 | 44.44% (40) | 40 / 0 | 0.00% (0) |
| `native_arial` | 90 | 44.44% (40) | 40 / 0 | 0.00% (0) |
| `serif_drift` | 90 | 46.67% (42) | 42 / 0 | 0.00% (0) |
| `small_dense` | 90 | 44.44% (40) | 40 / 0 | 0.00% (0) |

### By click config

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `click_action` | 180 | 18.89% (34) | 34 / 0 | 0.00% (0) |
| `click_name` | 180 | 71.11% (128) | 128 / 0 | 0.00% (0) |

### By collision class

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `generational_suffix` | 40 | 10.00% (4) | 4 / 0 | 0.00% (0) |
| `id_confusion_O0` | 40 | 85.00% (34) | 34 / 0 | 0.00% (0) |
| `id_confusion_l1` | 40 | 100.00% (40) | 40 / 0 | 0.00% (0) |
| `letterletter_name` | 40 | 40.00% (16) | 16 / 0 | 0.00% (0) |
| `mrn_transposition` | 40 | 20.00% (8) | 8 / 0 | 0.00% (0) |
| `near_surname` | 40 | 30.00% (12) | 12 / 0 | 0.00% (0) |
| `nguyen_variant` | 40 | 40.00% (16) | 16 / 0 | 0.00% (0) |
| `same_name_diff_dob` | 40 | 40.00% (16) | 16 / 0 | 0.00% (0) |
| `same_surname_diff_first` | 40 | 40.00% (16) | 16 / 0 | 0.00% (0) |

### By collision class x click config

| group | n | false-abort | (mismatch / unreadable) | false-accept |
| --- | --- | --- | --- | --- |
| `generational_suffix::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `generational_suffix::click_name` | 20 | 20.00% (4) | 4 / 0 | 0.00% (0) |
| `id_confusion_O0::click_action` | 20 | 70.00% (14) | 14 / 0 | 0.00% (0) |
| `id_confusion_O0::click_name` | 20 | 100.00% (20) | 20 / 0 | 0.00% (0) |
| `id_confusion_l1::click_action` | 20 | 100.00% (20) | 20 / 0 | 0.00% (0) |
| `id_confusion_l1::click_name` | 20 | 100.00% (20) | 20 / 0 | 0.00% (0) |
| `letterletter_name::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `letterletter_name::click_name` | 20 | 80.00% (16) | 16 / 0 | 0.00% (0) |
| `mrn_transposition::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `mrn_transposition::click_name` | 20 | 40.00% (8) | 8 / 0 | 0.00% (0) |
| `near_surname::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `near_surname::click_name` | 20 | 60.00% (12) | 12 / 0 | 0.00% (0) |
| `nguyen_variant::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `nguyen_variant::click_name` | 20 | 80.00% (16) | 16 / 0 | 0.00% (0) |
| `same_name_diff_dob::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `same_name_diff_dob::click_name` | 20 | 80.00% (16) | 16 / 0 | 0.00% (0) |
| `same_surname_diff_first::click_action` | 20 | 0.00% (0) | 0 / 0 | 0.00% (0) |
| `same_surname_diff_first::click_name` | 20 | 80.00% (16) | 16 / 0 | 0.00% (0) |

## Worst collision class

Highest false-abort collision class: **`id_confusion_l1`** at 100.00% (40/40).

## Adjacent-row bleed

- Bands whose raw OCR lines included a token from a NEIGHBOUR row (above/below the resolved row): 147/360 (40.83%).
- Neighbour tokens that SURVIVED the `lines_near_point` row filter into the identity band: 0.
- Trials where the row filter CHANGED the false-abort verdict (i.e. bleed would have changed the decision without it): 50.

**Finding:** the `lines_near_point` row refinement absorbs adjacent-row bleed — neighbour tokens are picked up in the coarse 64px band but filtered out before the verdict, and removing the filter would change decisions in the count above.

## False accepts (headline safety finding)

**Zero.** No seeded sibling was verified as its target on the real dense-OCR'd surface, across every collision class, click config, and replay condition. The catastrophic direction held.

## Honest verdict (does the product clear the flagship bar?)

On the dense sibling surface the TRUE per-click false abort is **45.00%**, above the synthetic 26.17%. False accept stayed at 0 — the catastrophic wrong-patient direction held on real dense OCR, which is the number that gates the regulated-clinic buyer.

The availability cost (false abort) is a per-click hybrid-fallback escalation (~$0.10) or a human retry; it is the cheap direction and is the price paid for the zero-false-accept posture. Selection-bias disclosure: this is measured on THIS rendered fixture + RapidOCR, not 'in the world' — a different renderer, font stack, or OCR engine would shift the false-abort rate (and could, in principle, surface a false accept the frozen confusion table does not model).

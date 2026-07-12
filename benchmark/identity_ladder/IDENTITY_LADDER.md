# Integrated identity ladder — measured on the dense O/0-collapse surface

The full substrate-complete ladder, end to end: **structured text → pixel-compare → optional VLM veto → OCR name+DOB → halt**, fail-safe (any tier unsure → fall through; nothing verifies → HALT).

Surface: dense O/0-glyph-collapse (different-patient siblings) (5 pairs, each measured CORRECT-resolution and WRONG-resolution).

| Config | substrate | false-accept | over-halt |
|---|---|---:|---:|
| `structured` | browser/DOM (structured text) | 0/5 (0%) | 0/5 (0%) |
| `pixel_stable` | pixel-only, stable render | 0/5 (0%) | 0/5 (0%) |
| `pixel_drift_vlm_on` | pixel-only, drifted render, VLM ON | 0/15 (0%) | 7/15 (47%) |
| `pixel_drift_vlm_off` | pixel-only, drifted render, VLM OFF | 0/15 (0%) | 15/15 (100%) |

**Safety invariant — 0 false-accept across ALL configs: HOLDS.**

- The VLM tier is OPTIONAL: the default install runs structured-text + pixel-compare + OCR + halt with no model.
- VLM verdicts: ProbeFaithfulVLM reproducing benchmark/vlm_identity (PR #28): 100% detection / 0% false-accept on the OCR-collapse surface; same-value-drift over-halt dark 0%, zoom 33%, font 67%.
- `pixel_drift_vlm_off` over-halt is the disclosed residual (docs/LIMITS.md): under render drift with no VLM and no name+DOB carrier, a sole glyph-confusable identifier HALTS rather than risk a wrong-patient click.

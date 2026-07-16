# OpenAdapt arXiv paper

This directory contains the source for the OpenAdapt technical paper. It is a
submission draft, not a submitted paper. The draft deliberately carries no
named byline until every author has approved the author list and order.

## Submission blockers

- Replace the contributor placeholder with the agreed author list, order, and
  affiliations.
- Choose an arXiv category and complete any required endorsement.
- Record the commit and release artifact used for the final evaluation.
- Re-run every promoted experiment from the release candidate and archive the
  raw outputs.
- Add repeated longitudinal and cross-environment trials before making claims
  beyond the bounded studies currently reported.
- Complete a disclosure review for screenshots, logs, and third-party app data.

## Build

Requirements: Python 3.10+, `latexmk`, and a TeX distribution with `booktabs`,
`microtype`, and `hyperref`.

```bash
python paper/check_artifacts.py
make -C paper
```

The PDF is written to `paper/build/main.pdf`. `make -C paper clean` removes
generated files.

## Evidence contract

`check_artifacts.py` binds the raw benchmark results to the comparison artifact
and the headline numbers in the LaTeX draft. It does not re-run the
applications. The reproducibility section and `ARTIFACT_CHECKLIST.md`
distinguish source-backed, CI-reproducible, field, fixture, and one-run
descriptive evidence.

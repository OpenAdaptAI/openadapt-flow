# OpenAdapt arXiv paper

Source for the OpenAdapt technical paper and its workshop condensation. This is
a submission draft, not a submitted paper.

Everything mechanically checkable is done: `python paper/check_artifacts.py`
passes, `make -C paper` builds both PDFs with zero LaTeX warnings, and
`sh paper/make_arxiv_tarball.sh` produces a verified submission tarball.
`paper/ARTIFACT_CHECKLIST.md` records the evidence item by item.

**Mechanically ready is not scientifically finished.** The build gate is green
and the constants are bound to artifacts, but the draft's scope is itself an
open question — see "Scope decision" immediately below. Do not read the green
gate as "ready to submit anywhere."

---

## Scope decision (open, and it precedes the other five)

A separate line of work has produced an optimization result that the current
draft does not contain: under a sound out-of-band system-of-record oracle,
selecting for a screen-based proxy reward raised apparent success while true
persisted correctness fell. That result, plus a planned fine-tuning arm, could
either be **merged into this paper** (one strong submission) or **kept as a
second paper** with this one as the instrument.

The two shapes are not small variants of each other:

- **Merged.** The claim becomes a statement about optimization, this system
  becomes the *apparatus* rather than the subject, and the breadth material —
  the cross-system study, the cost/latency comparison, the substrate
  qualifications, the resolution-ladder system description — is **cut**, not
  carried. Roughly half the current draft goes.
- **Separate.** This paper stays broadly as written and targets a
  software-engineering venue; the optimization result becomes its own submission
  and cites this one for the metric.

The comparison of both routes, with verified venue dates, costs, the experiments
each requires, and the gates that would kill either cheaply, is in the private
decision memo `.private/PAPER_PLAN_ONE_SUBMISSION_2026-07-27.md`. **That memo
recommends; it does not decide.** Every recommendation below is conditioned on
which shape is chosen.

---

## Founder decisions (the things left before submission)

These require a human decision and cannot be discharged by an agent. Every other
checklist item is either closed with evidence or is an open *evidence* gap that
requires new experiments, not a decision.

0. **Paper scope and portfolio: one merged paper or two.** See "Scope decision"
   above. This one comes first because it changes decisions 2 and 5 and changes
   what the draft even is. It is not resolved here.
1. **Authorship.** Final author list, order, ORCIDs, affiliations, and
   corresponding author. Currently `Richard Abrich, OpenAdapt.AI (MLDSAI Inc.),
   richard@openadapt.ai` (`paper/main.tex:36`, also `pdfauthor`). If the list
   changes, update both. *Relevant to decision 0:* both adversarial reviews and
   both planning documents identify single, unaffiliated, commercially
   interested authorship as a structural disadvantage at every candidate venue.
   Dropping the human-subjects study from the plan removes the IRB dependency
   that previously made an academic co-author a heavier ask, so the cost of that
   conversation is now lower than it was. Still a decision, not a recommendation.
2. **arXiv primary category, cross-lists, and endorsement.** Recommendation and
   justification below — **it is now a rule keyed on decision 0, not a single
   category.** arXiv requires endorsement for a first submission to a category,
   and endorsement is **per subject class**, so a later change of primary
   category can require a fresh endorsement. Check the account's standing for
   *both* candidate primaries in one sitting; obtaining endorsement is not
   instant.
3. **arXiv licence.** Recommendation below. Unaffected by decision 0.
4. **Funding and conflict-of-interest statement.** The paper currently has none.
   The author develops the evaluated system; `06_limitations.tex` already says
   so in the threats-to-validity paragraph, but a formal COI/funding statement
   is a separate, venue-facing decision (footnote on page 1 vs. an unnumbered
   section before the bibliography). *Two additions if decision 0 goes to the
   merged paper:* any donated or credited compute used for the fine-tuning arm
   must be named in the funding statement, and the merged paper's own bias
   mitigation — this system does not appear as a winning arm — belongs in the
   COI paragraph, because it is the reason a favourable result was left out.
5. **Venue intent.** The workshop condensation (`paper/workshop/main.tex`) is
   written venue-neutral and uses `article`; retarget its document class once a
   specific workshop is chosen, and **confirm the workshop is non-archival**
   before submitting, or it can create a dual-submission problem with a later
   main-track version. The full report is formatted for arXiv rather than for
   any camera-ready style. Verified deadlines and the reachability analysis are
   in the private memo; the choice between the software-engineering route and
   the machine-learning route follows from decision 0 and is not made here.

### Recommended arXiv categories

**This is a rule, not a category, because the right answer depends on decision 0.**

The previous recommendation — `cs.SE` primary — rested on one stated premise:
*"both adversarial reviews independently concluded the paper has little novel
machine learning, so a machine-learning primary would put it in front of the
wrong reviewers."* That premise is true of the draft as it stands and false of
the merged paper. So the recommendation splits.

#### Case 1 — posting the draft substantially as it is (including as a v1 preprint ahead of a later merged version)

- **Primary: `cs.SE` (Software Engineering).** Unchanged, and for the original
  reason. The contribution is a systems and evaluation one: a runtime that treats
  an out-of-band system-of-record read as a first-class execution gate, and a
  reliability metric (silent incorrect success jointly with over-halt) for
  automation. Its intellectual lineage is the test-oracle problem, runtime
  verification, the end-to-end argument, and transactional idempotency hazards.
  There is no gradient anywhere in this draft; claiming a machine-learning
  primary for it would be an overclaim, arXiv moderators do reclassify, and a
  mis-primaried preprint is a poor first impression in precisely the community
  the work is trying to persuade.
- **Cross-list `cs.LG` (Machine Learning).** New, and worth it even in this case:
  it puts v1 in front of the audience that will need to have seen it when the
  optimization result lands, and it makes any later change of primary a smaller
  step.
- **Cross-list `cs.PL` (Programming Languages).** Justified, not decorative: the
  compiler performs inductive program synthesis from demonstrations with explicit
  quarantine of underdetermined induction, and the paper argues in Related Work
  and again in the Conclusion that a sound out-of-band effect oracle removes the
  binding constraint on generate-and-check for programs whose correctness is
  defined by an effect on the world. That is an argument aimed squarely at the
  synthesis community, and it is positioned against PROSE/FlashMeta,
  version-space algebra, CEGIS, SyGuS, and WebRobot.
- **Cross-list `cs.HC` (Human-Computer Interaction).** The demonstration-recording
  loop, the one-shot operator confirm for inferred parameters, the interactive
  disambiguation of underdetermined induction, and the human-only gates in the
  repair promotion lifecycle are HCI contributions, and the PBD lineage
  (SUGILITE, Rousillon, DiLogics) lives here.
- **Optional cross-list `cs.AI`.** Only if you want the computer-use-agent
  audience to see it. The paper measures against a computer-use agent baseline
  and argues that task-success benchmarks mismeasure reliability, which is a
  message for that community — but this draft contributes no model or training
  method, so it is reach, not fit.

#### Case 2 — the merged paper, once the optimization result and a training arm are actually in it

- **Primary: `cs.LG` (Machine Learning).** The headline becomes a statement about
  *what optimization does to a policy* — reward misspecification, proxy-versus-
  gold overoptimization, rejection-sampling fine-tuning. Its direct lineage is
  the reward-misspecification and reward-model-overoptimization literature, which
  lives in `cs.LG`. Those are the reviewers who can adjudicate whether a turndown
  curve is real, whether the best-of-n parameterisation is applied correctly, and
  whether the fine-tuning control is the right control; no software-engineering
  reader can. Note this is a *promotion of the ML claim*, not a demotion of the
  systems work: the oracle is what makes the ML result measurable at all.
- **Not `cs.AI` primary.** arXiv's own taxonomy defers machine learning from
  `cs.AI` to `cs.LG`. A Goodhart result is machine learning.
- **Cross-list `cs.AI`** — and in the merged paper this stops being "reach." The
  computer-use-agent community is the community whose evaluation and optimization
  practice the paper criticises; it has to see it.
- **Cross-list `cs.SE`** — the oracle, the coverage principle, the pinned
  digest-verified environments, and the artifact discipline. Also the audience
  for any software-engineering-venue version of the work.
- **Drop `cs.PL` and `cs.HC`.** Both were justified by material the merged paper
  cuts: `cs.PL` by the inductive-synthesis and quarantine argument, `cs.HC` by
  the recording loop and the repair lifecycle. Once those sections are gone the
  cross-lists are decorative, and a decorative cross-list is a small but real
  credibility cost with the moderators.

**Endorsement, in both cases.** Endorsement is per subject class. If the primary
moves from `cs.SE` to `cs.LG` between a v1 preprint and the merged version, that
can require a separate `cs.LG` endorsement, and obtaining one is not instant.
Check the account's standing for both classes now rather than in the week of a
deadline.

### Recommended licence

**CC BY 4.0.** The engine is MIT and the argument depends on being quotable and
reusable; a non-commercial or no-derivatives licence would conflict with the
open-core positioning and would prevent the failure taxonomy and metric
definitions from being adopted by others, which is the stated goal. Avoid
`arXiv.org perpetual, non-exclusive license` if you want third parties to be
able to reuse the taxonomy directly.

---

## Build

Requirements: Python 3.10+, `latexmk`, and a TeX distribution with `booktabs`,
`microtype`, `hyperref`, `amssymb`, `array`, `tikz`, and `pgfplots`
(Debian/Ubuntu: `texlive-latex-extra texlive-pictures texlive-science`).

```bash
python paper/check_artifacts.py
make -C paper
```

`make -C paper` gate-checks every headline constant and then builds two PDFs
from the same constants:

- `paper/build/main.pdf` — the full technical report (canonical artifact), 21
  pages including the bibliography.
- `paper/workshop/build/main.pdf` — the workshop condensation
  (`paper/workshop/main.tex`), 6 pages, reframed around the silent-wrong-effect
  finding. It shares `references.bib` via a byte-identical copy (a regular file,
  not a symlink, so the sdist packages cleanly) and the same benchmark
  constants; `check_artifacts.py` binds both and asserts the two bib files stay
  identical.

A clean checkout builds both with **zero LaTeX warnings** — no overfull or
underfull boxes, no undefined citations, no undefined references. Treat any new
warning as a defect.

`make -C paper clean` removes generated files (`build/`, `workshop/build/`,
`dist/`).

## arXiv submission

```bash
make -C paper arxiv            # -> paper/dist/arxiv-main.tar.gz
make -C paper arxiv-workshop   # -> paper/dist/arxiv-workshop.tar.gz
```

`paper/make_arxiv_tarball.sh` is fail-loud. It gate-checks the constants, does a
clean build to produce `main.bbl` (arXiv does not reliably run `bibtex`, so the
`.bbl` is shipped and `references.bib` is not), stages only the files the
document actually `\input`s, recompiles that staged tree standalone in a scratch
directory, refuses to emit a tarball if any citation or reference is undefined,
strips auxiliary files, and prints the tarball's SHA-256 and the source commit.

Submission steps, in order:

1. Close the scope decision (0) and then the five founder decisions above. The
   scope decision comes first: it determines the primary category, and if it goes
   to the merged paper it changes the title, the abstract, and roughly half the
   sections, so building a tarball before it is settled builds the wrong paper.
   Set the byline and `pdfauthor` in `paper/main.tex` and
   `paper/workshop/main.tex` before building.
2. Confirm the paper CI workflow is green on the exact commit you intend to
   submit (`.github/workflows/paper.yml`). Do not assume a green run from an
   earlier commit still holds.
3. `make -C paper clean && make -C paper arxiv`. Record the printed SHA-256 and
   source commit in `paper/ARTIFACT_CHECKLIST.md` under "Release identity".
4. At <https://arxiv.org/submit>, start a new submission and upload
   `paper/dist/arxiv-main.tar.gz`. Do **not** upload a PDF; arXiv compiles the
   source.
5. Select the primary category and cross-lists (recommendation above). If you
   lack endorsement for the primary category, request it before proceeding — a
   submission held for endorsement is not queued.
6. Choose the licence (recommendation above).
7. Paste the abstract. It must match `paper/main.tex` exactly, minus LaTeX
   markup: arXiv's abstract field is plain text, so replace `\emph{...}` with the
   bare words and `---` with an em dash. Do not paraphrase; the abstract carries
   machine-checked constants.
8. Verify arXiv's generated PDF page-for-page against `paper/build/main.pdf`
   before announcing. arXiv's TeX Live version may differ from yours; the tarball
   is deliberately package-light (`booktabs`, `microtype`, `hyperref`, `amssymb`, `array`,
   `tikz`, `pgfplots`, `xcolor`, `lmodern`, `geometry`) to
   reduce that risk, and `pgfplots` is pinned with `\pgfplotsset{compat=1.13}`.
9. After announcement, record the arXiv identifier in
   `paper/ARTIFACT_CHECKLIST.md` and cite it from the repository README.

## Evidence contract

`check_artifacts.py` binds each headline number to a released raw result or a
bounded aggregate summary. It does not re-run the applications and does not imply
that all underlying evaluation rows are public. Grown corpora,
deployment-derived tuning, target-specific recipes, and raw private evaluation
rows stay outside the public release. The reproducibility section and
`ARTIFACT_CHECKLIST.md` distinguish source-backed, CI-reproducible, field,
fixture, and one-run descriptive evidence.

The check establishes **transcription fidelity only** — that the prose faithfully
cites the released evidence. It cannot distinguish a sound measurement from an
unsound one, and the paper says so in its own contributions list. It additionally
enforces two negative constraints: the retired, circular in-process
`silent_wrong_action` result may not appear in any paper prose, and the
N=0-external-deployments and Citrix-stand-in disclosures may not be removed.

## Review record

`REVIEW_ADVERSARIAL.md` and `REVIEW_ADVERSARIAL_2.md` are the two adversarial
peer reviews the current draft was hardened against. They are retained
deliberately: they are the record of what was wrong and what was fixed, and they
are review-only deliverables that have not been submitted anywhere.

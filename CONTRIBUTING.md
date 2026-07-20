# Contributing to openadapt-flow

Thanks for your interest in improving openadapt-flow. This project compiles a
recorded GUI demonstration into a deterministic, self-healing, locally-run
script — so correctness, determinism, and honest measurement matter more here
than raw feature count.

## Development setup

```bash
git clone https://github.com/OpenAdaptAI/openadapt-flow && cd openadapt-flow
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
pytest -q
```

Python 3.10–3.12 are supported and exercised in CI.

## The checks CI runs (run them locally first)

```bash
ruff check openadapt_flow          # lint
ruff format --check openadapt_flow # format (drop --check to auto-apply)
mypy                               # type-check (config in pyproject.toml)
pytest -q                          # tests
```

- **Lint/format:** `ruff`. Config lives in `[tool.ruff]` in `pyproject.toml`.
- **Types:** `mypy` runs on the core package (not tests). It is deliberately
  lenient today; a set of modules with known type debt is listed under
  `[[tool.mypy.overrides]]`. Improving a module's annotations and removing it
  from that list is a very welcome PR.
- **Coverage:** CI reports coverage for visibility. There is no hard floor yet,
  but new code should come with tests.

## Pull request guidelines

- **Conventional Commits** for titles and commits: `feat:`, `fix:`, `perf:`,
  `docs:`, `ci:`, `chore:`, `refactor:`, `test:`. Releases are automated from
  these — `feat:` → minor, `fix:`/`perf:` → patch, `BREAKING CHANGE` → major.
- Keep PRs focused. Separate mechanical changes (formatting, renames) from
  behavior changes so review stays legible.
- Add or update tests for any behavior change. The suite mocks browsers/servers
  where it can, so most of it runs with no live VM.
- Update docs (`README.md`, `DESIGN.md`, `docs/`) when behavior or contracts
  change. We prefer honest, measured claims — if something is experimental, say
  so.

## Licensing and vendored files

`openadapt-flow` package artifacts are MIT-licensed. Do not copy, adapt, vendor,
embed, or redistribute GPL, AGPL, LGPL, SSPL, source-available, or
field-of-use-restricted material in the wheel or source distribution without
explicit reviewed approval from qualified licensing counsel.

OpenAdapt-specific non-negotiable: do not ship AGPL benchmark files in a PyPI
wheel or sdist. The openIMIS reference environment and any other copied or
adapted AGPL benchmark material must remain repository-only or be obtained
through a pinned, hash-verified, opt-in upstream fetch.

Running or automating an external copyleft application is not the same as
redistributing its source. For reference environments, prefer an opt-in fetch of
the exact pinned, hash-verified upstream project. If repository-only benchmark
material has a different file-local license, preserve its full license,
provenance, modification notice, and source hashes, and exclude the entire
surface from permissively licensed package artifacts.

The release-consistency gate inspects the actual wheel and sdist. A source-tree
notice alone is not sufficient.

## Contributor License Agreement (CLA) and DCO

So that OpenAdapt (MLDSAI Inc.) can steward the project and retain future
relicensing flexibility, every contribution must be covered by both:

1. **DCO sign-off (required now):** sign off every commit with `git commit -s`,
   which adds a `Signed-off-by` line certifying the Developer Certificate of
   Origin (https://developercertificate.org).
2. **Contributor License Agreement:** by opening a pull request you agree to the
   [OpenAdapt CLA](CLA.md). `openadapt-flow/CLA.md` is the canonical CLA text for
   all OpenAdapt OSS repositories. When the CLA Assistant check is enabled,
   first-time contributors sign once by commenting on their PR.

This is *outbound* licensing of your contribution to the project; it is separate
from the *inbound* third-party rules above.

## Source-availability boundary (open-core)

`openadapt-flow` is the open engine. Do not add private crown-jewel artifacts —
the *grown* hardening failure corpus, *tuned* metamorphic-adversary params,
deployment-derived *thresholds*, per-system-of-record oracle/connector *recipes*,
or *real-EMR* datasets — to this public repository. The harness/mechanism is
public; that data lives in the private corpus repo. The release-consistency gate
also fails a build that would carry those private paths.

## Safety-sensitive areas

The identity gate, the resolution ladder, and the postcondition/halt logic are
the safety core: the whole value proposition is that the tool halts instead of
acting on the wrong target. Changes there deserve extra tests (see the
`test_identity_*`, `test_resolver*`, and `*_fuzz` suites) and a clear
explanation of why the never-false-accept invariant still holds.

## Reporting security issues

See [SECURITY.md](SECURITY.md) — do not file security problems as public issues.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

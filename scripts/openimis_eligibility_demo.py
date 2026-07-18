"""openIMIS coverage / eligibility-check reference demo (effect-verified).

Drives the INSURANCE reference environment in ``benchmark/openimis_claims``
through a front-office **coverage verification**: look up a policyholder by
insuree number in openIMIS's Insuree Enquiry, confirm the policy panel and a
service-eligibility answer, and certify the result against the SYSTEM OF
RECORD -- a read-only SQL effect contract on the policy row itself
(``docs/EFFECT_KIT.md``), never the pixels.

1. ``up``         start the pinned stack (loopback-only, synthetic demo data)
2. ``bootstrap``  synthetic policyholders (one in force, one LAPSED, one more
                  in force) + the read-only SQL-oracle role
3. ``record``     scripted demonstration of the enquiry lookup, captured
                  through ``Recorder`` (frames + events; replay never uses
                  selectors)
4. ``compile``    recording -> bundle, with the coverage effect contracts
                  bound to the run's ``insurance_no`` parameter
5. ``replay``     replay the bundle for any policyholder; every run is
                  verified by the SQL effect verifier built from
                  ``deployment.eligibility.yaml``. A policyholder whose
                  coverage is NOT in force REFUTES the contract and the run
                  HALTS -- the demo's halt-on-anomaly moment
                  (``--insuree 999000002 --expect-halt``)
6. ``down``       stop the stack

Why the effect contract is the point: the enquiry dialog for the LAPSED
policyholder still renders a service-eligibility thumbs-up -- a screen a
human (or a screen-scraping bot) can misread as "covered". The system-of-
record read refuses: coverage != Active -> REFUTED -> HALT with evidence.

Record time is allowed to cheat with Playwright locators (to find pixel
coordinates via ``bounding_box()``); every action is performed through
``Recorder``. Authentication is declared unmeasured setup, mirroring the
claims demo's setup boundary. ``--record-video`` opt-in captures a WebM for
the media pipeline; it never changes what is recorded or replayed.

All data is synthetic. Any published screenshot must be labelled as the
"openIMIS reference environment" and must not imply a customer deployment.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from openimis_claims.fixture import (  # noqa: E402
    ACTOR_PASSWORD,
    ACTOR_USER,
    LAPSED_CHF,
    ORACLE_PASSWORD_ENV,
    POLICYHOLDER_CHF,
    SECOND_ACTIVE_CHF,
    FixtureError,
    OpenIMISFixture,
)

HERE = Path(__file__).resolve().parent
DEPLOYMENT_YAML = (
    REPO_ROOT / "benchmark" / "openimis_claims" / ("deployment.eligibility.yaml")
)

SERVICE_QUERY = "General"
SERVICE_OPTION = "General Consultation"
SETTLE = {
    "settle_timeout_s": 10.0,
    "settle_stable_frames": 3,
    "settle_interval_s": 0.3,
}


def eligibility_effects() -> list[Any]:
    """The coverage contracts bound to the run's ``insurance_no`` parameter.

    Substrate-neutral (``docs/EFFECT_KIT.md``); the committed
    ``deployment.eligibility.yaml`` wires them to the read-only SQL verifier
    over openIMIS's own PostgreSQL policy tables.
    """
    from openadapt_flow.runtime.effects import Effect, ValueExpr

    chf = ValueExpr(param="insurance_no")
    return [
        Effect(
            kind="record_written",
            match={"chf_id": chf},
            expected_count=1,
            risk="reversible",
            probe=(
                "exactly one in-force policy row exists for the checked policyholder"
            ),
        ),
        Effect(
            kind="field_equals",
            match={"chf_id": chf},
            field="coverage",
            value=ValueExpr(literal="Active"),
            risk="reversible",
            probe=(
                "the checked policyholder's policy is Active and unexpired in "
                "the system of record"
            ),
        ),
    ]


def _center(locator: Any) -> tuple[int, int]:
    locator.wait_for(state="visible", timeout=30_000)
    box = locator.bounding_box()
    if box is None:
        raise FixtureError("target control has no bounding box")
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def _login(page: Any, fixture: OpenIMISFixture) -> None:
    """Unmeasured setup: authenticate and settle the home layout.

    The home app bar assembles asynchronously (the language selector, the
    welcome banner, and the tasks drawer each land on their own GraphQL
    round-trip) and the menu row RE-WRAPS as they land, moving the enquiry
    field. Record and replay must both demonstrate from the FINAL layout, so
    setup waits for the late-loading landmarks before handing control to the
    measured demonstration.
    """
    page.goto(fixture.front_url, wait_until="networkidle", timeout=60_000)
    page.locator("input[type=text]").fill(ACTOR_USER)
    page.locator("input[type=password]").fill(ACTOR_PASSWORD)
    page.get_by_role("button", name="LOG IN").click()
    page.wait_for_url("**/home", timeout=60_000)
    page.wait_for_load_state("networkidle")
    page.get_by_text("ENGLISH").first.wait_for(state="visible", timeout=30_000)
    page.get_by_text("Welcome Admin").first.wait_for(state="visible", timeout=30_000)
    page.wait_for_timeout(2_500)


def _demonstrate_eligibility(recorder: Any, page: Any, *, insuree: str) -> None:
    """The recorded demonstration: one coverage / eligibility check."""
    # Look up the policyholder: the app-bar Insuree Enquiry resolves the
    # insuree and their policy panel from the insuree number alone.
    enquiry = page.get_by_placeholder("Insuree enquiry")
    recorder.click(*_center(enquiry))
    recorder.type_text(insuree, param="insurance_no")
    recorder.press("Enter")
    # Record-time cheat (waits only): the enquiry dialog must render the
    # policyholder's policy table before the demonstration continues.
    dialog = page.locator("[role=dialog]").first
    dialog.wait_for(state="visible", timeout=30_000)
    dialog.get_by_text("Policies").first.wait_for(state="visible", timeout=30_000)
    page.wait_for_timeout(1_500)

    # Ask the service-eligibility question a front office asks ("is this
    # visitor covered for this service?").
    service = page.get_by_placeholder("Search Service").first
    recorder.click(*_center(service))
    recorder.type_text(SERVICE_QUERY)
    option = page.locator("[role=option]", has_text=SERVICE_OPTION).first
    option.wait_for(state="visible", timeout=30_000)
    recorder.click(*_center(option))
    # The eligibility indicator renders next to the selected service.
    page.wait_for_timeout(2_000)


def _launch(fixture: OpenIMISFixture, *, headed: bool, video_dir: str | None):
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    return PlaywrightBackend.launch(
        fixture.front_url,
        headless=not headed,
        record_video_dir=video_dir,
    )


def _finish_video(backend: Any, close: Callable[[], None], out_hint: str) -> None:
    video = getattr(backend.page, "video", None)
    close()
    if video is not None:
        try:
            print(f"{out_hint} video:", video.path())
        except Exception:  # noqa: BLE001 - videos are best-effort media
            pass


def _stage_oracle_password(fixture: OpenIMISFixture) -> None:
    """Surface the oracle role's generated secret as the kit's env reference.

    The committed deployment YAML names ``OPENIMIS_ORACLE_PASSWORD`` (secrets
    are references, never literals); the fixture's generated secret lives in
    the ignored ``out/state/secrets.json``. An operator-set value wins.
    """
    os.environ.setdefault(ORACLE_PASSWORD_ENV, fixture.oracle_password())


def _build_verifier(params: dict[str, str]) -> Any:
    from openadapt_flow.deployment import build_effect_verifier, load_deployment

    config = load_deployment(DEPLOYMENT_YAML)
    verifier = build_effect_verifier(config.effects, params)
    if verifier is None:
        raise FixtureError(f"{DEPLOYMENT_YAML} wired no effect verifier")
    return verifier


def cmd_up(args: argparse.Namespace) -> int:
    fixture = OpenIMISFixture(http_port=args.port)
    print(f"starting pinned openIMIS stack on {fixture.base_url} ...")
    fixture.up()
    print("stack ready:", fixture.front_url)
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    fixture = OpenIMISFixture(http_port=args.port)
    holders = fixture.bootstrap_eligibility()
    for chf, holder in holders.items():
        print(f"synthetic policyholder {chf}:", holder)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    fixture = OpenIMISFixture(http_port=args.port)
    fixture.down(volumes=args.volumes)
    print("stack stopped")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from openadapt_flow.recorder import Recorder

    out_dir = Path(args.out)
    if out_dir.exists():
        raise FixtureError(f"recording path already exists: {out_dir}")
    fixture = OpenIMISFixture(http_port=args.port)
    coverage = fixture.coverage(args.insuree)
    if coverage["coverage"] != "Active":
        raise FixtureError(
            f"demonstration insuree {args.insuree} is not in force "
            f"({coverage}); demonstrate on an ACTIVE policyholder"
        )
    backend, close = _launch(fixture, headed=args.headed, video_dir=args.record_video)
    try:
        _login(backend.page, fixture)
        recorder = Recorder(backend, out_dir, app_url=backend.page.url, **SETTLE)
        _demonstrate_eligibility(recorder, backend.page, insuree=args.insuree)
        recording = recorder.finish()
        print("recording:", recording)
        print("oracle coverage row:", coverage)
    finally:
        _finish_video(backend, close, "record")
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler import compile_recording

    bundle_dir = Path(args.bundle)
    if bundle_dir.exists():
        raise FixtureError(f"bundle path already exists: {bundle_dir}")
    workflow = compile_recording(
        Path(args.recording), bundle_dir, name="openimis-eligibility-check"
    )
    if not workflow.steps:
        raise FixtureError("compiler produced no steps")
    # Bind the coverage contracts to the LAST demonstrated step: verification
    # runs after the eligibility answer is on screen, certifying the check
    # against the system of record before the run may report success.
    workflow.steps[-1].effects = eligibility_effects()
    workflow.save(bundle_dir)
    print("bundle:", bundle_dir)
    print(
        "effect contracts:",
        [e.probe for e in workflow.steps[-1].effects],
    )
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    fixture = OpenIMISFixture(http_port=args.port)
    insuree = args.insuree
    expected = fixture.coverage(insuree)  # fail fast if bootstrap has not run
    _stage_oracle_password(fixture)
    params = {"insurance_no": insuree}
    verifier = _build_verifier(params)
    bundle_dir = Path(args.bundle)
    run_dir = Path(
        args.run_dir
        or "runs/openimis-eligibility-"
        f"{_dt.datetime.now(tz=_dt.timezone.utc):%Y%m%dT%H%M%SZ}"
    )
    backend, close = _launch(fixture, headed=args.headed, video_dir=args.record_video)
    started = time.monotonic()
    try:
        _login(backend.page, fixture)
        report = Replayer(backend, effect_verifier=verifier).run(
            Workflow.load(bundle_dir),
            params=params,
            bundle_dir=bundle_dir,
            run_dir=run_dir,
        )
    finally:
        _finish_video(backend, close, "replay")
    wall_s = time.monotonic() - started
    from openadapt_flow.report import render_run_report

    report_md = render_run_report(run_dir)
    print("replay success:", report.success)
    print("run dir:", run_dir)
    print("run report:", report_md)
    print(f"replay wall time: {wall_s:.1f}s")
    print("system-of-record coverage row:", expected)
    for result in report.results:
        if result.effect_contract_hashes:
            print(
                f"step {result.step_id}: effect_verified={result.effect_verified} "
                f"contracts={result.effect_contract_hashes}"
            )
    if args.expect_halt:
        if report.success:
            print(
                "error: expected the coverage contract to HALT this replay, "
                "but it succeeded",
                file=sys.stderr,
            )
            return 1
        print(
            "HALT demonstrated: the system-of-record contract refused to "
            "certify coverage (see the run report's effect-verification "
            "section)"
        )
        return 0
    return 0 if report.success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        default=OpenIMISFixture().http_port,
        help="loopback port the pinned stack serves on",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("up", help="start the pinned stack and wait until ready")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser(
        "bootstrap",
        help=(
            "create the synthetic policyholders (active + lapsed) and the "
            "read-only SQL-oracle role (idempotent)"
        ),
    )
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("record", help="record the scripted eligibility check")
    p.add_argument("--out", required=True, help="recording output directory")
    p.add_argument(
        "--insuree",
        default=POLICYHOLDER_CHF,
        help="insuree number to demonstrate on (must be in force)",
    )
    p.add_argument("--headed", action="store_true", help="run the browser headed")
    p.add_argument(
        "--record-video",
        default=None,
        metavar="DIR",
        help="OPT-IN: capture a WebM of the session into DIR (media pipeline)",
    )
    p.set_defaults(func=cmd_record)

    p = sub.add_parser(
        "compile",
        help="compile a recording into a bundle with the coverage contracts",
    )
    p.add_argument("--recording", required=True)
    p.add_argument("--bundle", required=True)
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser(
        "replay",
        help=(
            "replay the eligibility check for any policyholder; the SQL "
            "effect verifier certifies (or refuses) the coverage answer. "
            f"--insuree {LAPSED_CHF} --expect-halt demonstrates the halt"
        ),
    )
    p.add_argument("--bundle", required=True)
    p.add_argument(
        "--insuree",
        default=SECOND_ACTIVE_CHF,
        help=(
            "insuree number to check (default: the in-force policyholder the "
            "demonstration never saw)"
        ),
    )
    p.add_argument("--run-dir", default=None)
    p.add_argument("--headed", action="store_true", help="run the browser headed")
    p.add_argument(
        "--record-video",
        default=None,
        metavar="DIR",
        help="OPT-IN: capture a WebM of the session into DIR (media pipeline)",
    )
    p.add_argument(
        "--expect-halt",
        action="store_true",
        help=(
            "assert the replay HALTS (exit 0 on halt, 1 on unexpected "
            "success) -- the halt-on-anomaly demonstration"
        ),
    )
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("down", help="stop the stack")
    p.add_argument("--volumes", action="store_true", help="also remove data volumes")
    p.set_defaults(func=cmd_down)

    args = parser.parse_args()
    try:
        return args.func(args)
    except FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""openadapt-flow CLI.

Subcommands (thin wrappers over the module APIs; sibling modules are
imported lazily inside each handler so ``--help`` always works):

- ``record`` — open a headed browser on your OWN app (``--url``) and
  record what you do into the format ``compile`` consumes.
- ``demo-record`` — serve MockMed locally and record the canonical demo.
- ``compile`` — compile a recording directory into a workflow bundle.
- ``replay`` — replay a bundle; serves the bundled MockMed demo app when no
  ``--url`` is given (with optional ``--drift`` to demonstrate healing).
- ``bench`` — replay a bundle N times against MockMed and aggregate.
- ``lint`` — report a bundle's coverage gaps (advice; exit code by severity).
- ``certify`` — enforce a safety policy on a bundle (refuse it if it fails).
- ``emit-skill`` — emit an Agent Skills folder for a bundle.
- ``emit-mcp`` — emit a standalone MCP ``server.py`` for a bundle.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

_VIEWPORT = {"width": 1280, "height": 800}


def _parse_params(pairs: Sequence[str] | None) -> dict[str, str]:
    """Parse repeated ``--param k=v`` flags into a dict."""
    params: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--param expects k=v, got {pair!r}")
        key, value = pair.split("=", 1)
        params[key] = value
    return params


def _with_drift(url: str, drift: str | None) -> str:
    """Append a ``?drift=...`` query to a MockMed base URL."""
    if not drift:
        return url
    return f"{url.rstrip('/')}/?drift={drift}"


def _cmd_record(args: argparse.Namespace) -> int:
    from openadapt_flow.interactive_recorder import record_interactive

    out = record_interactive(
        args.url,
        Path(args.out),
        secret_fields=tuple(args.secret or ()),
        param_fields=tuple(args.param or ()),
        headless=args.headless,
    )
    print(f"Recording written to {out}")
    secrets = sorted(args.secret or ())
    if secrets:
        print(
            "Secret field(s) recorded (values NOT stored): "
            + ", ".join(secrets)
            + ". At replay, export "
            + ", ".join(f"OPENADAPT_FLOW_SECRET_{name.upper()}" for name in secrets)
        )
    return 0


def _cmd_demo_record(args: argparse.Namespace) -> int:
    from openadapt_flow.demo_driver import record_triage_demo
    from openadapt_flow.mockmed.server import serve

    url, stop = serve(port=0)
    try:
        url = _with_drift(url, args.drift)
        out = record_triage_demo(
            url,
            Path(args.out),
            note_text=args.note_text,
            param_name=args.param_name,
            headed=args.headed,
        )
        print(f"Recording written to {out}")
    finally:
        stop()
    return 0


def _cmd_compile(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler import compile_recording

    workflow = compile_recording(
        Path(args.recording), Path(args.out), name=args.name
    )
    print(
        f"Compiled {len(workflow.steps)} steps into {args.out} "
        f"(workflow: {workflow.name!r})"
    )
    return 0


def _default_run_dir() -> Path:
    """Timestamped default run directory under ``runs/``."""
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Path("runs") / f"replay-{stamp}"


def _cmd_replay(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import Workflow
    from openadapt_flow.report import render_run_report
    from openadapt_flow.runtime import Replayer

    bundle = Path(args.bundle)
    run_dir = Path(args.run_dir) if args.run_dir else _default_run_dir()
    workflow = Workflow.load(bundle)
    params = _parse_params(args.param)

    if args.url and args.drift:
        raise SystemExit(
            "--drift only applies to the bundled MockMed demo app; "
            "omit --url to use it (drift your own app for real)."
        )

    stop = None
    url = args.url
    if url is None:
        from openadapt_flow.mockmed.server import serve

        url, stop = serve(port=0)
        url = _with_drift(url, args.drift)
        drift_note = f" (drift: {args.drift})" if args.drift else ""
        print(f"No --url given; replaying against bundled MockMed{drift_note}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport=_VIEWPORT)
            page.goto(url)
            try:
                backend = PlaywrightBackend(page)
                import os

                from openadapt_flow.runtime.grounder import build_grounder
                from openadapt_flow.runtime.remote_vlm import (
                    appliance_from_env,
                )

                # An on-prem VLM appliance is opt-in (OPENADAPT_FLOW_VLM_URL).
                # Unset -> the identity veto tier stays off. Configured -> the
                # identity veto tier and the remote-VLM grounder come online,
                # both fail-safe (an appliance outage halts, never mis-clicks).
                appliance = appliance_from_env()
                if appliance is not None:
                    print(
                        "Using on-prem VLM appliance at "
                        f"{os.environ.get('OPENADAPT_FLOW_VLM_URL')} "
                        "(identity veto tier + remote-VLM grounder fallback; "
                        "fail-safe to halt)"
                    )
                # Grounding rung: OCR text-anchoring (openadapt-grounding) is
                # PRIMARY whenever the 'grounding' extra is installed; the
                # remote-VLM grounder (if an appliance is configured) is the
                # fallback for text-less surfaces. None when neither is present
                # (the model-free default; ladder simply has no grounder rung).
                grounder = build_grounder(
                    fallback=appliance.grounder if appliance else None
                )
                if grounder is not None:
                    print(f"Grounding rung active: {type(grounder).__name__}")
                report = Replayer(
                    backend,
                    grounder=grounder,
                    identity_vlm=appliance.identity_vlm if appliance else None,
                    state_verifier=(
                        appliance.state_verifier if appliance else None
                    ),
                    # Normal replay prefers the deterministic structural rung.
                    # ``--drift`` exists to DEMONSTRATE the visual healing ladder
                    # on the bundled MockMed app, so it forces the visual floor
                    # (structure would resolve the injected drift and there would
                    # be nothing to heal -- the very thing the flag shows).
                    use_structural=not bool(args.drift),
                ).run(
                    workflow,
                    params=params,
                    bundle_dir=bundle,
                    run_dir=run_dir,
                    save_healed_to=(
                        Path(args.save_healed_to)
                        if args.save_healed_to
                        else None
                    ),
                )
            finally:
                browser.close()
    finally:
        if stop is not None:
            stop()

    report_md = render_run_report(run_dir)
    outcome = "success" if report.success else "FAILED"
    print(f"Replay {outcome}: {report_md}")
    return 0 if report.success else 1


def _cmd_bench(args: argparse.Namespace) -> int:
    from contextlib import contextmanager

    from openadapt_flow.bench import run_bench
    from openadapt_flow.mockmed.server import serve
    from openadapt_flow.report import render_bench_report

    url, stop = serve(port=0)
    target_url = _with_drift(url, args.drift)

    @contextmanager
    def backend_factory():
        from playwright.sync_api import sync_playwright

        from openadapt_flow.backends.playwright_backend import (
            PlaywrightBackend,
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport=_VIEWPORT)
            page.goto(target_url)
            try:
                yield PlaywrightBackend(page)
            finally:
                browser.close()

    run_root = Path(args.run_root)
    try:
        result = run_bench(
            Path(args.bundle),
            backend_factory,
            args.n,
            params=_parse_params(args.param),
            run_root=run_root,
        )
    finally:
        stop()

    report_md = render_bench_report(
        run_root / "bench.json", run_root / "BENCH.md"
    )
    print(
        f"Bench: {result['success_count']}/{result['n']} succeeded "
        f"(p50 {result['total_ms_p50']:.0f} ms) — {report_md}"
    )
    return 0 if result["success_count"] == result["n"] else 1


def _cmd_benchmark(args: argparse.Namespace) -> int:
    from openadapt_flow.benchmark.run_benchmark import run_benchmark

    results = run_benchmark(
        Path(args.out),
        n_compiled=args.n_compiled,
        n_agent=args.n_agent,
        note_text=args.note_text,
        headed=args.headed,
    )
    compiled = results["arms"]["compiled"]
    agent = results["arms"]["agent"]
    print(
        f"compiled: {compiled['success_count']}/{compiled['n']} "
        f"(p50 {compiled['wall_s_p50']:.1f}s, $0/run) | "
        f"agent: {agent['success_count']}/{agent['n']} "
        f"(p50 {agent['wall_s_p50']:.1f}s, "
        f"${agent['cost_usd_per_run']:.4f}/run)"
    )
    return 0


def _cmd_lint(args: argparse.Namespace) -> int:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.policy import SEVERITY_ORDER, lint_workflow

    workflow = Workflow.load(Path(args.bundle))
    report = lint_workflow(workflow)
    print(report.render())
    # Exit code by max severity: nonzero once anything reaches `error`
    # (an unarmed or vacuous IRREVERSIBLE step). `--strict` also fails on warn.
    threshold = "warn" if args.strict else "error"
    fail = SEVERITY_ORDER[report.max_severity] >= SEVERITY_ORDER[threshold]
    return 1 if (report.findings and fail) else 0


def _cmd_certify(args: argparse.Namespace) -> int:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.policy import evaluate_policy, load_policy

    workflow = Workflow.load(Path(args.bundle))
    try:
        policy = load_policy(args.policy)
    except (FileNotFoundError, ValueError) as e:
        raise SystemExit(str(e))
    report = evaluate_policy(workflow, policy)
    print(report.render())
    # A failing certification exits nonzero so CI / deploy gates refuse the
    # bundle — the whole point of making "runnable" distinct from "certified".
    return 0 if report.passed else 2


def _cmd_emit_skill(args: argparse.Namespace) -> int:
    from openadapt_flow.emit.skill import emit_skill

    skill_dir = emit_skill(Path(args.bundle), Path(args.out))
    print(f"Skill written to {skill_dir}")
    return 0


def _cmd_emit_mcp(args: argparse.Namespace) -> int:
    from openadapt_flow.emit.mcp_tool import emit_mcp_server

    server_path = emit_mcp_server(Path(args.bundle), Path(args.out))
    print(f"MCP server written to {server_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="openadapt-flow",
        description=(
            "Record a workflow once, compile it into a deterministic "
            "vision-anchored script, replay it locally, heal it on drift."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "record",
        help="Record YOUR app interactively in a headed browser (--url)",
    )
    p.add_argument(
        "--url", required=True, help="URL of the app to record against"
    )
    p.add_argument("--out", required=True, help="Recording output directory")
    p.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="FIELD",
        help=(
            "Mark a typed field (by name or id) as a SECRET; its value is "
            "never persisted and is injected at replay from "
            "OPENADAPT_FLOW_SECRET_<FIELD>. input[type=password] is always "
            "treated as secret. Repeatable."
        ),
    )
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="FIELD",
        help=(
            "Record a typed field (by name or id) as a PARAMETER; its "
            "demonstrated value becomes the default, overridable at replay "
            "with --param. Repeatable."
        ),
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser headless (scripted/CI recording)",
    )
    p.set_defaults(func=_cmd_record)

    p = sub.add_parser(
        "demo-record",
        help="Serve MockMed and record the canonical triage demo",
    )
    p.add_argument("--out", required=True, help="Recording output directory")
    p.add_argument(
        "--note-text",
        default="Follow-up in 2 weeks; BP recheck.",
        help="Note text typed during the demo (recorded as a parameter)",
    )
    p.add_argument(
        "--param-name", default="note", help="Parameter name for the note"
    )
    p.add_argument(
        "--drift", default=None, help="Comma-separated MockMed drift modes"
    )
    p.add_argument(
        "--headed", action="store_true", help="Run the browser headed"
    )
    p.set_defaults(func=_cmd_demo_record)

    p = sub.add_parser(
        "compile", help="Compile a recording into a workflow bundle"
    )
    p.add_argument("recording", help="Recording directory")
    p.add_argument("--out", required=True, help="Output bundle directory")
    p.add_argument("--name", required=True, help="Workflow name")
    p.set_defaults(func=_cmd_compile)

    p = sub.add_parser(
        "replay",
        help=(
            "Replay a bundle (serves the bundled MockMed demo app when "
            "no --url is given)"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--url",
        default=None,
        help=(
            "URL of the target app (default: serve the bundled MockMed "
            "demo app)"
        ),
    )
    p.add_argument(
        "--drift",
        default=None,
        help=(
            "Comma-separated MockMed drift modes (theme,move,rename,modal) "
            "to demonstrate self-healing; only valid without --url"
        ),
    )
    p.add_argument(
        "--run-dir",
        default=None,
        help=(
            "Run output directory "
            "(default: runs/replay-<UTC timestamp> under the current "
            "directory)"
        ),
    )
    p.add_argument(
        "--param",
        action="append",
        metavar="K=V",
        help="Parameter substitution (repeatable)",
    )
    p.add_argument(
        "--save-healed-to",
        default=None,
        help="Write the healed bundle to this directory",
    )
    p.add_argument(
        "--headed", action="store_true", help="Run the browser headed"
    )
    p.set_defaults(func=_cmd_replay)

    p = sub.add_parser(
        "bench", help="Replay a bundle N times against MockMed and aggregate"
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument("--n", type=int, default=3, help="Number of iterations")
    p.add_argument(
        "--drift",
        default=None,
        help="Comma-separated drift modes forwarded to the MockMed URL",
    )
    p.add_argument(
        "--run-root", required=True, help="Directory for per-iteration runs"
    )
    p.add_argument(
        "--param",
        action="append",
        metavar="K=V",
        help="Parameter substitution (repeatable)",
    )
    p.add_argument(
        "--headed", action="store_true", help="Run the browser headed"
    )
    p.set_defaults(func=_cmd_bench)

    p = sub.add_parser(
        "benchmark",
        help=(
            "Benchmark compiled replay vs. a Claude computer-use agent on "
            "the MockMed triage task (agent arm needs an Anthropic API key "
            "and costs real money)"
        ),
    )
    p.add_argument(
        "--n-compiled",
        type=int,
        default=100,
        help="Compiled-replay iterations",
    )
    p.add_argument(
        "--n-agent", type=int, default=20, help="Agent iterations"
    )
    p.add_argument(
        "--out",
        default="benchmark/",
        help="Output directory for results.json / BENCHMARK.md / chart",
    )
    p.add_argument(
        "--note-text",
        default="Follow-up in 2 weeks; BP recheck.",
        help="Note text both arms enter",
    )
    p.add_argument(
        "--headed", action="store_true", help="Run the browsers headed"
    )
    p.set_defaults(func=_cmd_benchmark)

    p = sub.add_parser(
        "lint",
        help=(
            "Report a bundle's coverage gaps (unarmed clicks, vacuous "
            "postconditions, under-classified risk); exits nonzero by severity"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero on warnings too (default: only on errors)",
    )
    p.set_defaults(func=_cmd_lint)

    p = sub.add_parser(
        "certify",
        help=(
            "Enforce a policy on a bundle (exits nonzero + reports if it "
            "fails); makes 'runnable' distinct from 'certified safe'"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--policy",
        required=True,
        help=(
            "Policy YAML path, or a built-in name (permissive, clinical-write)"
        ),
    )
    p.set_defaults(func=_cmd_certify)

    p = sub.add_parser(
        "emit-skill", help="Emit an Agent Skills folder for a bundle"
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--out", required=True, help="Parent directory for the skill folder"
    )
    p.set_defaults(func=_cmd_emit_skill)

    p = sub.add_parser(
        "emit-mcp", help="Emit a standalone MCP server.py for a bundle"
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--out", required=True, help="Path for the generated server.py"
    )
    p.set_defaults(func=_cmd_emit_mcp)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

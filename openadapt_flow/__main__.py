"""openadapt-flow CLI.

Subcommands (thin wrappers over the module APIs; sibling modules are
imported lazily inside each handler so ``--help`` always works):

- ``record`` — open a headed browser on your OWN app (``--url``) and
  record what you do into the format ``compile`` consumes.
- ``demo-record`` — serve MockMed locally and record the canonical demo.
- ``compile`` — compile a recording directory into a workflow bundle.
- ``induce`` — induce a parameterized PROGRAM bundle from MULTIPLE recordings
  (multi-trace induction); refuses (nonzero exit) when intent is
  underdetermined rather than guessing a branch.
- ``replay`` — replay a bundle; serves the bundled MockMed demo app when no
  ``--url`` is given (with optional ``--drift`` to demonstrate healing).
  ``--worklist`` drives a program's loop over a CLI-supplied relation; effect
  verification and API actuation are wired from ``--config`` / flags.
- ``run`` — execute a bundle under a deployment config (``--config``): the
  same replay path, wired for a real deployment (backend / effects / actuation
  / durable runtime / policy) instead of the demo.
- ``resume`` — resume a durably-paused run from its last verified checkpoint.
- ``teach`` runs self-serve HALT -> LEARN: resolve a halted run from a fix
  demonstration (induce + gate + validate the correction, promote only a
  verified revision), writing an updated bundle. Refuses bad fixes.
- ``approve`` — mark a durably-paused run's pending escalation approved.
- ``bench`` — replay a bundle N times against MockMed and aggregate.
- ``lint`` — report a bundle's coverage gaps (advice; exit code by severity).
- ``certify`` — enforce a safety policy on a bundle (refuse it if it fails).
- ``emit-skill`` — emit an Agent Skills folder for a bundle.
- ``emit-mcp`` — emit a standalone MCP ``server.py`` for a bundle.

A single ``deployment.yaml`` (``--config``; see
``docs/deployment.example.yaml`` and :mod:`openadapt_flow.deployment`)
configures backend / actuation / effects / runtime / policy for ``record`` /
``compile`` / ``certify`` / ``replay`` / ``run`` / ``resume``.
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


def _load_worklist_file(path: Path) -> list[dict[str, str]]:
    """Load a CLI worklist file (``.csv`` or ``.json``) into param rows.

    CSV: the header row names the parameters; each subsequent row is one loop
    iteration's bindings. JSON: either a list of ``{param: value}`` row objects,
    or a single ``{param: value}`` object (one row). Every value is coerced to a
    string (the IR's worklist rows are ``dict[str, str]``).
    """
    import csv
    import json

    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"--worklist file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise SystemExit(
                f"--worklist JSON {path} must be a list of row objects (or one "
                "row object)"
            )
        return [{str(k): str(v) for k, v in row.items()} for row in data]
    if suffix == ".csv":
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            return [
                {str(k): str(v) for k, v in row.items() if k is not None}
                for row in reader
            ]
    raise SystemExit(f"--worklist file {path} must be .csv or .json (got {suffix!r})")


def _resolve_worklists(
    specs: Sequence[str] | None, workflow
) -> dict[str, list[dict[str, str]]]:
    """Turn ``--worklist`` specs into run-time worklists keyed by relation name.

    Each spec is ``RELATION=path`` (bind the file to that loop relation) or a
    bare ``path`` (bind to the workflow's SOLE loop relation; an error if the
    program has zero or several). Program-mode only — a linear bundle ignores
    worklists, so passing one there is refused loudly.
    """
    if not specs:
        return {}
    if workflow.program is None:
        raise SystemExit(
            "--worklist applies only to a PROGRAM bundle (with a loop over a "
            "relation); this bundle is linear."
        )
    relations = sorted(workflow.data_sources.keys())
    worklists: dict[str, list[dict[str, str]]] = {}
    for spec in specs:
        if "=" in spec:
            name, _, raw = spec.partition("=")
            name = name.strip()
        else:
            if len(relations) != 1:
                raise SystemExit(
                    "bare --worklist <file> needs exactly one loop relation to "
                    f"bind to; this program declares {relations or 'none'}. Use "
                    "--worklist RELATION=<file>."
                )
            name, raw = relations[0], spec
        if relations and name not in relations:
            raise SystemExit(
                f"--worklist relation {name!r} is not one of this program's "
                f"relations {relations}"
            )
        worklists[name] = _load_worklist_file(Path(raw))
    return worklists


def _deployment_runtime(args: argparse.Namespace):
    """Resolve the deployment wiring for a replay/run from ``--config`` + flags.

    Returns ``(cfg, effect_verifier, api_actuator, durable, allow_egress)``.
    A ``--config`` deployment YAML supplies the full surface (records paths,
    FHIR search params, ...); direct flags override the common fields. With
    neither, everything is default: no verifier, no actuator, non-durable, and
    egress only if ``--allow-model-grounding`` was passed (fully back-compatible).
    """
    from openadapt_flow.deployment import (
        DeploymentConfig,
        build_api_actuator,
        build_effect_verifier,
        load_deployment,
    )

    cfg = (
        load_deployment(args.config)
        if getattr(args, "config", None)
        else DeploymentConfig()
    )

    effects = cfg.effects
    if getattr(args, "effects_kind", None):
        effects = effects.model_copy(update={"kind": args.effects_kind})
    if getattr(args, "effects_base_url", None):
        effects = effects.model_copy(update={"base_url": args.effects_base_url})
    if getattr(args, "effects_root", None):
        effects = effects.model_copy(update={"root": args.effects_root})

    actuation = cfg.actuation
    if getattr(args, "api_base_url", None):
        actuation = actuation.model_copy(
            update={"api": True, "base_url": args.api_base_url}
        )
    elif getattr(args, "api_actuator", False):
        actuation = actuation.model_copy(update={"api": True})

    try:
        effect_verifier = build_effect_verifier(effects)
        api_actuator = build_api_actuator(actuation)
    except ValueError as e:
        raise SystemExit(str(e))

    durable = bool(cfg.runtime.durable or getattr(args, "durable", False))
    allow_egress = bool(
        cfg.runtime.allow_model_grounding
        or getattr(args, "allow_model_grounding", False)
    )
    return cfg, effect_verifier, api_actuator, durable, allow_egress


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
            record_video_dir=args.record_video,
        )
        print(f"Recording written to {out}")
    finally:
        stop()
    return 0


def _cmd_compile(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler import compile_recording

    workflow = compile_recording(Path(args.recording), Path(args.out), name=args.name)
    print(
        f"Compiled {len(workflow.steps)} steps into {args.out} "
        f"(workflow: {workflow.name!r})"
    )
    return 0


def _cmd_induce(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler.induction import induce_program, validate_held_out
    from openadapt_flow.ir import Workflow

    # Accept both RECORDING directories (compiled via the single-trace
    # bootstrap by induce_program) and already-compiled BUNDLE directories
    # (a dir containing workflow.json -> loaded as a Workflow). Detecting the
    # bundle case CLI-side keeps induce usable on artifacts the operator
    # already has, without touching the library API.
    traces: list = []
    for d in args.recording:
        path = Path(d)
        if (path / "workflow.json").is_file():
            traces.append(Workflow.load(path))
        else:
            traces.append(path)

    result = induce_program(traces)
    print(result.render())

    if args.held_out and len(traces) >= 2:
        print(validate_held_out(traces).render())

    if not result.certified or result.workflow is None:
        # Refuse rather than guess: surface the uncertainties honestly and exit
        # nonzero so a CI / deploy gate refuses the underdetermined program.
        print(
            "\nNOT CERTIFIED — no program bundle written. Resolve the point(s) "
            "above (e.g. via `disambiguate`) or supply more/consistent traces."
        )
        return 2

    workflow = result.workflow
    if args.name:
        workflow = workflow.model_copy(update={"name": args.name})
    out = Path(args.out)
    workflow.save(out)
    print(
        f"\nCERTIFIED — induced program bundle written to {out} "
        f"(workflow: {workflow.name!r}, "
        f"{len(result.param_specs)} param(s), "
        f"{len(result.column_decisions)} column decision(s))."
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

    # Deployment wiring (from --config and/or direct flags): a system-of-record
    # EffectVerifier, an ApiActuator, durable-runtime, and the egress opt-in.
    # All default to off, so an unconfigured replay behaves exactly as before.
    (
        cfg,
        effect_verifier,
        api_actuator,
        durable,
        allow_egress,
    ) = _deployment_runtime(args)
    worklists = _resolve_worklists(getattr(args, "worklist", None), workflow)

    headed = args.headed or cfg.backend.headed
    url = args.url or cfg.backend.url
    if url and args.drift:
        raise SystemExit(
            "--drift only applies to the bundled MockMed demo app; "
            "omit --url to use it (drift your own app for real)."
        )

    stop = None
    if url is None:
        from openadapt_flow.mockmed.server import serve

        url, stop = serve(port=0)
        url = _with_drift(url, args.drift)
        drift_note = f" (drift: {args.drift})" if args.drift else ""
        print(f"No --url given; replaying against bundled MockMed{drift_note}")

    video_dir = getattr(args, "record_video", None)
    from openadapt_flow._browser_setup import ensure_chromium_installed

    ensure_chromium_installed()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            # OPT-IN session video (default off): a recorded replay lives in a
            # context so Playwright can attach the recorder; None keeps the old
            # direct-page path with zero effect.
            context = None
            if video_dir is not None:
                context = browser.new_context(
                    viewport=_VIEWPORT,
                    record_video_dir=video_dir,
                    record_video_size=_VIEWPORT,
                )
                page = context.new_page()
            else:
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
                #
                # EGRESS GUARD (PHI audit REM-3): the appliance grounder /
                # identity-VLM / state-verifier send screenshots OFF the box, so
                # they are wired ONLY when the operator explicitly passes
                # --allow-model-grounding. Without it the replay is fully local
                # (OCR-anchoring grounder only) and makes zero outbound calls.
                # ``allow_egress`` was resolved above from --config/flags.
                appliance = appliance_from_env()
                if appliance is not None and not allow_egress:
                    print(
                        "On-prem VLM appliance is configured "
                        f"({os.environ.get('OPENADAPT_FLOW_VLM_URL')}) but NOT "
                        "wired: pass --allow-model-grounding to send screenshots "
                        "to it. Replaying FULLY LOCAL (zero outbound calls)."
                    )
                    appliance = None
                if appliance is not None:
                    print(
                        "Using on-prem VLM appliance at "
                        f"{os.environ.get('OPENADAPT_FLOW_VLM_URL')} "
                        "(identity veto tier + remote-VLM grounder fallback; "
                        "fail-safe to halt). WARNING: screenshots WILL leave "
                        "the box for this run (--allow-model-grounding)."
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
                    state_verifier=(appliance.state_verifier if appliance else None),
                    allow_model_grounding=allow_egress,
                    # Deployment wiring resolved from --config / flags: verify
                    # consequential writes against the system of record, actuate
                    # bound steps via the API tier, and checkpoint for resume.
                    effect_verifier=effect_verifier,
                    api_actuator=api_actuator,
                    durable=durable,
                    # Normal replay prefers the deterministic structural rung.
                    # ``--drift`` exists to DEMONSTRATE the visual healing ladder
                    # on the bundled MockMed app, so it forces the visual floor
                    # (structure would resolve the injected drift and there would
                    # be nothing to heal -- the very thing the flag shows).
                    use_structural=not bool(args.drift),
                ).run(
                    workflow,
                    params=params,
                    worklists=worklists,
                    bundle_dir=bundle,
                    run_dir=run_dir,
                    save_healed_to=(
                        Path(args.save_healed_to) if args.save_healed_to else None
                    ),
                )
            finally:
                video_path = None
                if context is not None:
                    try:
                        video_path = page.video.path() if page.video else None
                    except Exception:
                        video_path = None
                    context.close()  # flush the recorded video to disk
                browser.close()
                if video_path is not None:
                    print(f"Session video written to {video_path}")
    finally:
        if stop is not None:
            stop()

    report_md = render_run_report(run_dir)
    outcome = "success" if report.success else "FAILED"
    print(f"Replay {outcome}: {report_md}")
    if report.screenshots_may_leave_box:
        print(
            "NOTE: a model-grounding component was wired for this run — "
            "screenshots could have left the box (see REPORT.md)."
        )
    return 0 if report.success else 1


def _cmd_run(args: argparse.Namespace) -> int:
    """Execute a bundle under a deployment config.

    The SAME code path as ``replay`` (backend + effect verification + API
    actuation + durable runtime, all wired from ``--config``), framed for a
    real deployment rather than the demo: ``--drift`` (a MockMed-only teaching
    aid) is not offered here. ``--config`` supplies the backend URL, the system
    of record, the actuation tier, durability, and the policy.
    """
    # A deployment run is not the drift-demo; force it off and delegate to the
    # shared replay executor (which reads all deployment wiring from --config).
    args.drift = None
    return _cmd_replay(args)


def _cmd_resume(args: argparse.Namespace) -> int:
    from openadapt_flow import crypto
    from openadapt_flow.runtime.durable import resume, resume_point
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

    run_dir = Path(args.run_dir)
    # Encrypted runs (OPENADAPT_BUNDLE_KEY set) need the key to read the pause;
    # unset => None => plaintext, unchanged.
    ckpt_key = crypto.resolve_key(None)
    store = CheckpointStore(run_dir, key=ckpt_key)
    pending = store.read_pending()
    if pending is None:
        print(
            f"No pending escalation at {run_dir} — nothing to resume "
            "(a run only durably pauses when executed with a durable "
            "deployment; see --config runtime.durable / --durable)."
        )
        return 1
    if args.require_approval and pending.status != "approved":
        print(
            f"Pending escalation at {run_dir} is {pending.status!r}, not "
            "'approved'. Re-run without --require-approval to resume anyway, "
            f"or approve it first:\n    openadapt-flow approve {run_dir}"
        )
        return 3

    where = (
        f"state '{pending.step_id}'"
        if pending.program
        else f"step {pending.step_index} '{pending.step_id}' "
        f"(from index {resume_point(run_dir, key=ckpt_key)})"
    )
    print(
        f"Resuming {run_dir} at {where}: {pending.category}. "
        "Already-verified work is NOT re-run."
    )

    # A GUI automation cannot be resumed without a LIVE backend/vision, so build
    # a fresh Replayer here (deployment wiring from --config) and hand it to the
    # durable resume entrypoint, which re-binds params from the run manifest.
    from playwright.sync_api import sync_playwright

    from openadapt_flow._browser_setup import ensure_chromium_installed
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.report import render_run_report
    from openadapt_flow.runtime import Replayer

    (
        cfg,
        effect_verifier,
        api_actuator,
        _durable,
        allow_egress,
    ) = _deployment_runtime(args)
    url = args.url or cfg.backend.url
    if url is None:
        raise SystemExit(
            "resume needs the target app URL to rebuild a live backend — pass "
            "--url or set backend.url in --config."
        )
    headed = args.headed or cfg.backend.headed

    from openadapt_flow.runtime.durable.approval import ResumeRefused

    ensure_chromium_installed()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(viewport=_VIEWPORT)
        page.goto(url)
        try:
            replayer = Replayer(
                PlaywrightBackend(page),
                effect_verifier=effect_verifier,
                api_actuator=api_actuator,
                durable=True,  # resume forces durability so it can pause again
                checkpoint_key=ckpt_key,
                allow_model_grounding=allow_egress,
            )
            report = resume(run_dir, replayer, key=ckpt_key)
        except ResumeRefused as refused:
            # P0-5: the library REFUSED the resume (no valid approval, an expired
            # pause, a changed bundle, or a diverged app state) — never a silent
            # proceed. Approve first:  openadapt-flow approve <run_dir>
            print(f"Resume REFUSED: {refused}")
            return 3
        finally:
            browser.close()

    report_md = render_run_report(run_dir)
    outcome = "success" if report.success else "FAILED"
    print(f"Resume {outcome}: {report_md}")
    return 0 if report.success else 1


def _cmd_approve(args: argparse.Namespace) -> int:
    """Record an AUTHENTICATED approval for a durably-paused run (P0-5).

    Writes an :class:`ApprovalRecord` (approver identity / timestamp / chosen
    resolution / bundle-version hash) to ``run_dir/approval.json`` — the artifact
    the durable ``resume`` entrypoint now ENFORCES (a resume with no valid
    approval is refused). Also flips the pending escalation's ``status`` to
    ``approved`` for the audit trail / back-compat.
    """
    import getpass

    from openadapt_flow import crypto
    from openadapt_flow.runtime.durable.approval import ApprovalRecord
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
    from openadapt_flow.runtime.durable.program_checkpoint import bundle_version

    run_dir = Path(args.run_dir)
    store = CheckpointStore(run_dir, key=crypto.resolve_key(None))
    pending = store.read_pending()
    if pending is None:
        print(f"No pending escalation at {run_dir} — nothing to approve.")
        return 1
    if store.read_approval() is not None:
        print(f"Pending escalation at {run_dir} is already approved.")
        return 0

    # The approver identity defaults to the invoking OS user (a resume with a
    # blank approver is refused by the durable library); --approver overrides.
    approver = args.approver or getpass.getuser()
    manifest = store.read_manifest()
    bundle_ver = ""
    if manifest is not None:
        try:
            bundle_ver = bundle_version(manifest.bundle_dir)
        except OSError:
            bundle_ver = ""
    resolution = args.resolution or (
        pending.proposed_options[0] if pending.proposed_options else "approved"
    )
    store.write_approval(
        ApprovalRecord(
            approver=approver,
            resolution=resolution,
            bundle_version=bundle_ver,
            workflow_name=pending.workflow_name,
            run_dir=str(run_dir),
        )
    )
    # Keep the pending status in sync for the audit trail.
    store.write_pending(pending.model_copy(update={"status": "approved"}))
    print(
        f"Approved pending escalation at {run_dir} by {approver!r} "
        f"(step {pending.step_index} '{pending.step_id}': {pending.category}).\n"
        f"Resume it with:  openadapt-flow resume {run_dir}"
    )
    return 0


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

        from openadapt_flow._browser_setup import ensure_chromium_installed
        from openadapt_flow.backends.playwright_backend import (
            PlaywrightBackend,
        )

        ensure_chromium_installed()
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

    report_md = render_bench_report(run_root / "bench.json", run_root / "BENCH.md")
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
    # Policy source: explicit --policy, else the deployment config's policy
    # section (so one deployment.yaml certifies AND runs the bundle).
    policy_source = args.policy
    if policy_source is None and getattr(args, "config", None):
        from openadapt_flow.deployment import load_deployment

        policy_source = load_deployment(args.config).policy.policy
    if policy_source is None:
        raise SystemExit(
            "certify needs a policy: pass --policy <name-or-path> or set "
            "policy.policy in --config."
        )
    try:
        policy = load_policy(policy_source)
    except (FileNotFoundError, ValueError) as e:
        raise SystemExit(str(e))
    report = evaluate_policy(workflow, policy)
    print(report.render())
    # A failing certification exits nonzero so CI / deploy gates refuse the
    # bundle — the whole point of making "runnable" distinct from "certified".
    return 0 if report.passed else 2


def _cmd_disambiguate(args: argparse.Namespace) -> int:
    import json

    from openadapt_flow.compiler.disambiguation import (
        apply_answers,
        detect_ambiguities,
    )
    from openadapt_flow.ir import Workflow

    bundle = Path(args.bundle)
    workflow = Workflow.load(bundle)
    questions = detect_ambiguities(workflow)

    if not questions:
        print("No ambiguities detected; the demo is fully specified.")
        return 0

    answers: dict[str, str] = {}
    if args.answers:
        answers = json.loads(Path(args.answers).read_text())
    elif args.interactive:
        # Thin interactive wrapper -- prompts a human, then calls the same API
        # the tests drive directly. The core stays non-interactive.
        for q in questions:
            print(f"\n{q.prompt}")
            for opt in q.options:
                print(f"  ({opt.key}) {opt.label}")
            tag = "" if q.consequential else f" [default: {q.default_key}]"
            reply = input(f"Answer for {q.id}{tag}: ").strip()
            if reply:
                answers[q.id] = reply
    else:
        # Non-interactive listing: surface the questions and exit nonzero if
        # any is a consequential (must-answer) ambiguity.
        for q in questions:
            flag = " (CONSEQUENTIAL)" if q.consequential else ""
            print(f"\n[{q.kind.value}] {q.id}{flag}\n  {q.prompt}")
            for opt in q.options:
                print(f"  ({opt.key}) {opt.label}")
        consequential = any(q.consequential for q in questions)
        print(
            f"\n{len(questions)} question(s) detected. Re-run with "
            "--interactive or --answers to resolve."
        )
        return 2 if consequential else 0

    result = apply_answers(workflow, answers)
    print(result.render())
    if args.write:
        result.workflow.save(bundle)
        print(f"Resolved workflow written to {bundle}")
    return 0 if result.certified else 2


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


def _cmd_teach(args: argparse.Namespace) -> int:
    """Self-serve HALT -> LEARN -> RESOLVE for a halted run + a fix demo.

    Drives the governed halt->learn loop (induce the operator resolution as a
    guarded exception branch, gate it, validate it on held-out coverage) and
    writes an UPDATED bundle ONLY when it promotes. On a governed refusal
    (underdetermined or unsafe correction) nothing is written, the base bundle
    stays halting, and this exits nonzero.
    """
    from openadapt_flow.learning.teach import TeachError, teach

    try:
        result = teach(
            Path(args.run_dir),
            Path(args.fix),
            Path(args.out),
            bundle=Path(args.bundle),
            skill_id=args.skill_id,
            library_dir=Path(args.library) if args.library else None,
        )
    except TeachError as e:
        print(f"teach cannot run: {e}")
        return 2

    print(result.summary())
    if result.promoted:
        print(
            "\nLEARNED. Re-run the updated bundle and the workflow no longer "
            f"halts on this situation:\n    openadapt-flow replay {args.out}"
        )
        return 0
    print(
        "\nREFUSED (governed): the correction was underdetermined or would "
        "weaken a safety invariant, so nothing was promoted and the base bundle "
        "is unchanged (it still halts here). Supply a clearer or safer fix."
    )
    return 1


def _add_deployment_flags(
    p: argparse.ArgumentParser, *, worklist: bool = False
) -> None:
    """Add the shared deployment-wiring flags (config + effects + actuation +
    durable, optionally a worklist) to a replay-family subparser."""
    p.add_argument(
        "--config",
        default=None,
        metavar="YAML",
        help=(
            "Deployment config YAML wiring backend / actuation / effects / "
            "runtime / policy (see docs/deployment.example.yaml). Direct flags "
            "below override individual fields."
        ),
    )
    p.add_argument(
        "--effects-kind",
        choices=["none", "rest", "fhir", "document-hash"],
        default=None,
        help=(
            "System-of-record EffectVerifier to wire so consequential writes "
            "are verified against the real record (not the screen)"
        ),
    )
    p.add_argument(
        "--effects-base-url",
        default=None,
        help="Base URL for the rest / fhir effect verifier",
    )
    p.add_argument(
        "--effects-root",
        default=None,
        help="Document-store root for the document-hash effect verifier",
    )
    p.add_argument(
        "--api-actuator",
        action="store_true",
        help=(
            "Wire the API/tool actuation tier: a step carrying an ApiBinding is "
            "performed via the API (deterministic, $0) and confirmed by the "
            "effect verifier, skipping the GUI"
        ),
    )
    p.add_argument(
        "--api-base-url",
        default=None,
        help="Base URL for the API actuator (implies --api-actuator)",
    )
    p.add_argument(
        "--durable",
        action="store_true",
        help=(
            "Enable the Tier-3 durable runtime: checkpoint each verified step "
            "and durably PAUSE on halt, so the run is resumable via `resume` "
            "(never re-performing a confirmed write)"
        ),
    )
    if worklist:
        p.add_argument(
            "--worklist",
            action="append",
            metavar="[RELATION=]FILE",
            help=(
                "CSV/JSON worklist of parameter rows driving a PROGRAM bundle's "
                "loop over a relation (repeatable). 'RELATION=FILE' binds the "
                "file to that relation; a bare 'FILE' binds to the program's "
                "sole loop relation."
            ),
        )


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
    p.add_argument("--url", required=True, help="URL of the app to record against")
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
    p.add_argument("--param-name", default="note", help="Parameter name for the note")
    p.add_argument("--drift", default=None, help="Comma-separated MockMed drift modes")
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    p.add_argument(
        "--record-video",
        default=None,
        metavar="DIR",
        help=(
            "OPT-IN: capture a WebM video of the recording session into DIR "
            "(default: off; no effect on the recording written to --out)"
        ),
    )
    p.set_defaults(func=_cmd_demo_record)

    p = sub.add_parser("compile", help="Compile a recording into a workflow bundle")
    p.add_argument("recording", help="Recording directory")
    p.add_argument("--out", required=True, help="Output bundle directory")
    p.add_argument("--name", required=True, help="Workflow name")
    p.set_defaults(func=_cmd_compile)

    p = sub.add_parser(
        "induce",
        help=(
            "Induce a parameterized PROGRAM bundle from MULTIPLE recordings "
            "(multi-trace induction: infer params / loops / branches). REFUSES "
            "(nonzero exit, no bundle) when intent is underdetermined"
        ),
    )
    p.add_argument(
        "recording",
        nargs="+",
        help=(
            "Two or more recording directories (or already-compiled bundle "
            "directories) of the SAME task"
        ),
    )
    p.add_argument(
        "--out",
        required=True,
        help="Output program-bundle directory (written only when CERTIFIED)",
    )
    p.add_argument(
        "--name",
        default=None,
        help="Name for the induced workflow (default: 'induced-program')",
    )
    p.add_argument(
        "--held-out",
        action="store_true",
        help=(
            "Also run leave-one-out held-out validation and print the per-fold "
            "reproduction scores (needs >= 2 traces)"
        ),
    )
    p.set_defaults(func=_cmd_induce)

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
        help=("URL of the target app (default: serve the bundled MockMed demo app)"),
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
        "--allow-model-grounding",
        action="store_true",
        help=(
            "EGRESS OPT-IN (PHI audit REM-3): permit wiring an off-box model "
            "grounder / identity-VLM / state-verifier (a paid API or an on-prem "
            "VLM appliance via OPENADAPT_FLOW_VLM_URL). Screenshots may leave "
            "the box. Off by default: replay is fully local with zero outbound "
            "calls."
        ),
    )
    p.add_argument(
        "--save-healed-to",
        default=None,
        help="Write the healed bundle to this directory",
    )
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    p.add_argument(
        "--record-video",
        default=None,
        metavar="DIR",
        help=(
            "OPT-IN: capture a WebM video of the replay session into DIR "
            "(default: off; no effect on the run directory or report)"
        ),
    )
    _add_deployment_flags(p, worklist=True)
    p.set_defaults(func=_cmd_replay)

    p = sub.add_parser(
        "run",
        help=(
            "Execute a bundle under a deployment config (--config): the replay "
            "path wired for a real deployment (backend / effects / actuation / "
            "durable / policy) instead of the demo"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--url",
        default=None,
        help="Target app URL (default: backend.url from --config)",
    )
    p.add_argument(
        "--run-dir",
        default=None,
        help="Run output directory (default: runs/replay-<UTC timestamp>)",
    )
    p.add_argument(
        "--param",
        action="append",
        metavar="K=V",
        help="Parameter substitution (repeatable)",
    )
    p.add_argument(
        "--allow-model-grounding",
        action="store_true",
        help=(
            "EGRESS OPT-IN (PHI audit REM-3): permit wiring an off-box model "
            "component (also settable via runtime.allow_model_grounding)"
        ),
    )
    p.add_argument(
        "--save-healed-to",
        default=None,
        help="Write the healed bundle to this directory",
    )
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    p.add_argument(
        "--record-video", default=None, metavar="DIR", help=argparse.SUPPRESS
    )
    _add_deployment_flags(p, worklist=True)
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser(
        "resume",
        help=(
            "Resume a durably-paused run from its last verified checkpoint "
            "(never re-running an already-confirmed write)"
        ),
    )
    p.add_argument("run_dir", help="The paused run directory (holds checkpoints)")
    p.add_argument(
        "--url",
        default=None,
        help="Target app URL to rebuild a live backend (default: backend.url)",
    )
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
    p.add_argument(
        "--require-approval",
        action="store_true",
        help=(
            "Refuse to resume unless the pending escalation is 'approved' "
            "(see `approve`)"
        ),
    )
    _add_deployment_flags(p)
    p.set_defaults(func=_cmd_resume)

    p = sub.add_parser(
        "approve",
        help=(
            "Record an authenticated approval (approver / resolution / bundle "
            "version) authorizing a durably-paused run to resume"
        ),
    )
    p.add_argument("run_dir", help="The paused run directory (holds the escalation)")
    p.add_argument(
        "--approver",
        default=None,
        help=(
            "Approver identity recorded on the approval (defaults to the "
            "invoking OS user; a blank identity is refused at resume)"
        ),
    )
    p.add_argument(
        "--resolution",
        default=None,
        help=(
            "The chosen resolution (defaults to the pause's first proposed "
            "option) — recorded for the audit trail"
        ),
    )
    p.set_defaults(func=_cmd_approve)

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
    p.add_argument("--run-root", required=True, help="Directory for per-iteration runs")
    p.add_argument(
        "--param",
        action="append",
        metavar="K=V",
        help="Parameter substitution (repeatable)",
    )
    p.add_argument("--headed", action="store_true", help="Run the browser headed")
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
    p.add_argument("--n-agent", type=int, default=20, help="Agent iterations")
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
    p.add_argument("--headed", action="store_true", help="Run the browsers headed")
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
        default=None,
        help=(
            "Policy YAML path, or a built-in name (permissive, clinical-write). "
            "Defaults to policy.policy from --config."
        ),
    )
    p.add_argument(
        "--config",
        default=None,
        metavar="YAML",
        help="Deployment config YAML to read the policy from when --policy is omitted",
    )
    p.set_defaults(func=_cmd_certify)

    p = sub.add_parser(
        "disambiguate",
        help=(
            "Surface compile-time multiple-choice questions for an ambiguous "
            "demo and apply the answers as guards/params (ask, don't guess)"
        ),
    )
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for each question on the terminal",
    )
    p.add_argument(
        "--answers",
        help="JSON file mapping question id -> chosen option key",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="Save the resolved workflow back into the bundle",
    )
    p.set_defaults(func=_cmd_disambiguate)

    p = sub.add_parser("emit-skill", help="Emit an Agent Skills folder for a bundle")
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument("--out", required=True, help="Parent directory for the skill folder")
    p.set_defaults(func=_cmd_emit_skill)

    p = sub.add_parser("emit-mcp", help="Emit a standalone MCP server.py for a bundle")
    p.add_argument("bundle", help="Workflow bundle directory")
    p.add_argument("--out", required=True, help="Path for the generated server.py")
    p.set_defaults(func=_cmd_emit_mcp)

    p = sub.add_parser(
        "teach",
        help=(
            "Self-serve HALT -> LEARN: resolve a halted run from a fix "
            "demonstration. Induces the correction as a guarded exception "
            "branch, gates + validates it, and writes an updated bundle ONLY "
            "if it passes (governed refusal otherwise; nonzero exit)"
        ),
    )
    p.add_argument(
        "run_dir",
        help="The HALTED run directory (holds report.json with a halt)",
    )
    p.add_argument(
        "--fix",
        required=True,
        help=(
            "The fix demonstration: a RECORDING directory of the resolution "
            "(record ONLY the corrective actions, e.g. dismiss the dialog), or "
            "a .json correction spec (scripted / CI: resolution_steps, optional "
            "tail_intents / facts / params)"
        ),
    )
    p.add_argument(
        "--bundle",
        required=True,
        help="The base bundle that halted (seeds the skill's active version)",
    )
    p.add_argument(
        "--out",
        required=True,
        help=(
            "Output directory for the UPDATED bundle (written only when the "
            "correction is promoted)"
        ),
    )
    p.add_argument(
        "--skill-id",
        default=None,
        help="Skill id in the library (default: the run's workflow name)",
    )
    p.add_argument(
        "--library",
        default=None,
        help=(
            "Directory for the versioned skill library that keeps the "
            "promotion lineage (default: <out>.skills)"
        ),
    )
    p.set_defaults(func=_cmd_teach)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

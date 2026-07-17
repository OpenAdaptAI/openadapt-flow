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
from typing import TYPE_CHECKING, Optional, Sequence
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.backend import Backend
    from openadapt_flow.ir import RunReport

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


def _replay_params(
    pairs: Sequence[str] | None,
    params_file: str | None = None,
) -> dict[str, str]:
    """Load replay bindings without requiring sensitive values in argv.

    ``--params-file`` is intended for managed runners: the file can be staged
    inside the per-run boundary while process listings contain only its path.
    Explicit ``--param`` flags remain supported and override file values.
    """
    import json

    params: dict[str, str] = {}
    if params_file:
        path = Path(params_file)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"--params-file could not be read as JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise SystemExit("--params-file must contain one JSON object")
        if len(raw) > 100:
            raise SystemExit("--params-file may contain at most 100 parameters")
        for key, value in raw.items():
            if not isinstance(key, str) or not key:
                raise SystemExit("--params-file keys must be non-empty strings")
            if not isinstance(value, (str, int, float, bool)) or isinstance(
                value, (dict, list)
            ):
                raise SystemExit(f"--params-file value for {key!r} must be a scalar")
            params[key] = str(value)
    params.update(_parse_params(pairs))
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


def _deployment_runtime(args: argparse.Namespace, params: dict[str, str] | None = None):
    """Resolve the deployment wiring for a replay/run from ``--config`` + flags.

    Returns ``(cfg, effect_verifier, api_actuator, durable, allow_egress)``.
    A ``--config`` deployment YAML supplies the full surface (records paths,
    FHIR search params, ...); direct flags override the common fields. With
    neither, everything is default: no verifier, no actuator, non-durable, and
    egress only if ``--allow-model-grounding`` was passed (fully back-compatible).

    ``params`` (the governed ``--params-file`` / ``--param`` values) binds an
    effect-verifier config's explicit ``{param: ...}`` references
    (``effects.path_params`` / ``search_param_exprs`` / ``sql_query_params``)
    at construction — see ``docs/EFFECT_KIT.md``. A config with no references
    ignores it.
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
        effect_verifier = build_effect_verifier(effects, params=params)
        api_actuator = build_api_actuator(actuation)
    except ValueError as e:
        raise SystemExit(str(e))

    durable = bool(cfg.runtime.durable or getattr(args, "durable", False))
    allow_egress = bool(
        cfg.runtime.allow_model_grounding
        or getattr(args, "allow_model_grounding", False)
    )
    return cfg, effect_verifier, api_actuator, durable, allow_egress


def _resolve_backend_config(args: argparse.Namespace, cfg):
    """Merge the ``--backend`` family of CLI flags over ``cfg.backend``.

    A deployment ``--config`` supplies the backend section; direct flags
    (``--backend`` / ``--agent-url`` / ``--macos-app`` / ``--rdp-host``) override individual
    fields, exactly as the effects/actuation flags override their sections. With
    no flags the config's backend (default ``web``) is returned unchanged, so an
    unflagged web replay behaves precisely as before.
    """
    backend = cfg.backend
    if getattr(args, "backend", None):
        backend = backend.model_copy(update={"kind": args.backend})
    if getattr(args, "agent_url", None):
        backend = backend.model_copy(update={"agent_url": args.agent_url})
    if getattr(args, "macos_app", None):
        backend = backend.model_copy(update={"macos_app": args.macos_app})
    if getattr(args, "macos_window_title", None):
        backend = backend.model_copy(
            update={"macos_window_title": args.macos_window_title}
        )
    if getattr(args, "rdp_host", None):
        backend = backend.model_copy(update={"rdp_host": args.rdp_host})
    return backend


def _build_and_run_replayer(
    backend,
    *,
    workflow,
    params: dict[str, str],
    worklists: dict[str, list[dict[str, str]]],
    bundle: Path,
    run_dir: Path,
    save_healed_to: Optional[Path],
    allow_egress: bool,
    effect_verifier,
    api_actuator,
    durable: bool,
    use_structural: bool,
    governed_authorization=None,
    execution_origin: Optional[str] = None,
    execution_entry_url: Optional[str] = None,
):
    """Wire the grounding / identity-VLM ladder and run the replayer.

    Backend-agnostic: the on-prem VLM appliance (opt-in, egress-guarded), the
    OCR grounding rung, and the deployment wiring (effect verifier / API
    actuator / durable runtime) are identical whether the backend is the
    browser, the Windows agent, or an RDP/remote-display session. Returns the
    run report. Extracted verbatim from the historical inline web path so the
    web behavior is unchanged and every backend shares one code path.
    """
    import os

    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.grounder import build_grounder
    from openadapt_flow.runtime.remote_vlm import appliance_from_env

    # An on-prem VLM appliance is opt-in (OPENADAPT_FLOW_VLM_URL). Unset -> the
    # identity veto tier stays off. Configured -> the identity veto tier and the
    # remote-VLM grounder come online, both fail-safe (an appliance outage halts,
    # never mis-clicks).
    #
    # EGRESS GUARD (PHI audit REM-3): the appliance grounder / identity-VLM /
    # state-verifier send screenshots OFF the box, so they are wired ONLY when
    # the operator explicitly passes --allow-model-grounding. Without it the
    # replay is fully local and makes zero outbound calls.
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
    # Grounding rung: OCR text-anchoring (openadapt-grounding) is PRIMARY
    # whenever the 'grounding' extra is installed; the remote-VLM grounder (if
    # an appliance is configured) is the fallback for text-less surfaces. None
    # when neither is present (the model-free default).
    grounder = build_grounder(fallback=appliance.grounder if appliance else None)
    if grounder is not None:
        print(f"Grounding rung active: {type(grounder).__name__}")
    return Replayer(
        backend,
        grounder=grounder,
        identity_vlm=appliance.identity_vlm if appliance else None,
        state_verifier=(appliance.state_verifier if appliance else None),
        allow_model_grounding=allow_egress,
        # Deployment wiring resolved from --config / flags: verify consequential
        # writes against the system of record, actuate bound steps via the API
        # tier, and checkpoint for resume.
        effect_verifier=effect_verifier,
        api_actuator=api_actuator,
        durable=durable,
        use_structural=use_structural,
        governed_authorization=governed_authorization,
    ).run(
        workflow,
        params=params,
        worklists=worklists,
        bundle_dir=bundle,
        run_dir=run_dir,
        save_healed_to=save_healed_to,
        execution_origin=execution_origin,
        execution_entry_url=execution_entry_url,
    )


def _finish_replay(run_dir: Path, report, *, synthetic_demo: bool = False) -> int:
    """Render the run report, print the outcome, and map it to an exit code.

    ``synthetic_demo`` is True only for the bundled-MockMed demo replay with no
    operator ``--param`` overrides (see ``render_run_report``); it softens the
    first-run plaintext-PHI warning for known-synthetic demo data and nothing
    else.
    """
    from openadapt_flow.report import render_run_report

    report_md = render_run_report(run_dir, synthetic_demo=synthetic_demo)
    outcome = "success" if report.success else "FAILED"
    print(f"Replay {outcome}: {report_md}")
    if report.screenshots_may_leave_box:
        print(
            "NOTE: a model-grounding component was wired for this run — "
            "screenshots could have left the box (see REPORT.md)."
        )
    _maybe_report_break(run_dir, report)
    return 0 if report.success else 1


def _replay_desktop(
    args: argparse.Namespace,
    backend_cfg,
    *,
    workflow,
    params: dict[str, str],
    worklists: dict[str, list[dict[str, str]]],
    bundle: Path,
    run_dir: Path,
    allow_egress: bool,
    effect_verifier,
    api_actuator,
    durable: bool,
    governed_authorization=None,
) -> int:
    """Replay against a NON-browser backend (windows / rdp) built by the factory.

    No Playwright browser, no bundled MockMed, no session video — those are
    web-only. ``--drift`` (a MockMed teaching aid) is refused. The backend is
    built from the resolved ``BackendConfig`` and the shared replayer wiring runs
    exactly as it does for the web path.
    """
    from openadapt_flow.backends.factory import build_backend

    if args.drift:
        raise SystemExit(
            "--drift only demonstrates healing on the bundled MockMed web demo; "
            f"it does not apply to the {backend_cfg.kind!r} backend."
        )
    try:
        backend = build_backend(backend_cfg)
    except ValueError as e:
        raise SystemExit(str(e))

    try:
        report = _build_and_run_replayer(
            backend,
            workflow=workflow,
            params=params,
            worklists=worklists,
            bundle=bundle,
            run_dir=run_dir,
            save_healed_to=(Path(args.save_healed_to) if args.save_healed_to else None),
            allow_egress=allow_egress,
            effect_verifier=effect_verifier,
            api_actuator=api_actuator,
            durable=durable,
            # No MockMed drift here, so the deterministic structural rung is
            # preferred exactly as in a non-drift web replay.
            use_structural=True,
            governed_authorization=governed_authorization,
        )
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()  # RDP transports hold a live socket; browsers/agents don't
    return _finish_replay(run_dir, report)


def _cmd_record(args: argparse.Namespace) -> int:
    # The interactive (web) recorder installs in-page DOM listeners against a
    # headed Playwright page. The DESKTOP recorder (--backend windows) captures
    # the operator's native OS input over the win_agent contract; both emit the
    # SAME compile-ready recording format. Selection is fail-loud (a missing
    # target for the chosen backend raises rather than silently record web).
    backend = getattr(args, "backend", None) or "web"
    if backend in ("windows", "macos", "rdp"):
        return _cmd_record_desktop(args, backend)

    if not args.url:
        raise SystemExit(
            "record --backend web requires --url (the app to record against)."
        )

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


def _cmd_record_desktop(args: argparse.Namespace, backend: str) -> int:
    """Record a live desktop demonstration for a native/pixel desktop backend.

    Reuses the tested capture stack: an ``openadapt-capture`` session captures
    the operator's real demonstration, then
    :func:`openadapt_flow.adapters.capture.convert_capture` emits the same
    compile-ready recording format the browser recorder produces — closing
    ``record -> compile -> replay`` on the desktop substrate through the CLI.

    The recording is substrate-agnostic (pixel frames + coordinates); the
    ``--backend`` selects intent and REPLAY wiring. For ``rdp`` (a remote
    display painted in a client WINDOW), capture must happen in the SAME pixel
    space the rdp backend replays in — record inside the remote session (or
    full-screen the client) so coordinates align; a cross-machine coordinate
    remap is a documented follow-up (docs/desktop/RECORDING.md).
    """
    if args.secret:
        # Field-level secret redaction relies on DOM field geometry (the
        # browser recorder blacks out the field rect). A pixel/desktop capture
        # has no such geometry, so we refuse rather than persist an unredacted
        # secret frame — a silent PHI leak. Deferred (docs/desktop/RECORDING.md).
        raise SystemExit(
            f"record --backend {backend}: --secret is not yet supported on the "
            "pixel/desktop substrate (no field geometry to redact the typed "
            "value from the captured frames). Use a masked/password field, or "
            "see docs/desktop/RECORDING.md for the deferred design."
        )

    # On the desktop substrate there is no field identity, so a parameter is
    # keyed by its demonstrated VALUE: --param NAME=VALUE (mirrors
    # convert_capture / the replay --param contract).
    params = _parse_params(args.param)

    from openadapt_flow.desktop_record import record_desktop_capture

    task = args.task or f"openadapt-flow {backend} recording"
    out = record_desktop_capture(
        Path(args.out),
        task_description=task,
        params=params,
    )
    print(f"Recording written to {out}")
    if params:
        print(
            "Recorded parameter(s): "
            + ", ".join(f"{k}={v!r}" for k, v in params.items())
            + ". Override at replay with --param NAME=VALUE."
        )
    print(
        "Compile it:  openadapt-flow compile "
        f"{out} --out <bundle> --name <workflow>\n"
        f"Then replay: openadapt-flow replay <bundle> --backend {backend} …"
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

    from openadapt_flow.backends.factory import _normalize_kind, build_backend
    from openadapt_flow.ir import Workflow

    bundle = Path(args.bundle)
    run_dir = Path(args.run_dir) if args.run_dir else _default_run_dir()
    workflow = Workflow.load(bundle)
    params = _replay_params(args.param, getattr(args, "params_file", None))

    # Deployment wiring (from --config and/or direct flags): a system-of-record
    # EffectVerifier, an ApiActuator, durable-runtime, and the egress opt-in.
    # All default to off, so an unconfigured replay behaves exactly as before.
    (
        cfg,
        effect_verifier,
        api_actuator,
        durable,
        allow_egress,
    ) = _deployment_runtime(args, params=params)
    worklists = _resolve_worklists(getattr(args, "worklist", None), workflow)

    # Backend selection (--backend web|windows|rdp, overriding --config). A
    # non-web backend drives a native desktop / RDP / remote-display session with
    # no browser: delegate to the desktop path. Default web is unchanged below.
    backend_cfg = _resolve_backend_config(args, cfg)
    if _normalize_kind(backend_cfg.kind) != "web":
        return _replay_desktop(
            args,
            backend_cfg,
            workflow=workflow,
            params=params,
            worklists=worklists,
            bundle=bundle,
            run_dir=run_dir,
            allow_egress=allow_egress,
            effect_verifier=effect_verifier,
            api_actuator=api_actuator,
            durable=durable,
            governed_authorization=getattr(args, "_governed_run_authorization", None),
        )

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
                # The browser backend is built through the same factory as the
                # desktop backends; the grounding / identity / deployment wiring
                # and the run are shared (see _build_and_run_replayer). ``--drift``
                # (a MockMed teaching aid) forces the visual floor so the healing
                # ladder is exercised instead of the structural rung resolving it.
                report = _build_and_run_replayer(
                    build_backend(backend_cfg, page=page),
                    workflow=workflow,
                    params=params,
                    worklists=worklists,
                    bundle=bundle,
                    run_dir=run_dir,
                    save_healed_to=(
                        Path(args.save_healed_to) if args.save_healed_to else None
                    ),
                    allow_egress=allow_egress,
                    effect_verifier=effect_verifier,
                    api_actuator=api_actuator,
                    durable=durable,
                    use_structural=not bool(args.drift),
                    governed_authorization=getattr(
                        args, "_governed_run_authorization", None
                    ),
                    execution_origin=(
                        f"{urlsplit(page.url).scheme}://{urlsplit(page.url).netloc}"
                    ),
                    execution_entry_url=url,
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

    # Soften the first-run plaintext-PHI warning ONLY when the CLI itself
    # served the bundled synthetic MockMed demo (no --url) and no operator
    # values flowed in (--param / --params-file / worklists) — every
    # identity-like value is then the recorded fake demo data. Real targets
    # or operator-supplied values keep the full warning.
    return _finish_replay(
        run_dir,
        report,
        synthetic_demo=(stop is not None and not params and not worklists),
    )


def _cmd_run(args: argparse.Namespace) -> int:
    """Execute a bundle under a deployment config -- FAIL-CLOSED.

    Unlike ``replay`` (the permissive demo path, where certification / identity
    arming / effect verification / encryption are all OPTIONAL), ``run`` REFUSES
    to execute unless every fail-closed admission gate holds
    (:mod:`openadapt_flow.run_gate`): the bundle is certified, every
    entity-sensitive / consequential action is identity-armed, every write has a
    verifiable (or explicitly approved) effect contract, the bundle is encrypted
    at rest, and its integrity manifest re-verifies. On any refusal it prints the
    coverage report naming the failing gate and exits nonzero WITHOUT executing.
    ``--dry-run`` / ``--explain`` print the coverage report and stop before
    execution. Once admitted, it delegates to the shared executor (the same
    backend / effect / actuation / durable runtime as ``replay``), with
    ``--drift`` (a MockMed-only teaching aid) forced off.
    """
    from openadapt_flow.ir import Workflow
    from openadapt_flow.run_gate import (
        build_runtime_authorization,
        evaluate_run_gate,
    )

    bundle = Path(args.bundle)
    # Load the bundle first (decrypting if encrypted -- the key comes from
    # --config/env via OPENADAPT_BUNDLE_KEY); a missing/wrong key fails LOUDLY.
    try:
        workflow = Workflow.load(bundle)
    except Exception as e:  # crypto / integrity / structural errors -> fail closed
        print(f"run REFUSED: bundle could not be loaded safely: {e}")
        return 2

    gate_params = _replay_params(args.param, getattr(args, "params_file", None))
    cfg, effect_verifier, api_actuator, _durable, _egress = _deployment_runtime(
        args, params=gate_params
    )
    policy_source = args.policy or cfg.policy.policy

    report = evaluate_run_gate(
        workflow,
        bundle_dir=bundle,
        deployment=cfg,
        effect_verifier=effect_verifier,
        api_actuator=api_actuator,
        policy_source=policy_source,
        approval_available=bool(getattr(args, "approve_unverified_writes", False)),
        strict_templates=bool(getattr(args, "strict_templates", False)),
        require_encryption=not bool(getattr(args, "allow_unencrypted", False)),
        pinned_content_digest=getattr(args, "pin_digest", None),
        pinned_compiler_version=getattr(args, "pin_version", None),
    )
    print(report.render())

    if getattr(args, "dry_run", False) or getattr(args, "explain", False):
        # Report-only: never execute, regardless of the verdict.
        return 0 if report.passed else 2
    if not report.passed:
        # Fail closed: refuse execution and exit nonzero.
        return 2

    runtime_params = _replay_params(args.param, getattr(args, "params_file", None))
    runtime_worklists = _resolve_worklists(getattr(args, "worklist", None), workflow)
    args._governed_run_authorization = build_runtime_authorization(
        workflow,
        report,
        params=runtime_params,
        worklists=runtime_worklists,
    )

    # Admitted. A deployment run is not the drift-demo; force it off and delegate
    # to the shared replay executor (which reads all deployment wiring itself).
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
    from openadapt_flow.backends.factory import _normalize_kind, build_backend
    from openadapt_flow.report import render_run_report
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.durable.approval import ResumeRefused

    (
        cfg,
        effect_verifier,
        api_actuator,
        _durable,
        allow_egress,
    ) = _deployment_runtime(
        args,
        params=_replay_params(
            getattr(args, "param", None), getattr(args, "params_file", None)
        ),
    )

    # Route the resumed run through the SAME backend factory as replay/run
    # (--backend / --agent-url / --rdp-host over --config), so a resume drives
    # the bundle's real substrate rather than always the browser. The default
    # (web / no flag) reproduces the historical Playwright path below exactly.
    backend_cfg = _resolve_backend_config(args, cfg)

    def _resume_with(backend: "Backend") -> "RunReport":
        replayer = Replayer(
            backend,
            effect_verifier=effect_verifier,
            api_actuator=api_actuator,
            durable=True,  # resume forces durability so it can pause again
            checkpoint_key=ckpt_key,
            allow_model_grounding=allow_egress,
        )
        return resume(run_dir, replayer, key=ckpt_key)

    try:
        if _normalize_kind(backend_cfg.kind) == "web":
            from playwright.sync_api import sync_playwright

            from openadapt_flow._browser_setup import ensure_chromium_installed

            url = args.url or cfg.backend.url
            if url is None:
                raise SystemExit(
                    "resume needs the target app URL to rebuild a live backend — "
                    "pass --url or set backend.url in --config."
                )
            headed = args.headed or cfg.backend.headed
            ensure_chromium_installed()
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=not headed)
                page = browser.new_page(viewport=_VIEWPORT)
                page.goto(url)
                try:
                    report = _resume_with(build_backend(backend_cfg, page=page))
                finally:
                    browser.close()
        else:
            # Desktop (windows / rdp): no browser, no --url; the factory builds
            # the native backend from the resolved config (fail-loud on a missing
            # required field). RDP transports hold a live socket — close them.
            try:
                backend = build_backend(backend_cfg)
            except ValueError as e:
                raise SystemExit(str(e))
            try:
                report = _resume_with(backend)
            finally:
                close = getattr(backend, "close", None)
                if callable(close):
                    close()
    except ResumeRefused as refused:
        # P0-5: the library REFUSED the resume (no valid approval, an expired
        # pause, a changed bundle, or a diverged app state) — never a silent
        # proceed. Approve first:  openadapt-flow approve <run_dir>
        print(f"Resume REFUSED: {refused}")
        return 3

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


def _cmd_login(args: argparse.Namespace) -> int:
    """Validate an ingest token against the hosted control plane and store the
    host in config and the token in the OS keychain when saving is enabled.

    Token resolution: ``--token`` -> ``OPENADAPT_INGEST_TOKEN`` env ->
    OS keychain -> existing config migration token. Mint a
    token in the dashboard at ``<host>/dashboard/settings/ingest``.
    """
    from openadapt_flow.hosted import HostedError, login

    try:
        result = login(
            token=args.token,
            host=args.host,
            save=not args.no_save,
            allow_plaintext_token=args.allow_plaintext_token,
            destination_kind=args.destination_kind,
            trusted_hosts=args.trusted_host,
        )
    except HostedError as e:
        print(f"login failed: {e}")
        return 1
    print(f"Logged in to {result['host']} (token validated).")
    if result.get("config_path"):
        if result.get("token_storage") == "keyring":
            print(
                f"Token saved to {result['config_path']}; non-secret host saved in config."
            )
        else:
            print(
                f"Host + token saved to {result['config_path']} (mode 0600).\n"
                "WARNING: plaintext storage was explicitly enabled. Prefer the "
                "OS keychain or OPENADAPT_INGEST_TOKEN."
            )
    print(f"Manage tokens at {result['settings_url']}")
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    """Upload the exact approved sanitized archive to ``/api/ingest``.

    ``PATH`` defaults to the most-recent recording directory. Raw input creates
    a derivative and pauses for review; approved input sends the exact frozen
    archive and prints the server-assigned workflow id/dashboard URL.
    """
    from openadapt_flow.hosted import HostedError, push

    try:
        result = push(
            args.path,
            kind=args.kind,
            name=args.name,
            workflow_id=args.workflow_id,
            resolves_run_id=args.resolves_run_id,
            host=args.host,
            token=args.token,
            deployment_kind=args.deployment_kind,
            attest_non_phi=args.attest_non_phi,
            destination_kind=args.destination_kind,
            trusted_hosts=args.trusted_host,
            sanitized_out=args.sanitized_out,
            auto_approve=args.auto_approve,
            validation_attestation=args.validation_attestation,
        )
    except HostedError as e:
        print(f"push failed: {e}")
        return 1
    if result.get("pending_review"):
        print(f"Sanitized derivative created at {result['sanitized_path']}.")
        print(
            "Upload paused for local review; the original was not modified or uploaded."
        )
        print(result["review_command"])
        return 0
    workflow_id = result.get("workflow_id", "<unknown>")
    compile_status = (result.get("compile") or {}).get("status", "?")
    print(
        f"Pushed. workflow_id={workflow_id} "
        f"(name={result.get('workflow_name')!r}, kind={result.get('kind')}, "
        f"compile={compile_status})."
    )
    if result.get("dashboard_url"):
        print(f"Dashboard: {result['dashboard_url']}")
    return 0


def _cmd_validate_hosted(args: argparse.Namespace) -> int:
    """Create a challenge-bound operator runtime-validation attestation."""
    import json

    from openadapt_flow.runtime_validation import (
        RuntimeValidationError,
        create_runtime_validation_attestation,
        save_runtime_validation_attestation,
    )

    try:
        compiler_config = (
            json.loads(Path(args.compiler_config).read_text(encoding="utf-8"))
            if args.compiler_config
            else None
        )
        if compiler_config is not None and not isinstance(compiler_config, dict):
            raise RuntimeValidationError("Compiler config must be a JSON object")
        attestation = create_runtime_validation_attestation(
            recording_derivative=Path(args.recording),
            bundle_derivative=Path(args.bundle),
            run_dir=Path(args.run_dir),
            policy_source=args.policy,
            risk_class=args.risk_class,
            environment=args.environment,
            target_url=args.target_url,
            allowed_hosts=args.allowed_host,
            compiler_config=compiler_config,
            host=args.host,
            token=args.token,
            destination_kind=args.destination_kind,
            trusted_hosts=args.trusted_host,
        )
        output = save_runtime_validation_attestation(attestation, Path(args.out))
    except (OSError, json.JSONDecodeError, RuntimeValidationError) as exc:
        print(f"validate-hosted failed: {exc}")
        return 1
    print(f"Runtime-validation attestation written to {output}.")
    print(
        "This is a challenge-bound operator attestation, not independent "
        "certification. Upload it once with `push --validation-attestation`."
    )
    return 0


def _cmd_sanitize(args: argparse.Namespace) -> int:
    from openadapt_flow.sanitized_artifact import SanitizationError, sanitize_artifact

    try:
        manifest = sanitize_artifact(
            Path(args.path),
            Path(args.out),
            kind=args.kind,
            redactions_file=Path(args.redactions) if args.redactions else None,
            overwrite=args.overwrite,
        )
    except SanitizationError as e:
        print(f"sanitize failed: {e}")
        return 1
    print(
        f"Sanitized {manifest['processed_file_count']} file(s) into {args.out}; "
        f"execution semantics: {manifest['execution_semantics']}."
    )
    print(
        "Review locally: openadapt-flow review-sanitized "
        f"{args.out} --original {args.path}"
    )
    return 0


def _cmd_review_sanitized(args: argparse.Namespace) -> int:
    from openadapt_flow.sanitized_artifact import SanitizationError, serve_review

    try:
        serve_review(
            Path(args.original),
            Path(args.path),
            port=args.port,
            open_browser=not args.no_open,
        )
    except SanitizationError as e:
        print(f"review failed: {e}")
        return 1
    return 0


def _cmd_approve_sanitized(args: argparse.Namespace) -> int:
    from openadapt_flow.sanitized_artifact import SanitizationError, approve_derivative

    try:
        approval = approve_derivative(
            Path(args.path), source=Path(args.original), reviewer=args.reviewer
        )
    except SanitizationError as e:
        print(f"approval failed: {e}")
        return 1
    print(
        "Approved immutable archive "
        f"sha256={approval['approved_derivative_sha256']} "
        f"size={approval['approved_archive_size_bytes']} bytes."
    )
    return 0


def _cmd_report_break(args: argparse.Namespace) -> int:
    """Emit a PHI-free break diagnostic from a halted run's ``report.json``.

    Reads ``run_dir/report.json`` (``RunReport.halt`` / ``HaltObservation``) —
    halt is read from the report, NOT a process exit code — scrubs it
    fail-closed, and POSTs it to ``/api/runs/ingest-report`` so the break is
    triageable centrally. The recording never leaves the machine.
    """
    from openadapt_flow.hosted import HostedError, report_break

    try:
        result = report_break(
            args.run_dir,
            workflow_id=args.workflow_id,
            host=args.host,
            token=args.token,
            deployment_kind=args.deployment_kind,
            org_id=args.org_id,
            destination_kind=args.destination_kind,
            trusted_hosts=args.trusted_host,
        )
    except HostedError as e:
        print(f"report-break failed: {e}")
        return 1
    if not result.get("emitted"):
        if result.get("local_only"):
            print(f"Break kept LOCAL-ONLY: {result.get('reason')}")
        else:
            print(f"Nothing emitted: {result.get('reason')}")
        return 0
    print(
        f"Break reported (run_id={result.get('run_id')}, "
        f"halt_id={result.get('halt_id')}, status={result.get('status')})."
    )
    if result.get("teach_url"):
        print(f"Teach: {result['teach_url']}")
    return 0


def _maybe_report_break(run_dir: Path, report) -> None:
    """Opt-in post-run hook: emit a break diagnostic when a run halts.

    Off by default and fully best-effort — it only fires when BOTH
    ``OPENADAPT_FLOW_HOSTED_WORKFLOW_ID`` is set (the hosted workflow id this
    bundle maps to) and the run carries a halt. Any failure is swallowed so the
    hook NEVER changes the run's outcome or exit code (WRAP-not-rewrite).
    """
    import os

    workflow_id = os.environ.get("OPENADAPT_FLOW_HOSTED_WORKFLOW_ID")
    if not workflow_id or getattr(report, "halt", None) is None:
        return
    try:
        from openadapt_flow.hosted import report_break

        result = report_break(
            run_dir,
            workflow_id=workflow_id,
            deployment_kind=os.environ.get("OPENADAPT_FLOW_DEPLOYMENT_KIND", "cloud"),
            org_id=os.environ.get("OPENADAPT_FLOW_ORG_ID"),
        )
        if result.get("emitted"):
            print(f"Break reported to hosted control plane (workflow {workflow_id}).")
    except Exception as e:  # noqa: BLE001 — a diagnostic hook must never fail a run
        print(f"(break report skipped: {e})")


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


def _add_backend_flags(p: argparse.ArgumentParser) -> None:
    """Add the backend-selector flags (``--backend`` + targets) to a subparser.

    These override the ``backend`` section of a deployment ``--config``. Default
    (``web`` / no flag) reproduces the historical browser behavior byte-for-byte.
    """
    p.add_argument(
        "--backend",
        choices=["web", "windows", "macos", "rdp"],
        default=None,
        help=(
            "Backend to drive: 'web' (default; Playwright/Chromium), 'windows' "
            "(native Windows via the WAA HTTP agent — needs --agent-url), "
            "'macos' (one native Mac app window — needs --macos-app), or "
            "'rdp' (pixel-only remote desktop / Citrix — needs --rdp-host or a "
            "configured rdp_window). Overrides backend.kind from --config."
        ),
    )
    p.add_argument(
        "--agent-url",
        default=None,
        metavar="URL",
        help=(
            "Base URL of the in-guest Windows (WAA) agent for --backend windows "
            "(e.g. http://localhost:5001). Overrides backend.agent_url."
        ),
    )
    p.add_argument(
        "--macos-app",
        default=None,
        metavar="APP",
        help=(
            "Owner application for --backend macos (e.g. TextEdit). Overrides "
            "backend.macos_app."
        ),
    )
    p.add_argument(
        "--macos-window-title",
        default=None,
        metavar="TITLE",
        help=(
            "Window-title substring for --backend macos. Ambiguous matches "
            "are refused. Overrides backend.macos_window_title."
        ),
    )
    p.add_argument(
        "--rdp-host",
        default=None,
        metavar="HOST",
        help=(
            "RDP host/IP for --backend rdp (network RDP via FreeRDP). Overrides "
            "backend.rdp_host. For the local Citrix/Parallels window path set "
            "backend.rdp_window in --config instead."
        ),
    )


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
        choices=["none", "rest", "fhir", "sql", "file", "document-hash"],
        default=None,
        help=(
            "System-of-record EffectVerifier to wire so consequential writes "
            "are verified against the real record (not the screen). The sql/"
            "file kinds need their config fields (sql_query, root, ...) from "
            "a --config deployment YAML"
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


def _package_version() -> str:
    """The installed ``openadapt-flow`` distribution version.

    Falls back to the source tree's ``openadapt_flow.__version__`` when the
    package is not installed as a distribution (e.g. run from a checkout).
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("openadapt-flow")
    except PackageNotFoundError:
        from openadapt_flow import __version__

        return __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="openadapt-flow",
        description=(
            "Record a workflow once, compile it into a deterministic "
            "vision-anchored script, replay it locally, and use bounded "
            "re-resolution or governed repair when the interface drifts."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "record",
        help=(
            "Record YOUR workflow interactively: a headed browser "
            "(--backend web --url), or a native Windows desktop "
            "(--backend windows --agent-url) capturing the operator's real input"
        ),
    )
    p.add_argument(
        "--url",
        default=None,
        help="URL of the app to record against (required for --backend web)",
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
            "Record a typed value as a PARAMETER; its demonstrated value "
            "becomes the default, overridable at replay with --param. For "
            "--backend web, FIELD is the field name/id. For --backend "
            "windows/macos/rdp (no field identity on a pixel substrate), use "
            "NAME=VALUE — the typed value equal to VALUE is marked as parameter "
            "NAME. Repeatable."
        ),
    )
    p.add_argument(
        "--task",
        default=None,
        help=(
            "Task description for a desktop (--backend windows/macos/rdp) capture "
            "session (stored in the recording metadata)."
        ),
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser headless (scripted/CI recording)",
    )
    _add_backend_flags(p)
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
            "to demonstrate bounded drift resolution; only valid without --url"
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
        "--params-file",
        default=None,
        help=(
            "JSON object of parameter bindings; keeps values out of process "
            "arguments for managed execution"
        ),
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
    _add_backend_flags(p)
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
        "--params-file",
        default=None,
        help=(
            "JSON object of parameter bindings; keeps values out of process "
            "arguments for managed execution"
        ),
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
    _add_backend_flags(p)
    _add_deployment_flags(p, worklist=True)
    # Fail-closed admission-gate controls (see openadapt_flow.run_gate).
    p.add_argument(
        "--policy",
        default=None,
        metavar="NAME-OR-PATH",
        help=(
            "Certifying policy the bundle must PASS to run (default: the "
            "deployment config's policy, else 'clinical-write')"
        ),
    )
    p.add_argument(
        "--approve-unverified-writes",
        action="store_true",
        help=(
            "APPROVAL FALLBACK: explicitly approve executing writes whose "
            "effects cannot be independently verified in this deployment (no "
            "verifier configured). Without it such a bundle is refused"
        ),
    )
    p.add_argument(
        "--strict-templates",
        action="store_true",
        help=(
            "Refuse (not just warn) when template/screenshot assets are unsealed "
            "(plaintext at rest)"
        ),
    )
    p.add_argument(
        "--allow-unencrypted",
        action="store_true",
        help=(
            "Escape hatch: permit running a bundle whose workflow.json is NOT "
            "encrypted at rest (disables the encryption gate). Discouraged"
        ),
    )
    p.add_argument(
        "--pin-digest",
        default=None,
        metavar="SHA256",
        help="Refuse unless the bundle's sealed content digest equals this",
    )
    p.add_argument(
        "--pin-version",
        default=None,
        metavar="VERSION",
        help="Refuse unless the bundle's compiler version equals this",
    )
    p.add_argument(
        "--dry-run",
        "--explain",
        dest="dry_run",
        action="store_true",
        help="Print the fail-closed coverage report and exit WITHOUT executing",
    )
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
    # A deployment whose effect verifier binds run parameters
    # (effects.path_params / search_param_exprs / sql_query_params) needs the
    # SAME params to rebuild the verifier on resume — without them the
    # construction fails loud and the resume refuses. Mirror replay/run.
    p.add_argument(
        "--param",
        action="append",
        metavar="K=V",
        help=(
            "Parameter substitution (repeatable); required again on resume "
            "when the effect-verifier config binds run parameters"
        ),
    )
    p.add_argument(
        "--params-file",
        default=None,
        help=(
            "JSON object of parameter bindings; keeps values out of process "
            "arguments for managed execution (see --param)"
        ),
    )
    _add_backend_flags(p)
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

    p = sub.add_parser(
        "login",
        help=(
            "Validate an ingest token against the hosted control plane and "
            "store it in the OS keychain"
        ),
    )
    p.add_argument(
        "--token",
        default=None,
        help=(
            "Ingest token (oai_ingest_…). Falls back to OPENADAPT_INGEST_TOKEN, "
            "then OS keychain, then an existing config migration token. "
            "Mint one at <host>/dashboard/settings/ingest."
        ),
    )
    p.add_argument(
        "--host",
        default=None,
        help="Hosted base URL (default: config.toml host, else https://app.openadapt.ai)",
    )
    p.add_argument(
        "--destination-kind",
        choices=["openadapt-managed", "customer-managed", "local"],
        default=None,
        help="Trust class for the token destination",
    )
    p.add_argument(
        "--trusted-host",
        action="append",
        default=None,
        help="Exact allowed customer-managed origin (repeatable)",
    )
    p.add_argument(
        "--no-save",
        action="store_true",
        help="Validate only; do not store the host or token",
    )
    p.add_argument(
        "--allow-plaintext-token",
        action="store_true",
        help=(
            "Explicitly allow mode-0600 config token storage when no OS keychain "
            "is available (insecure fallback)"
        ),
    )
    p.set_defaults(func=_cmd_login)

    p = sub.add_parser(
        "sanitize",
        help="Create a verified PHI-scrubbed derivative without modifying the original",
    )
    p.add_argument("path", help="Original recording or bundle directory")
    p.add_argument("--out", required=True, help="New sanitized derivative directory")
    p.add_argument(
        "--kind", choices=["recording", "bundle"], required=True, help="Artifact type"
    )
    p.add_argument(
        "--redactions",
        default=None,
        help="Optional local JSON file with additional text/image redactions",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing derivative (never modifies the original)",
    )
    p.set_defaults(func=_cmd_sanitize)

    p = sub.add_parser(
        "review-sanitized",
        help="Review original vs sanitized content in a loopback-only local viewer",
    )
    p.add_argument("path", help="Sanitized derivative directory")
    p.add_argument("--original", required=True, help="Original artifact directory")
    p.add_argument(
        "--port", type=int, default=0, help="Loopback port (default: random)"
    )
    p.add_argument(
        "--no-open", action="store_true", help="Print the viewer URL without opening it"
    )
    p.set_defaults(func=_cmd_review_sanitized)

    p = sub.add_parser(
        "approve-sanitized",
        help="Approve and freeze the exact reviewed derivative as an immutable archive",
    )
    p.add_argument("path", help="Sanitized derivative directory")
    p.add_argument("--original", required=True, help="Original artifact directory")
    p.add_argument(
        "--reviewer", required=True, help="Reviewer identity for the audit record"
    )
    p.set_defaults(func=_cmd_approve_sanitized)

    p = sub.add_parser(
        "validate-hosted",
        help=(
            "Bind strict lint, policy certification, and a successful local "
            "replay to an expiring Cloud challenge and exact approved artifacts"
        ),
    )
    p.add_argument(
        "--recording",
        required=True,
        help="Approved sanitized recording derivative used to compile the bundle",
    )
    p.add_argument(
        "--bundle",
        required=True,
        help="Approved sanitized bundle derivative whose exact archive will upload",
    )
    p.add_argument(
        "--run-dir",
        required=True,
        help="Successful governed replay directory containing report.json",
    )
    p.add_argument(
        "--policy",
        required=True,
        help="Named or file-backed policy that the bundle must pass",
    )
    p.add_argument(
        "--risk-class",
        required=True,
        choices=["low", "consequential"],
        help=(
            "Compiled workflow risk class: low for reversible-only workflows, "
            "consequential when any step is irreversible; must match the bundle"
        ),
    )
    p.add_argument(
        "--environment",
        required=True,
        help=(
            "Non-PHI validation environment identifier; only its SHA-256 is uploaded"
        ),
    )
    p.add_argument(
        "--target-url",
        required=True,
        help=(
            "Exact non-PHI HTTPS entry URL used by the validated browser workflow; "
            "query strings, fragments, and credentials are refused"
        ),
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help=(
            "Additional exact hostname the hosted browser may reach (repeatable); "
            "the target hostname is included automatically"
        ),
    )
    p.add_argument(
        "--compiler-config",
        default=None,
        help="Optional JSON object describing the compile configuration",
    )
    p.add_argument("--out", required=True, help="Attestation JSON output path")
    p.add_argument(
        "--host",
        default=None,
        help="Hosted base URL (default: configured host or https://app.openadapt.ai)",
    )
    p.add_argument(
        "--destination-kind",
        choices=["openadapt-managed", "customer-managed", "local"],
        default=None,
        help="Trust class for the challenge destination",
    )
    p.add_argument(
        "--trusted-host",
        action="append",
        default=None,
        help="Exact allowed customer-managed origin (repeatable)",
    )
    p.add_argument(
        "--token",
        default=None,
        help="Ingest token used to acquire and sign the one-time challenge",
    )
    p.set_defaults(func=_cmd_validate_hosted)

    p = sub.add_parser(
        "push",
        help=(
            "Upload the exact approved sanitized archive to /api/ingest; "
            "raw input is sanitized locally and paused for review"
        ),
    )
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help=(
            "Recording (or bundle) directory to push. Default: the most-recent "
            "recording directory found under the current directory."
        ),
    )
    p.add_argument(
        "--kind",
        choices=["recording", "bundle"],
        default="recording",
        help="What the directory is (default: recording)",
    )
    p.add_argument(
        "--name",
        default=None,
        help="Workflow name (the server auto-suggests one otherwise)",
    )
    p.add_argument(
        "--workflow-id",
        default=None,
        help=(
            "Existing hosted workflow UUID to receive this validated bundle as "
            "a new active version (bundle uploads only)"
        ),
    )
    p.add_argument(
        "--deployment-kind",
        choices=["cloud", "byoc", "regulated"],
        default=None,
        help=(
            "Execution deployment lane (independent of destination trust; default: "
            "OPENADAPT_FLOW_DEPLOYMENT_KIND env, then config.toml "
            "deployment_lane, else cloud). All lanes may upload only a verified "
            "sanitized derivative."
        ),
    )
    p.add_argument(
        "--attest-non-phi",
        action="store_true",
        help=(
            "Deprecated and refused: declarations no longer bypass sanitization, "
            "review, or exact-hash approval."
        ),
    )
    p.add_argument(
        "--destination-kind",
        choices=["openadapt-managed", "customer-managed", "local"],
        default=None,
        help=(
            "Trust class for the upload endpoint. app.openadapt.ai is recognized "
            "automatically; customer-managed endpoints also require --trusted-host."
        ),
    )
    p.add_argument(
        "--trusted-host",
        action="append",
        default=None,
        help="Exact allowed customer-managed origin, e.g. https://control.example (repeatable)",
    )
    p.add_argument(
        "--sanitized-out",
        default=None,
        help="Where to create the derivative when PATH is raw (default: OPENADAPT_HOME)",
    )
    p.add_argument(
        "--auto-approve",
        action="store_true",
        default=None,
        help=(
            "Administrator policy approval for fully covered, stable derivatives; "
            "human review is the default."
        ),
    )
    p.add_argument(
        "--validation-attestation",
        default=None,
        help=("Challenge-bound runtime-validation JSON required for runnable bundles"),
    )
    p.add_argument(
        "--resolves-run-id",
        default=None,
        help=(
            "Halted run UUID resolved by this validated replacement; requires "
            "--kind bundle and --workflow-id"
        ),
    )
    p.add_argument(
        "--host",
        default=None,
        help="Hosted base URL (default: config.toml host, else https://app.openadapt.ai)",
    )
    p.add_argument(
        "--token",
        default=None,
        help=(
            "Ingest token (default: OPENADAPT_INGEST_TOKEN env, OS keychain, "
            "then an existing config migration token)"
        ),
    )
    p.set_defaults(func=_cmd_push)

    p = sub.add_parser(
        "report-break",
        help=(
            "Emit a PHI-free break diagnostic from a halted run's report.json "
            "to /api/runs/ingest-report (the recording stays local)"
        ),
    )
    p.add_argument("run_dir", help="The halted run directory (holds report.json)")
    p.add_argument(
        "--workflow-id",
        required=True,
        help="The hosted workflow id this run belongs to (from `push`/dashboard)",
    )
    p.add_argument(
        "--deployment-kind",
        choices=["cloud", "byoc"],
        default="cloud",
        help="Deployment lane (routes the teach target; default: cloud)",
    )
    p.add_argument(
        "--org-id",
        default=None,
        help="Org id, carried in the body until the per-user token store is canonical",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Hosted base URL (default: config.toml host, else https://app.openadapt.ai)",
    )
    p.add_argument(
        "--destination-kind",
        choices=["openadapt-managed", "customer-managed", "local"],
        default=None,
        help="Trust class for the break-report destination",
    )
    p.add_argument(
        "--trusted-host",
        action="append",
        default=None,
        help="Exact allowed customer-managed origin (repeatable)",
    )
    p.add_argument(
        "--token",
        default=None,
        help=(
            "Ingest token (default: OPENADAPT_INGEST_TOKEN env, OS keychain, "
            "then an existing config migration token)"
        ),
    )
    p.set_defaults(func=_cmd_report_break)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

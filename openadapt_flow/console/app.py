"""FastAPI application for the operator console (localhost-only, read-first).

``create_app`` builds the app over three on-disk roots -- bundles, runs, and
(optionally) skill libraries -- and a single ``allow_actions`` switch. With
``allow_actions=False`` (the default) EVERY mutating endpoint refuses with
HTTP 403 and returns the exact CLI command the operator can copy instead; the
GET surface is a pure read of engine artifacts either way.

Ids in URLs are resolved by re-scanning the root and matching the id against
the scan results (never by joining user input into a path), and run artifacts
are served only after a traversal-safe resolve inside the run directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from openadapt_flow import __version__
from openadapt_flow.console import actions as actions_mod
from openadapt_flow.console import data

_STATIC_DIR = Path(__file__).parent / "static"


def _resolve_bundle(root: Path, bundle_id: str) -> Path:
    path = data.resolve_scanned_dir(root, bundle_id, data._is_bundle_dir)
    if path is None:
        raise HTTPException(status_code=404, detail=f"no such bundle: {bundle_id}")
    return path


def _resolve_run(root: Path, run_id: str) -> Path:
    path = data.resolve_scanned_dir(root, run_id, data._is_run_dir)
    if path is None:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")
    return path


def _guess_bundle_for_run(
    bundles_root: Path, runs_root: Path, run_dir: Path
) -> Optional[str]:
    """Best-effort bundle path for a run: the durable manifest's recorded
    ``bundle_dir`` first, else a content-digest match over the bundle root."""
    manifest = data._read_json_opt(run_dir / "checkpoints" / "_manifest.json")
    if manifest and manifest.get("bundle_dir"):
        return str(manifest["bundle_dir"])
    report, _ = data._load_report(run_dir)
    if report is None or not report.bundle_content_digest:
        return None
    for summary in data.list_bundles(bundles_root):
        if summary.content_digest == report.bundle_content_digest:
            return summary.path
    return None


def _find_library(
    bundles_root: Path, skills_root: Optional[Path], library_path: str
) -> Path:
    for lib in data.find_skill_libraries(bundles_root, skills_root):
        if str(lib) == library_path:
            return lib
    raise HTTPException(
        status_code=404, detail=f"no such skill library: {library_path}"
    )


def _refuse_or_none(allow_actions: bool, command: str) -> None:
    """The read-only gate every mutation passes through: 403 + the exact
    command to copy when actions are disabled."""
    if not allow_actions:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "console is read-only (start with --allow-actions "
                "to enable governed actions)",
                "command": command,
            },
        )


def create_app(
    bundles_root: Path | str,
    runs_root: Path | str,
    skills_root: Path | str | None = None,
    *,
    allow_actions: bool = False,
) -> FastAPI:
    bundles = Path(bundles_root).resolve()
    runs = Path(runs_root).resolve()
    skills = Path(skills_root).resolve() if skills_root else None

    app = FastAPI(title="OpenAdapt Flow operator console", version=__version__)

    # -- meta ---------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "read_only": not allow_actions,
            "bundles_root": str(bundles),
            "runs_root": str(runs),
            "skills_root": str(skills) if skills else None,
        }

    # -- workflows ----------------------------------------------------------

    @app.get("/api/workflows")
    def workflows() -> list[dict[str, Any]]:
        last = data.latest_runs_by_workflow(runs)
        out = []
        for summary in data.list_bundles(bundles):
            d = summary.model_dump()
            d["last_run"] = last.get(summary.name) if summary.name else None
            out.append(d)
        return out

    @app.get("/api/workflows/{bundle_id:path}/diff/{other_id:path}")
    def workflow_diff(bundle_id: str, other_id: str) -> dict[str, Any]:
        a = _resolve_bundle(bundles, bundle_id)
        b = _resolve_bundle(bundles, other_id)
        return data.bundle_diff(bundles, a, b)

    @app.get("/api/workflows/{bundle_id:path}/actions")
    def workflow_actions(bundle_id: str) -> list[dict[str, Any]]:
        path = _resolve_bundle(bundles, bundle_id)
        summary = data.bundle_summary(bundles, path)
        return [
            a.model_dump()
            for a in actions_mod.actions_for_bundle(path, summary.policy_name)
        ]

    @app.get("/api/workflows/{bundle_id:path}")
    def workflow_detail(
        bundle_id: str, policy: Optional[str] = Query(default=None)
    ) -> dict[str, Any]:
        path = _resolve_bundle(bundles, bundle_id)
        return data.bundle_detail(bundles, path, policy_override=policy)

    # -- runs ---------------------------------------------------------------

    @app.get("/api/runs")
    def runs_list() -> list[dict[str, Any]]:
        return [r.model_dump() for r in data.list_runs(runs)]

    @app.get("/api/runs/{run_id:path}/artifact")
    def run_artifact(run_id: str, path: str = Query(...)) -> FileResponse:
        run_dir = _resolve_run(runs, run_id)
        artifact = data.safe_artifact(run_dir, path)
        if artifact is None:
            raise HTTPException(status_code=404, detail="no such artifact")
        return FileResponse(artifact)

    @app.get("/api/runs/{run_id:path}/actions")
    def run_actions(run_id: str) -> list[dict[str, Any]]:
        run_dir = _resolve_run(runs, run_id)
        summary = data.run_summary(runs, run_dir)
        bundle = _guess_bundle_for_run(bundles, runs, run_dir)
        return [
            a.model_dump()
            for a in actions_mod.actions_for_run(
                run_dir,
                halted=summary.halted,
                paused=summary.paused,
                bundle_dir=bundle,
            )
        ]

    @app.get("/api/runs/{run_id:path}")
    def run_detail(run_id: str) -> dict[str, Any]:
        run_dir = _resolve_run(runs, run_id)
        return data.run_detail(runs, run_dir)

    # -- skills -------------------------------------------------------------

    @app.get("/api/skills")
    def skills_list() -> list[dict[str, Any]]:
        return [
            data.skill_library_view(lib)
            for lib in data.find_skill_libraries(bundles, skills)
        ]

    # -- governed actions ---------------------------------------------------

    @app.post("/api/runs/{run_id:path}/actions/{action_id}")
    def execute_run_action(
        run_id: str, action_id: str, payload: dict[str, Any] | None = None
    ) -> JSONResponse:
        run_dir = _resolve_run(runs, run_id)
        summary = data.run_summary(runs, run_dir)
        bundle = _guess_bundle_for_run(bundles, runs, run_dir)
        specs = {
            a.id: a
            for a in actions_mod.actions_for_run(
                run_dir,
                halted=summary.halted,
                paused=summary.paused,
                bundle_dir=bundle,
            )
        }
        spec = specs.get(action_id)
        if spec is None:
            raise HTTPException(
                status_code=404,
                detail=f"action {action_id!r} not available for this run",
            )
        if not spec.executable:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": f"{action_id!r} needs operator input the console "
                    "cannot supply; copy the command instead",
                    "command": spec.command,
                },
            )
        _refuse_or_none(allow_actions, spec.command)
        kwargs = actions_mod.collect_execution_kwargs(payload or {})
        try:
            result = actions_mod.execute_run_action(
                action_id,
                run_dir,
                approver=kwargs.get("approver"),
                resolution=kwargs.get("resolution"),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return JSONResponse(result.model_dump())

    @app.post("/api/workflows/{bundle_id:path}/actions/{action_id}")
    def execute_bundle_action(
        bundle_id: str, action_id: str, payload: dict[str, Any] | None = None
    ) -> JSONResponse:
        path = _resolve_bundle(bundles, bundle_id)
        summary = data.bundle_summary(bundles, path)
        specs = {
            a.id: a for a in actions_mod.actions_for_bundle(path, summary.policy_name)
        }
        spec = specs.get(action_id)
        if spec is None:
            raise HTTPException(
                status_code=404,
                detail=f"action {action_id!r} not available for this bundle",
            )
        if not spec.executable:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": f"{action_id!r} must run in the deployment "
                    "environment; copy the command instead",
                    "command": spec.command,
                },
            )
        if spec.mutating:
            _refuse_or_none(allow_actions, spec.command)
        kwargs = actions_mod.collect_execution_kwargs(payload or {})
        try:
            result = actions_mod.execute_bundle_action(
                action_id, path, policy=kwargs.get("policy") or summary.policy_name
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return JSONResponse(result.model_dump())

    @app.post("/api/skills/{skill_id}/actions/{action_id}")
    def execute_skill_action(
        skill_id: str, action_id: str, payload: dict[str, Any]
    ) -> JSONResponse:
        library_path = payload.get("library")
        version = payload.get("version")
        if not isinstance(library_path, str) or not isinstance(version, int):
            raise HTTPException(
                status_code=400,
                detail="payload must carry 'library' (path str) and 'version' (int)",
            )
        lib = _find_library(bundles, skills, library_path)
        specs = {a.id: a for a in actions_mod.actions_for_skill(lib, skill_id, version)}
        spec = specs.get(action_id)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"no such action {action_id!r}")
        _refuse_or_none(allow_actions, spec.command)
        kwargs = actions_mod.collect_execution_kwargs(payload)
        try:
            result = actions_mod.execute_skill_action(
                action_id, lib, skill_id, version, reason=kwargs.get("reason")
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return JSONResponse(result.model_dump())

    # -- UI -----------------------------------------------------------------

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    return app

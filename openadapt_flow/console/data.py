"""Read-only projections of on-disk engine artifacts for the operator console.

Everything here READS artifacts the engine already writes -- bundle
directories (:meth:`openadapt_flow.ir.Workflow.load`), run directories
(``report.json`` / ``pending_escalation.json`` / ``approval.json`` /
``checkpoints/``), and skill libraries (``skills.json``) -- and computes
coverage with the SAME helpers the ``lint`` / ``certify`` CLI verbs use.
Nothing in this module writes to disk and nothing invents a new metric the
engine does not already define.

Degradation is deliberate and graceful: an encrypted bundle without a key, a
corrupt ``workflow.json``, or a report predating a field yields a summary with
``load_error`` / ``None`` fields instead of an exception, so one bad artifact
never blanks the operator's inventory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import RunReport, Step, Workflow
from openadapt_flow.policy import (
    CertifyReport,
    builtin_policy_names,
    evaluate_policy,
    has_system_effect,
    is_identity_applicable,
    is_identity_armed,
    lint_workflow,
    load_policy,
    step_confidence,
)
from openadapt_flow.traversal import iter_workflow_steps

#: Directory-scan depth for bundle / run roots: direct children plus one more
#: level (covers ``runs/replay-*`` and bench roots like ``bench/iter-*/``).
_SCAN_DEPTH = 2


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def _is_bundle_dir(path: Path) -> bool:
    return path.is_dir() and (
        (path / "workflow.json").is_file() or (path / "workflow.json.enc").is_file()
    )


def _is_run_dir(path: Path) -> bool:
    return path.is_dir() and (path / "report.json").is_file()


def _scan(root: Path, predicate: Any, depth: int = _SCAN_DEPTH) -> list[Path]:
    """Directories under ``root`` (including ``root`` itself) matching
    ``predicate``, breadth-first to ``depth`` levels, sorted by path."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    frontier = [root]
    for _ in range(depth + 1):
        nxt: list[Path] = []
        for d in frontier:
            if predicate(d):
                out.append(d)
                continue  # a bundle/run dir does not nest another
            try:
                nxt.extend(c for c in sorted(d.iterdir()) if c.is_dir())
            except OSError:
                continue
        frontier = nxt
    return sorted(out)


def _rel_id(root: Path, path: Path) -> str:
    """Stable, URL-safe id for a scanned directory: its path relative to the
    root, with ``/`` kept (the API accepts it via a path-validated lookup)."""
    if path == root:
        return "."
    return path.relative_to(root).as_posix()


def resolve_scanned_dir(root: Path, entry_id: str, predicate: Any) -> Optional[Path]:
    """Resolve an id BACK to a scanned directory, refusing anything that is
    not exactly one of the directories a fresh scan yields (so a crafted id
    can never escape the root)."""
    for d in _scan(root, predicate):
        if _rel_id(root, d) == entry_id:
            return d
    return None


# ---------------------------------------------------------------------------
# bundle projections
# ---------------------------------------------------------------------------


class IdentityCoverage(BaseModel):
    applicable: int = 0
    armed: int = 0
    unarmed: list[dict[str, str]] = Field(default_factory=list)


class EffectCoverage(BaseModel):
    """Effect-verification coverage: how many CONSEQUENTIAL (irreversible)
    actions carry a declared system-of-record contract (``Step.effects``).

    ``coverage_pct`` is None when the bundle has no irreversible step -- the
    UI shows "n/a" instead of a fabricated 100%.
    """

    consequential: int = 0
    consequential_with_contract: int = 0
    steps_with_contract: int = 0
    coverage_pct: Optional[float] = None


class BundleSummary(BaseModel):
    id: str
    path: str
    name: Optional[str] = None
    schema_version: Optional[int] = None
    compiler_version: Optional[str] = None
    created_at: Optional[str] = None
    content_digest: Optional[str] = None
    certified: Optional[bool] = None
    certification_status: Optional[str] = None
    policy_name: Optional[str] = None
    certified_at: Optional[str] = None
    expires_at: Optional[str] = None
    contains_phi: Optional[bool] = None
    phi_scrubbed: Optional[bool] = None
    encrypted: bool = False
    n_steps: Optional[int] = None
    load_error: Optional[str] = None
    last_run: Optional[dict[str, Any]] = None


def load_workflow_safe(path: Path) -> tuple[Optional[Workflow], Optional[str]]:
    """``Workflow.load`` that degrades to ``(None, reason)`` instead of
    raising -- encrypted-without-key, tampered, or corrupt bundles stay
    listable."""
    try:
        return Workflow.load(path), None
    except Exception as e:  # noqa: BLE001 - inventory must never blank
        return None, f"{type(e).__name__}: {e}"


def bundle_summary(root: Path, path: Path) -> BundleSummary:
    encrypted_on_disk = (path / "workflow.json.enc").is_file()
    wf, err = load_workflow_safe(path)
    if wf is None:
        return BundleSummary(
            id=_rel_id(root, path),
            path=str(path),
            encrypted=encrypted_on_disk,
            load_error=err,
        )
    prov = wf.manifest.provenance if wf.manifest else None
    return BundleSummary(
        id=_rel_id(root, path),
        path=str(path),
        name=wf.name,
        schema_version=wf.schema_version,
        compiler_version=prov.compiler_version if prov else None,
        created_at=(prov.created_at if prov else wf.created_at),
        content_digest=(wf.manifest.content_digest if wf.manifest else None),
        certified=prov.certified if prov else None,
        certification_status=prov.certification_status if prov else None,
        policy_name=prov.policy_name if prov else None,
        certified_at=prov.certified_at if prov else None,
        expires_at=prov.expires_at if prov else None,
        contains_phi=wf.contains_phi,
        phi_scrubbed=wf.phi_scrubbed,
        encrypted=wf.encrypted or encrypted_on_disk,
        n_steps=len(list(iter_workflow_steps(wf))),
        load_error=None,
    )


def list_bundles(root: Path) -> list[BundleSummary]:
    return [bundle_summary(root, p) for p in _scan(root, _is_bundle_dir)]


def identity_coverage(wf: Workflow) -> IdentityCoverage:
    cov = IdentityCoverage()
    for step in iter_workflow_steps(wf):
        if not is_identity_applicable(step):
            continue
        cov.applicable += 1
        if is_identity_armed(step):
            cov.armed += 1
        else:
            cov.unarmed.append(
                {
                    "step_id": step.id,
                    "intent": step.intent,
                    "reason": step.identity_unarmed_reason
                    or "no identity context recorded at compile time",
                }
            )
    return cov


def effect_coverage(wf: Workflow) -> EffectCoverage:
    cov = EffectCoverage()
    for step in iter_workflow_steps(wf):
        if has_system_effect(step):
            cov.steps_with_contract += 1
        if step.risk == "irreversible":
            cov.consequential += 1
            if has_system_effect(step):
                cov.consequential_with_contract += 1
    if cov.consequential:
        cov.coverage_pct = round(
            100.0 * cov.consequential_with_contract / cov.consequential, 1
        )
    return cov


def _step_projection(step: Step) -> dict[str, Any]:
    return {
        "id": step.id,
        "intent": step.intent,
        "action": step.action.value,
        "risk": step.risk,
        "secret": step.secret,
        "identity_applicable": is_identity_applicable(step),
        "identity_armed": (
            is_identity_armed(step) if is_identity_applicable(step) else None
        ),
        "identity_unarmed_reason": step.identity_unarmed_reason,
        "n_effects": len(step.effects),
        "effects": [
            {
                "kind": e.kind.value,
                "match_fields": sorted(e.match.keys()),
                "field": e.field,
                "expected_count": e.expected_count,
                "has_idempotency_key": bool(getattr(e, "idempotency_key", None)),
                "needs_operator_confirmation": bool(
                    getattr(e, "needs_operator_confirmation", False)
                ),
            }
            for e in step.effects
        ],
        "n_postconditions": len(step.expect),
        "postconditions": [{"kind": p.kind.value, "text": p.text} for p in step.expect],
        "confidence": round(step_confidence(step), 2),
        "param": step.param,
    }


def certification_view(
    wf: Workflow, policy_override: Optional[str] = None
) -> dict[str, Any]:
    """The bundle's SEALED certification block plus, when a policy is
    resolvable (the sealed ``policy_name``, an explicit override, or a
    builtin), a LIVE ``evaluate_policy`` pass with its violations.

    When no policy can be resolved the live block degrades to the exact
    ``certify`` command for the operator to run."""
    prov = wf.manifest.provenance if wf.manifest else None
    sealed_policy: Optional[str] = prov.policy_name if prov else None
    sealed = {
        "certified": prov.certified if prov else False,
        "status": prov.certification_status if prov else None,
        "policy_name": sealed_policy,
        "certified_at": prov.certified_at if prov else None,
        "expires_at": prov.expires_at if prov else None,
    }
    source = policy_override or sealed_policy
    live: Optional[dict[str, Any]] = None
    live_error: Optional[str] = None
    if source:
        try:
            report: CertifyReport = evaluate_policy(wf, load_policy(source))
            live = report.model_dump()
        except Exception as e:  # noqa: BLE001 - degrade, never 500 the page
            live_error = f"{type(e).__name__}: {e}"
    return {
        "sealed": sealed,
        "live": live,
        "live_error": live_error,
        "available_policies": builtin_policy_names(),
    }


def bundle_detail(
    root: Path, path: Path, policy_override: Optional[str] = None
) -> dict[str, Any]:
    summary = bundle_summary(root, path)
    if summary.load_error:
        return {"summary": summary.model_dump(), "load_error": summary.load_error}
    wf, _ = load_workflow_safe(path)
    assert wf is not None  # summary.load_error was None
    steps = [_step_projection(s) for s in iter_workflow_steps(wf)]
    lint = lint_workflow(wf)
    return {
        "summary": summary.model_dump(),
        "params": wf.params,
        "param_specs": {
            k: v.model_dump(mode="json") for k, v in wf.param_specs.items()
        },
        "secret_params": wf.secret_params,
        "program_mode": wf.program is not None,
        "steps": steps,
        "identity_coverage": identity_coverage(wf).model_dump(),
        "effect_coverage": effect_coverage(wf).model_dump(),
        "lint": lint.model_dump(),
        "certification": certification_view(wf, policy_override),
    }


# ---------------------------------------------------------------------------
# bundle diff
# ---------------------------------------------------------------------------


def _step_index(wf: Workflow) -> dict[str, Step]:
    return {s.id: s for s in iter_workflow_steps(wf)}


def bundle_diff(root: Path, path_a: Path, path_b: Path) -> dict[str, Any]:
    """Structural diff between two bundle directories: manifest-level fields
    plus steps added / removed / changed (by step id, comparing the serialized
    step models field-by-field)."""
    a_sum, b_sum = bundle_summary(root, path_a), bundle_summary(root, path_b)
    out: dict[str, Any] = {"a": a_sum.model_dump(), "b": b_sum.model_dump()}
    if a_sum.load_error or b_sum.load_error:
        out["error"] = "one or both bundles could not be loaded"
        return out
    wf_a, _ = load_workflow_safe(path_a)
    wf_b, _ = load_workflow_safe(path_b)
    assert wf_a is not None and wf_b is not None
    ia, ib = _step_index(wf_a), _step_index(wf_b)
    added = sorted(set(ib) - set(ia))
    removed = sorted(set(ia) - set(ib))
    changed: list[dict[str, Any]] = []
    for sid in sorted(set(ia) & set(ib)):
        da = ia[sid].model_dump(mode="json")
        db = ib[sid].model_dump(mode="json")
        fields = sorted(k for k in set(da) | set(db) if da.get(k) != db.get(k))
        if fields:
            changed.append({"step_id": sid, "fields": fields})
    out.update(
        {
            "params_changed": wf_a.params != wf_b.params,
            "steps_added": added,
            "steps_removed": removed,
            "steps_changed": changed,
            "identical": (
                not added and not removed and not changed and wf_a.params == wf_b.params
            ),
        }
    )
    return out


# ---------------------------------------------------------------------------
# run projections
# ---------------------------------------------------------------------------


class RunSummary(BaseModel):
    id: str
    path: str
    workflow_name: Optional[str] = None
    started_at: Optional[str] = None
    success: Optional[bool] = None
    terminal_outcome: Optional[str] = None
    halted: bool = False
    paused: bool = False
    approved: bool = False
    n_results: int = 0
    n_failed: int = 0
    total_ms: Optional[float] = None
    identity_applicable_steps: Optional[int] = None
    identity_armed_steps: Optional[int] = None
    screenshots_may_leave_box: Optional[bool] = None
    bundle_content_digest: Optional[str] = None
    load_error: Optional[str] = None


def _load_report(path: Path) -> tuple[Optional[RunReport], Optional[str]]:
    try:
        raw = json.loads((path / "report.json").read_text())
        return RunReport.model_validate(raw), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _read_json_opt(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def run_summary(root: Path, path: Path) -> RunSummary:
    report, err = _load_report(path)
    paused = (path / "pending_escalation.json").is_file() or (
        path / "pending_escalation.json.enc"
    ).is_file()
    approved = (path / "approval.json").is_file() or (
        path / "approval.json.enc"
    ).is_file()
    if report is None:
        return RunSummary(
            id=_rel_id(root, path),
            path=str(path),
            paused=paused,
            approved=approved,
            load_error=err,
        )
    return RunSummary(
        id=_rel_id(root, path),
        path=str(path),
        workflow_name=report.workflow_name,
        started_at=report.started_at,
        success=report.success,
        terminal_outcome=report.terminal_outcome,
        halted=report.halt is not None,
        paused=paused,
        approved=approved,
        n_results=len(report.results),
        n_failed=sum(1 for r in report.results if not r.ok and not r.skipped),
        total_ms=report.total_ms,
        identity_applicable_steps=report.identity_applicable_steps,
        identity_armed_steps=report.identity_armed_steps,
        screenshots_may_leave_box=report.screenshots_may_leave_box,
        bundle_content_digest=report.bundle_content_digest,
    )


def list_runs(root: Path) -> list[RunSummary]:
    runs = [run_summary(root, p) for p in _scan(root, _is_run_dir)]
    return sorted(runs, key=lambda r: r.started_at or "", reverse=True)


def latest_runs_by_workflow(root: Path) -> dict[str, dict[str, Any]]:
    """Newest run summary per workflow name -- the 'last run' join for the
    workflow list."""
    out: dict[str, dict[str, Any]] = {}
    for run in list_runs(root):  # already newest-first
        if run.workflow_name and run.workflow_name not in out:
            out[run.workflow_name] = {
                "run_id": run.id,
                "started_at": run.started_at,
                "success": run.success,
                "halted": run.halted,
                "paused": run.paused,
            }
    return out


def _checkpoints_listing(run_dir: Path) -> list[dict[str, Any]]:
    cdir = run_dir / "checkpoints"
    out: list[dict[str, Any]] = []
    if not cdir.is_dir():
        return out
    for p in sorted(cdir.glob("step_*.json")):
        data = _read_json_opt(p) or {}
        out.append(
            {
                "file": p.name,
                "step_index": data.get("step_index"),
                "step_id": data.get("step_id"),
                "intent": data.get("intent"),
                "created_at": data.get("created_at"),
            }
        )
    for p in sorted(cdir.glob("step_*.json.enc")):
        out.append({"file": p.name, "encrypted": True})
    return out


def run_detail(root: Path, path: Path) -> dict[str, Any]:
    summary = run_summary(root, path)
    out: dict[str, Any] = {"summary": summary.model_dump()}
    report, _ = _load_report(path)
    if report is not None:
        out["report"] = report.model_dump(mode="json")
        out["timeline"] = [
            {
                "step_id": r.step_id,
                "intent": r.intent,
                "ok": r.ok,
                "skipped": r.skipped,
                "safety_halt": r.safety_halt,
                "identity": r.identity.model_dump() if r.identity else None,
                "effect_verified": r.effect_verified,
                "effect_approved_unverified": r.effect_approved_unverified,
                "effect_results": r.effect_results,
                "actuation": r.actuation,
                "resolution_rung": r.resolution.rung if r.resolution else None,
                "error": r.error,
                "before_png": r.before_png,
                "after_png": r.after_png,
                "elapsed_ms": r.elapsed_ms,
            }
            for r in report.results
        ]
        out["halt"] = report.halt.model_dump() if report.halt else None
    pending = _read_json_opt(path / "pending_escalation.json")
    out["pending_escalation"] = pending
    out["pending_escalation_encrypted"] = (
        path / "pending_escalation.json.enc"
    ).is_file()
    out["approval"] = _read_json_opt(path / "approval.json")
    out["checkpoints"] = _checkpoints_listing(path)
    manifest = _read_json_opt(path / "checkpoints" / "_manifest.json")
    out["manifest"] = manifest
    return out


def safe_artifact(run_dir: Path, rel: str) -> Optional[Path]:
    """Resolve a run-dir-relative artifact path (e.g. a step screenshot),
    refusing traversal outside the run directory. None => not servable."""
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        return None
    candidate = (run_dir / rel).resolve()
    try:
        candidate.relative_to(run_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


# ---------------------------------------------------------------------------
# skill library projections
# ---------------------------------------------------------------------------


def find_skill_libraries(*roots: Optional[Path]) -> list[Path]:
    """Every ``skills.json`` under the given roots (depth-limited), i.e. the
    versioned skill libraries ``teach`` maintains next to updated bundles."""
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        if root is None or not root.is_dir():
            continue
        for depth_glob in ("skills.json", "*/skills.json", "*/*/skills.json"):
            for p in sorted(root.glob(depth_glob)):
                r = p.resolve()
                if r.is_file() and r not in seen:
                    seen.add(r)
                    out.append(r)
    return out


def skill_library_view(library_file: Path) -> dict[str, Any]:
    """Versions + lineage of every skill in one library, WITHOUT the (large)
    program graphs -- the governance metadata only."""
    from openadapt_flow.learning.library import SkillLibrary

    try:
        lib = SkillLibrary(library_file.parent)
    except Exception as e:  # noqa: BLE001
        return {"path": str(library_file), "error": f"{type(e).__name__}: {e}"}
    skills = []
    for sid in lib.skill_ids():
        skill = lib.get(sid)
        skills.append(
            {
                "skill_id": sid,
                "versions": [
                    {
                        "version": v.version,
                        "status": v.status,
                        "validation_score": v.validation_score,
                        "reason": v.reason,
                        "created_at": v.provenance.created_at,
                        "parent_version": v.provenance.parent_version,
                        "note": v.provenance.note,
                        "n_traces": len(v.provenance.trace_ids),
                    }
                    for v in skill.versions
                ],
            }
        )
    return {"path": str(library_file), "skills": skills}

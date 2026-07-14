"""Cloud connectivity for the openadapt-flow CLI (``login`` / ``push`` /
break-report emitter).

This is a thin, additive WRAPPER around the existing engine: it changes no
compiler / IR / replay internals. It adds exactly three capabilities the local
loop needs to talk to the hosted control plane (``app.openadapt.ai``):

* :func:`login`  — validate an ingest token against the hosted API and record
  the non-secret host (and, as a documented-insecure last resort when no OS
  keychain is available, the token) into ``~/.openadapt/config.toml``.
* :func:`push`   — zip a recording (or a compiled bundle) DIRECTORY to a temp
  ``.zip`` and upload it as ``multipart/form-data`` to ``POST /api/ingest``,
  returning the server-assigned ``workflow_id`` + dashboard URL.
* :func:`report_break` — serialize a halted run's ``report.json`` (its
  :class:`~openadapt_flow.ir.HaltObservation` / ``RunReport.halt``) into a
  PHI-free diagnostic and ``POST`` it to ``/api/runs/ingest-report`` so a break
  is triageable centrally WITHOUT any recording leaving the machine.

Design notes (grounded in the desktop/tray architecture spec, §3a/§3b/§3c/§3e,
§8):

* **Halt signaling is read from ``report.json``** (``RunReport.halt`` /
  ``HaltObservation``), NEVER from a process exit code. ``replay`` / ``run``
  return 0/1 only; there is no "exit 2 = safe halt". The break emitter parses
  the report, so a wrapping caller (the desktop engine, the cloud runner) reads
  the same source of truth (§8 item 3).
* **Bundle/recording is a DIRECTORY; the ingest API wants a ``.zip``** — the
  push path zips before upload (§8 item 4).
* **Secrets belong in the OS keychain**, which the desktop app owns (via
  ``keyring``). ``openadapt-flow`` has no keychain dependency, so its ``login``
  falls back to ``config.toml`` with ``0600`` perms and a printed warning — the
  documented-insecure last resort in the resolution precedence (§3e). The
  desktop app supersedes this by storing the token in the OS keychain.

Token resolution precedence (for ``push`` / ``report_break`` and any outbound
call): ``--token`` argument  ->  ``OPENADAPT_INGEST_TOKEN`` env  ->
``~/.openadapt/config.toml`` ``[hosted] token``.

Only the existing ``httpx`` dependency (and the stdlib) is used — no new heavy
dependency is introduced.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

import httpx

__all__ = [
    "DEFAULT_HOST",
    "HostedError",
    "config_path",
    "resolve_host",
    "resolve_token",
    "find_latest_recording",
    "login",
    "push",
    "report_break",
]

#: The hosted control plane. Overridable per call (``--host``) or via
#: ``~/.openadapt/config.toml`` ``[hosted] host``.
DEFAULT_HOST = "https://app.openadapt.ai"

#: Environment variable read for the ingest token (the non-interactive / CI /
#: BYOC-server path).
TOKEN_ENV = "OPENADAPT_INGEST_TOKEN"

#: Network timeouts (seconds). Uploads can be large, so the push timeout is
#: generous; the lightweight validate/report calls use a short timeout.
_UPLOAD_TIMEOUT = 120.0
_API_TIMEOUT = 15.0


class HostedError(RuntimeError):
    """A hosted-connectivity failure (auth, network, or a non-2xx response)."""


# ---------------------------------------------------------------------------
# config.toml (non-secret host/lane config; the last-resort token store)
# ---------------------------------------------------------------------------


def config_path() -> Path:
    """Path to the shared engine config: ``~/.openadapt/config.toml``.

    The root is overridable via ``OPENADAPT_HOME`` so tests (and a sandboxed
    deployment) never touch the real home directory.
    """
    root = os.environ.get("OPENADAPT_HOME")
    base = Path(root) if root else Path.home() / ".openadapt"
    return base / "config.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file into a dict (stdlib ``tomllib`` on 3.11+, else a
    minimal ``[table] key = "value"`` fallback for 3.10)."""
    if not path.is_file():
        return {}
    try:
        import tomllib

        with path.open("rb") as fh:
            return tomllib.load(fh)
    except ModuleNotFoundError:
        return _load_toml_minimal(path)


def _load_toml_minimal(path: Path) -> dict[str, Any]:
    """A tiny TOML reader for Python 3.10 (no ``tomllib``).

    Handles exactly what ``config.toml`` needs: ``[table]`` headers and
    ``key = value`` scalar lines (quoted strings, bools, ints, floats). Enough
    for the ``[hosted]`` section; not a general TOML parser.
    """
    data: dict[str, Any] = {}
    table: dict[str, Any] = data
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            table = data.setdefault(name, {})
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        table[key.strip()] = _parse_scalar(value.strip())
    return data


def _parse_scalar(raw: str) -> Any:
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        return raw[1:-1]
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _dump_toml(data: dict[str, Any]) -> str:
    """Serialize a one-level-nested dict (top-level scalars + ``[table]``s of
    scalars) back to TOML. Sufficient for ``config.toml``'s shape."""
    lines: list[str] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_scalar(value)}")
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append("")
            lines.append(f"[{key}]")
            for sub_key, sub_value in value.items():
                lines.append(f"{sub_key} = {_toml_scalar(sub_value)}")
    return "\n".join(lines) + "\n"


def _hosted_config() -> dict[str, Any]:
    """The ``[hosted]`` table from ``config.toml`` (empty when absent)."""
    section = _load_toml(config_path()).get("hosted", {})
    return section if isinstance(section, dict) else {}


def _update_hosted_config(updates: dict[str, Any]) -> Path:
    """Merge ``updates`` into the ``[hosted]`` table and write ``config.toml``
    back with ``0600`` permissions (it may hold the last-resort token)."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_toml(path)
    hosted = data.get("hosted")
    if not isinstance(hosted, dict):
        hosted = {}
    hosted.update({k: v for k, v in updates.items() if v is not None})
    data["hosted"] = hosted
    path.write_text(_dump_toml(data))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------------------
# host + token resolution
# ---------------------------------------------------------------------------


def resolve_host(host: Optional[str] = None) -> str:
    """Resolve the hosted base URL: ``host`` arg  ->  ``config.toml``  ->
    :data:`DEFAULT_HOST`. Trailing slash stripped."""
    resolved = host or _hosted_config().get("host") or DEFAULT_HOST
    return str(resolved).rstrip("/")


def resolve_token(token: Optional[str] = None) -> str:
    """Resolve the ingest token by precedence, or raise :class:`HostedError`.

    Precedence: ``token`` arg  ->  ``OPENADAPT_INGEST_TOKEN`` env  ->
    ``~/.openadapt/config.toml`` ``[hosted] token`` (the documented-insecure
    last resort; the desktop app stores it in the OS keychain instead).
    """
    resolved = token or os.environ.get(TOKEN_ENV) or _hosted_config().get("token")
    if not resolved:
        raise HostedError(
            "No ingest token. Pass --token, set "
            f"{TOKEN_ENV}, or run `openadapt-flow login` to store one. "
            "Mint a token at <host>/dashboard/settings/ingest."
        )
    return str(resolved)


def _auth_headers(token: str) -> dict[str, str]:
    """The bearer header every outbound call carries (also acceptable to the
    API as ``x-ingest-token``)."""
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# recording / bundle discovery + zipping
# ---------------------------------------------------------------------------


def _is_recording_dir(path: Path) -> bool:
    return (path / "meta.json").is_file() and (path / "events.jsonl").is_file()


def _is_bundle_dir(path: Path) -> bool:
    return (path / "workflow.json").is_file() or (path / "workflow.json.enc").is_file()


def find_latest_recording(base: Optional[Path] = None) -> Path:
    """Return the most-recently-modified recording directory.

    Searches the immediate children of a few conventional roots (``base`` or
    the CWD, plus ``recordings/`` and ``runs/`` under it). A recording dir has
    ``meta.json`` + ``events.jsonl``. Raises :class:`HostedError` when none is
    found — the caller must then pass an explicit PATH.
    """
    root = Path(base) if base else Path.cwd()
    roots = [root, root / "recordings", root / "runs"]
    candidates: list[Path] = []
    for r in roots:
        if not r.is_dir():
            continue
        if _is_recording_dir(r):
            candidates.append(r)
        for child in r.iterdir():
            if child.is_dir() and _is_recording_dir(child):
                candidates.append(child)
    if not candidates:
        raise HostedError(
            f"No recording directory found under {root} "
            "(looked for meta.json + events.jsonl). Pass an explicit PATH."
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _zip_dir(src: Path) -> Path:
    """Zip a directory's CONTENTS (files at the archive root) to a temp
    ``.zip`` and return its path. The caller is responsible for deleting it."""
    if not src.is_dir():
        raise HostedError(f"Not a directory: {src}")
    tmp = tempfile.mkdtemp(prefix="openadapt-flow-push-")
    base = Path(tmp) / src.name
    archive = shutil.make_archive(str(base), "zip", root_dir=str(src))
    return Path(archive)


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def login(
    token: Optional[str] = None,
    host: Optional[str] = None,
    *,
    save: bool = True,
) -> dict[str, Any]:
    """Validate an ingest token against the hosted API and (optionally) record
    the host + token into ``~/.openadapt/config.toml``.

    Args:
        token: The ingest token (``oai_ingest_…``). Falls back to
            ``OPENADAPT_INGEST_TOKEN`` env, then ``config.toml`` — so a
            re-login with no argument re-validates the stored token.
        host: Hosted base URL (default: ``config.toml`` host, else
            :data:`DEFAULT_HOST`).
        save: When True (default), persist host + token to ``config.toml`` on a
            successful validation (see the module docstring on secret storage).

    Returns:
        ``{"host": <str>, "valid": True, "settings_url": <str>,
           "config_path": <str|None>}``.

    Raises:
        HostedError: the token is missing, invalid (401), or the API is
            unreachable.
    """
    resolved_host = resolve_host(host)
    resolved_token = resolve_token(token)
    # Validate with a cheap authenticated GET (resolves the token -> org).
    url = f"{resolved_host}/api/needs-attention/count"
    try:
        resp = httpx.get(
            url, headers=_auth_headers(resolved_token), timeout=_API_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise HostedError(f"Could not reach {resolved_host}: {exc}") from exc
    if resp.status_code == 401:
        raise HostedError(
            "Ingest token was rejected (401). Mint a fresh token at "
            f"{resolved_host}/dashboard/settings/ingest."
        )
    if resp.status_code >= 400:
        raise HostedError(
            f"Token validation failed ({resp.status_code}) against {url}."
        )
    saved_to: Optional[str] = None
    if save:
        path = _update_hosted_config({"host": resolved_host, "token": resolved_token})
        saved_to = str(path)
    return {
        "host": resolved_host,
        "valid": True,
        "settings_url": f"{resolved_host}/dashboard/settings/ingest",
        "config_path": saved_to,
    }


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def push(
    path: Optional[Any] = None,
    *,
    kind: str = "recording",
    name: Optional[str] = None,
    host: Optional[str] = None,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """Zip a recording/bundle DIRECTORY and upload it to ``POST /api/ingest``.

    Args:
        path: The recording (or compiled bundle) directory. Defaults to the
            most-recent recording dir (:func:`find_latest_recording`).
        kind: ``"recording"`` (default) or ``"bundle"``.
        name: Optional workflow name (the server auto-suggests one otherwise).
        host: Hosted base URL (default resolution).
        token: Ingest token (default resolution).

    Returns:
        The server ``ingest`` object plus a convenience
        ``dashboard_url`` — e.g. ``{"workflow_id": …, "workflow_name": …,
        "kind": …, "compile": {…}, "dashboard_url": …}``.

    Raises:
        HostedError: bad ``kind``, missing path, auth failure, or a non-201
            response.
    """
    if kind not in ("recording", "bundle"):
        raise HostedError(f"--kind must be 'recording' or 'bundle', got {kind!r}")
    resolved_host = resolve_host(host)
    resolved_token = resolve_token(token)

    src = Path(path) if path is not None else find_latest_recording()
    if not src.is_dir():
        raise HostedError(f"PATH is not a directory: {src}")

    zip_path = _zip_dir(src)
    data: dict[str, str] = {"kind": kind}
    if name:
        data["name"] = name
    try:
        with zip_path.open("rb") as fh:
            resp = httpx.post(
                f"{resolved_host}/api/ingest",
                headers=_auth_headers(resolved_token),
                files={"file": (f"{src.name}.zip", fh, "application/zip")},
                data=data,
                timeout=_UPLOAD_TIMEOUT,
            )
    except httpx.HTTPError as exc:
        raise HostedError(
            f"Upload to {resolved_host}/api/ingest failed: {exc}"
        ) from exc
    finally:
        shutil.rmtree(zip_path.parent, ignore_errors=True)

    if resp.status_code == 401:
        raise HostedError("Ingest token was rejected (401).")
    if resp.status_code != 201:
        raise HostedError(
            f"Ingest returned {resp.status_code} (expected 201): {_body_snippet(resp)}"
        )
    payload = resp.json()
    ingest = payload.get("ingest", payload) if isinstance(payload, dict) else {}
    workflow_id = ingest.get("workflow_id")
    result = dict(ingest)
    if workflow_id:
        result["dashboard_url"] = f"{resolved_host}/dashboard/workflows/{workflow_id}"
    return result


# ---------------------------------------------------------------------------
# break report
# ---------------------------------------------------------------------------


def _body_snippet(resp: httpx.Response, limit: int = 300) -> str:
    try:
        return resp.text[:limit]
    except Exception:  # noqa: BLE001 — diagnostics only, never raise here
        return "<unreadable body>"


def _scrub(text: Optional[str]) -> str:
    """Fail-closed PHI scrub of a single diagnostic string (empty for None)."""
    from openadapt_flow import privacy

    return privacy.scrub_text(text) or ""


def _drift_signature(state_id: str, reason: str) -> str:
    """A stable, PHI-free fingerprint of the halt situation (for dedup)."""
    digest = hashlib.sha256(f"{state_id}|{reason}".encode("utf-8")).hexdigest()
    return digest[:16]


def _last_failed_rung(report: Any) -> Optional[str]:
    """The resolution rung of the last non-ok step, if any (diagnostic)."""
    for step in reversed(report.results):
        if not step.ok and step.resolution is not None:
            return step.resolution.rung
    return None


def _build_break_payload(
    report: Any,
    *,
    workflow_id: str,
    deployment_kind: str,
    org_id: Optional[str],
    report_path: Path,
    include_error: bool = True,
) -> dict[str, Any]:
    """Assemble the PHI-free ``/api/runs/ingest-report`` payload from a
    ``RunReport`` (§3c). All free-text fields pass through the fail-closed
    scrubber; no screenshots / field values / DOM / report body are included."""
    halt = report.halt
    step_intent = _scrub(halt.intent if halt else "")
    reason = _scrub(halt.reason if halt else "")
    status = "halt" if halt is not None else "failed"

    error: Optional[str] = None
    if include_error:
        for step in reversed(report.results):
            if step.error:
                error = _scrub(step.error)
                break

    steps = len(report.results)
    duration_s = round(report.total_ms / 1000.0, 3)
    state_id = halt.state_id if halt else ""

    payload: dict[str, Any] = {
        "org_id": org_id,
        "workflow_id": workflow_id,
        "deployment_kind": deployment_kind,
        "status": status,
        "step_intent": step_intent,
        "reason": reason,
        "resolver_rung": _last_failed_rung(report),
        "drift_signature": _drift_signature(state_id, reason),
        "report_path": str(report_path),
        "metrics": {"steps": steps, "duration_s": duration_s},
    }
    if error:
        payload["error"] = error
    return payload


def report_break(
    run_dir: Any,
    *,
    workflow_id: str,
    host: Optional[str] = None,
    token: Optional[str] = None,
    deployment_kind: str = "cloud",
    org_id: Optional[str] = None,
    allow_local_fallback: bool = True,
) -> dict[str, Any]:
    """Emit a PHI-free break diagnostic for a halted run to
    ``POST /api/runs/ingest-report`` (§3c).

    Reads ``run_dir/report.json`` (:class:`~openadapt_flow.ir.RunReport`) and,
    when it carries a halt (or failed), posts a scrubbed descriptor so the break
    is triageable centrally. The recording NEVER leaves the machine — only the
    diagnostic (§8 item 3: halt is read from ``report.json``, not an exit code).

    Args:
        run_dir: The run directory holding ``report.json``.
        workflow_id: The hosted workflow id this run belongs to (returned by
            :func:`push` / the dashboard).
        host: Hosted base URL (default resolution).
        token: Ingest token (default resolution).
        deployment_kind: ``"cloud"`` or ``"byoc"`` (routes the teach target).
        org_id: The org id, carried in the body until the per-user token store
            is canonical server-side (§3c note).
        allow_local_fallback: When the server rejects the payload as a PHI
            boundary violation (422) even after a harder scrub — or the
            scrubber is unavailable under a fail-closed policy — return a
            ``local_only`` result instead of raising.

    Returns:
        On success: the server response (``run_id`` / ``halt_id`` / ``status`` /
        ``teach_url``) plus ``{"emitted": True}``. When there is nothing to
        report (a successful run): ``{"emitted": False, "reason": …}``. On a
        PHI-boundary fallback: ``{"emitted": False, "local_only": True, …}``.

    Raises:
        HostedError: missing report, auth failure, or a non-2xx/422 response
            (422 is handled by scrub+retry then local fallback).
    """
    from openadapt_flow import privacy
    from openadapt_flow.ir import RunReport

    run_path = Path(run_dir)
    report_path = run_path / "report.json"
    if not report_path.is_file():
        raise HostedError(f"No report.json in {run_path} — nothing to report.")
    report = RunReport.model_validate_json(report_path.read_text())

    if report.halt is None and report.success:
        return {"emitted": False, "reason": "run succeeded; no halt to report"}

    resolved_host = resolve_host(host)
    resolved_token = resolve_token(token)
    url = f"{resolved_host}/api/runs/ingest-report"

    # Build + post; fail-closed scrub may raise PrivacyNotAvailable when the
    # compliance-pinned policy (SCRUB=on) is set without the capability.
    try:
        payload = _build_break_payload(
            report,
            workflow_id=workflow_id,
            deployment_kind=deployment_kind,
            org_id=org_id,
            report_path=report_path,
        )
    except privacy.PrivacyNotAvailable:
        if allow_local_fallback:
            return {
                "emitted": False,
                "local_only": True,
                "reason": "scrubber unavailable under fail-closed policy",
            }
        raise

    resp = _post_report(url, resolved_token, payload)

    if resp.status_code == 422:
        # PHI boundary violation (fail-closed). Retry once with a harder,
        # error-free payload; if it still trips, fall back to local-only.
        harder = _build_break_payload(
            report,
            workflow_id=workflow_id,
            deployment_kind=deployment_kind,
            org_id=org_id,
            report_path=report_path,
            include_error=False,
        )
        resp = _post_report(url, resolved_token, harder)
        if resp.status_code == 422:
            if allow_local_fallback:
                return {
                    "emitted": False,
                    "local_only": True,
                    "reason": "server rejected payload as PHI boundary (422)",
                    "detail": _body_snippet(resp),
                }
            raise HostedError(
                f"ingest-report rejected as PHI boundary (422): {_body_snippet(resp)}"
            )

    if resp.status_code == 401:
        raise HostedError("Ingest token was rejected (401).")
    if resp.status_code not in (200, 202):
        raise HostedError(
            f"ingest-report returned {resp.status_code} (expected 202): "
            f"{_body_snippet(resp)}"
        )
    body = resp.json() if resp.content else {}
    result = dict(body) if isinstance(body, dict) else {"response": body}
    result["emitted"] = True
    teach_url = result.get("teach_url")
    if teach_url and str(teach_url).startswith("/"):
        result["teach_url"] = f"{resolved_host}{teach_url}"
    return result


def _post_report(url: str, token: str, payload: dict[str, Any]) -> httpx.Response:
    """POST a JSON break payload; wrap transport errors as :class:`HostedError`."""
    try:
        return httpx.post(
            url,
            headers={**_auth_headers(token), "x-ingest-token": token},
            json=payload,
            timeout=_API_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise HostedError(f"POST {url} failed: {exc}") from exc

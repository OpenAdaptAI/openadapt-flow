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
    "resolve_deployment_kind",
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

#: Environment variable naming the deployment lane (``cloud`` | ``byoc`` |
#: ``regulated``). Falls back to ``config.toml`` ``[hosted] deployment_lane``.
DEPLOYMENT_KIND_ENV = "OPENADAPT_FLOW_DEPLOYMENT_KIND"

#: Environment variable forcing PHI mode (a truthy value treats every recording
#: as PHI-bearing, so it is never uploaded to the multi-tenant cloud).
PHI_MODE_ENV = "OPENADAPT_FLOW_PHI_MODE"

#: Lanes whose recordings must NEVER leave the customer machine/tenant. A
#: recording on one of these lanes is refused for upload (teach locally); only a
#: compiled, PHI-free bundle may be pushed.
_REGULATED_LANES = frozenset({"byoc", "regulated"})

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


def resolve_deployment_kind(deployment_kind: Optional[str] = None) -> str:
    """Resolve the deployment lane: arg -> ``OPENADAPT_FLOW_DEPLOYMENT_KIND`` env
    -> ``config.toml`` ``[hosted] deployment_lane`` -> ``"cloud"``.

    The lane governs the outbound PHI boundary: ``cloud`` is multi-tenant (a
    recording must be scrubbed before upload); ``byoc``/``regulated`` are
    single-tenant/on-prem (a raw recording must never be uploaded at all)."""
    resolved = (
        deployment_kind
        or os.environ.get(DEPLOYMENT_KIND_ENV)
        or _hosted_config().get("deployment_lane")
        or "cloud"
    )
    return str(resolved).strip().lower()


def _phi_mode(phi_mode: Optional[bool] = None) -> bool:
    """Resolve PHI mode: arg -> ``OPENADAPT_FLOW_PHI_MODE`` env -> ``SCRUB=on``.

    When True, every recording is treated as PHI-bearing and is never uploaded
    to the multi-tenant cloud (only compiled, PHI-free bundles may be pushed)."""
    if phi_mode is not None:
        return phi_mode
    env = os.environ.get(PHI_MODE_ENV)
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    from openadapt_flow import privacy

    return privacy.scrub_mode() == "on"


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


#: Recording artifacts scrubbed before a cloud upload. Frames are image-redacted;
#: structured/text artifacts are run through the text scrubber (whole-file — a
#: best-effort de-identification that preserves surrounding JSON syntax).
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_TEXT_SUFFIXES = frozenset({".json", ".jsonl", ".txt", ".md", ".csv"})


def _scrub_recording_tree(src: Path) -> Path:
    """Copy a recording directory to a temp location with every frame + text
    artifact PHI-scrubbed, and return the scrubbed COPY (the original is never
    mutated). The caller deletes the returned dir's parent.

    Fail-closed: raises :class:`HostedError` if no scrubbing provider is
    available (the caller must first check
    :func:`openadapt_flow.privacy.scrubbing_available`) or if any single frame
    cannot be redacted — an unredactable frame must abort the upload, never ship
    raw.
    """
    from openadapt_flow import privacy

    scrubber = privacy.get_scrubber()
    if scrubber is None:  # defensive; the caller gates on scrubbing_available()
        raise HostedError(
            "Refusing to upload a recording: no PHI scrubber is available. "
            "Install it with: pip install 'openadapt-flow[privacy]'."
        )
    tmp = Path(tempfile.mkdtemp(prefix="openadapt-flow-scrub-"))
    dest = tmp / src.name
    shutil.copytree(src, dest)
    try:
        for path in sorted(dest.rglob("*")):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in _IMAGE_SUFFIXES:
                path.write_bytes(
                    privacy.scrub_image_bytes(path.read_bytes(), force=True)
                )
            elif suffix in _TEXT_SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="replace")
                path.write_text(scrubber.scrub_text(text), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — fail closed: never ship raw PHI
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(exc, HostedError):
            raise
        raise HostedError(
            f"Refusing to upload: PHI scrub of the recording failed ({exc}). "
            "The recording was NOT uploaded."
        ) from exc
    return dest


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
    deployment_kind: Optional[str] = None,
    phi_mode: Optional[bool] = None,
) -> dict[str, Any]:
    """Zip a recording/bundle DIRECTORY and upload it to ``POST /api/ingest``.

    PHI boundary (fail-closed) — a raw recording carries full-frame screenshots
    and typed field values, so it is NOT uploaded blindly:

    * On a ``byoc``/``regulated`` lane, or under PHI mode, a **recording** is
      REFUSED for upload (teach locally; only a compiled, PHI-free ``bundle``
      may be pushed). This keeps regulated recordings on the customer machine.
    * On the ``cloud`` (multi-tenant) lane, a **recording** is de-identified
      (frames image-redacted, text artifacts scrubbed) on a temp copy BEFORE
      upload. If no scrubber is available, the upload is REFUSED rather than
      shipping raw PHI.
    * A ``bundle`` is already PHI-free by construction (the compiler strips
      field values / screenshots), so it uploads directly on any lane.

    Args:
        path: The recording (or compiled bundle) directory. Defaults to the
            most-recent recording dir (:func:`find_latest_recording`).
        kind: ``"recording"`` (default) or ``"bundle"``.
        name: Optional workflow name (the server auto-suggests one otherwise).
        host: Hosted base URL (default resolution).
        token: Ingest token (default resolution).
        deployment_kind: Deployment lane (``cloud`` | ``byoc`` | ``regulated``);
            default resolution via :func:`resolve_deployment_kind`.
        phi_mode: Force PHI mode; default resolution via env/``SCRUB=on``.

    Returns:
        The server ``ingest`` object plus a convenience
        ``dashboard_url`` — e.g. ``{"workflow_id": …, "workflow_name": …,
        "kind": …, "compile": {…}, "dashboard_url": …}``.

    Raises:
        HostedError: bad ``kind``, missing path, a refused PHI boundary, auth
            failure, or a non-201 response.
    """
    if kind not in ("recording", "bundle"):
        raise HostedError(f"--kind must be 'recording' or 'bundle', got {kind!r}")
    resolved_host = resolve_host(host)
    resolved_token = resolve_token(token)
    lane = resolve_deployment_kind(deployment_kind)

    src = Path(path) if path is not None else find_latest_recording()
    if not src.is_dir():
        raise HostedError(f"PATH is not a directory: {src}")

    # PHI boundary for RAW recordings (bundles are PHI-free by construction).
    upload_src = src
    scrub_tmp: Optional[Path] = None
    if kind == "recording":
        from openadapt_flow import privacy

        if lane in _REGULATED_LANES or _phi_mode(phi_mode):
            raise HostedError(
                f"Refusing to upload a raw recording on the {lane!r} lane"
                + (" (PHI mode)" if lane not in _REGULATED_LANES else "")
                + ". A recording carries full-frame screenshots and typed field "
                "values; on a regulated/PHI deployment it must never leave the "
                "machine. Teach locally, then push the compiled bundle "
                "(--kind bundle), which is PHI-free."
            )
        if not privacy.scrubbing_available():
            raise HostedError(
                "Refusing to upload a recording to the cloud: no PHI scrubber "
                "is available to de-identify its frames/values first. Install "
                "it with: pip install 'openadapt-flow[privacy]' (and "
                "python -m spacy download en_core_web_trf), or teach locally "
                "and push the compiled bundle (--kind bundle)."
            )
        upload_src = _scrub_recording_tree(src)
        scrub_tmp = upload_src.parent

    zip_path = _zip_dir(upload_src)
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
        if scrub_tmp is not None:
            shutil.rmtree(scrub_tmp, ignore_errors=True)

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
    include_free_text: bool = True,
) -> dict[str, Any]:
    """Assemble the PHI-free ``/api/runs/ingest-report`` payload from a
    ``RunReport`` (§3c). No screenshots / field values / DOM / report body are
    ever included.

    The free-text diagnostic fields (``step_intent`` / ``reason`` / ``error``)
    are potentially PHI-bearing, so they are emitted ONLY when
    ``include_free_text`` is True — i.e. when an active scrubber can de-identify
    them first (or the operator has explicitly opted out via ``SCRUB=off``).
    When ``include_free_text`` is False (scrubber unavailable under the default
    ``auto`` posture), the payload degrades to a PHI-free MINIMAL descriptor:
    the one-way ``drift_signature`` hash, status, resolver rung, and numeric
    metrics — enough to dedup/triage a break centrally without shipping raw PHI.

    The ``drift_signature`` is hashed from the RAW state_id + reason (a one-way
    SHA-256, itself PHI-free) so it is stable regardless of whether free text is
    included."""
    halt = report.halt
    status = "halt" if halt is not None else "failed"
    raw_reason = (halt.reason if halt else "") or ""
    state_id = halt.state_id if halt else ""

    steps = len(report.results)
    duration_s = round(report.total_ms / 1000.0, 3)

    payload: dict[str, Any] = {
        "org_id": org_id,
        "workflow_id": workflow_id,
        "deployment_kind": deployment_kind,
        "status": status,
        "resolver_rung": _last_failed_rung(report),
        "drift_signature": _drift_signature(state_id, raw_reason),
        "report_path": str(report_path),
        "metrics": {"steps": steps, "duration_s": duration_s},
    }

    if include_free_text:
        payload["step_intent"] = _scrub(halt.intent if halt else "")
        payload["reason"] = _scrub(raw_reason)
        if include_error:
            for step in reversed(report.results):
                if step.error:
                    payload["error"] = _scrub(step.error)
                    break
    else:
        # No scrubber to de-identify free text: omit it and flag the descriptor
        # as PHI-minimal so the server (and any human triager) knows why.
        payload["phi_minimal"] = True

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

    # Decide whether PHI-bearing free text (step_intent/reason/error) may be
    # included. It may ONLY when an active scrubber can de-identify it first, or
    # the operator explicitly opted out of scrubbing (SCRUB=off, e.g. an
    # already-de-identified fixture corpus). Under the default `auto` posture
    # with the capability MISSING, we must NOT send raw free text — degrade to a
    # PHI-free minimal descriptor. Under SCRUB=on with the capability missing,
    # text_scrubbing_enabled() raises (fail-closed) -> local-only fallback.
    try:
        scrubbing = privacy.text_scrubbing_enabled()
    except privacy.PrivacyNotAvailable:
        if allow_local_fallback:
            return {
                "emitted": False,
                "local_only": True,
                "reason": "scrubber unavailable under fail-closed policy",
            }
        raise
    include_free_text = scrubbing or privacy.scrub_mode() == "off"

    # Build + post; the fail-closed scrub inside _scrub may still raise
    # PrivacyNotAvailable if the policy flips concurrently.
    try:
        payload = _build_break_payload(
            report,
            workflow_id=workflow_id,
            deployment_kind=deployment_kind,
            org_id=org_id,
            report_path=report_path,
            include_free_text=include_free_text,
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
            include_free_text=include_free_text,
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

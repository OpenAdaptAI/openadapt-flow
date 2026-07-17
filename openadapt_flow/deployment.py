"""Deployment configuration: one YAML that wires a full openadapt-flow run.

A recorded-and-compiled bundle is portable, but *running it in production*
needs deployment-specific wiring the bundle deliberately does NOT carry: which
GUI URL to drive, which system of record to verify writes against, whether an
API actuation tier is available, whether the run is durable, and which safety
policy certifies it. Scattering those across a dozen CLI flags makes a real
deployment un-reviewable.

This module is the single, documented schema for that wiring. One
``deployment.yaml`` (see ``docs/deployment.example.yaml``) is read by
``record`` / ``compile`` / ``certify`` / ``replay`` / ``run`` / ``resume`` so
the same backend / actuation / effects / runtime / policy configuration drives
every stage.

The config only *constructs and injects* existing library objects
(``EffectVerifier`` subclasses, ``ApiActuator``); it changes no library
behavior. Every section is optional — an empty file is a valid (fully-default,
fully-local, zero-egress) deployment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from pydantic import BaseModel, Field, field_validator

# Import-light (pydantic only): the effect CONTRACT types double as the
# declarative config vocabulary, so a deployment YAML binds run parameters
# with the exact ``{param: ...}`` / ``{literal: ...}`` form bundles use.
from openadapt_flow.runtime.effects.auth import AuthRef
from openadapt_flow.runtime.effects.effect import ValueExpr


class BackendConfig(BaseModel):
    """Where and how to drive the target application's GUI.

    ``kind`` selects the concrete :class:`~openadapt_flow.backend.Backend` the
    CLI drives (built by :func:`openadapt_flow.backends.factory.build_backend`):

    - ``web`` (default) — the Playwright/Chromium browser backend. Reproduces
      the historical behavior exactly (``url`` / ``headed`` are browser fields);
      every other field here is ignored.
    - ``windows`` — the WAA (Windows Agent Arena) HTTP backend for native
      Windows desktops. Requires ``agent_url`` (the in-guest agent's base URL);
      ``agent_token`` authenticates a token-protected agent.
    - ``rdp`` — a pixel-only remote-desktop backend for the Citrix/legacy-EMR
      wedge. ``rdp_host`` drives a network RDP session (FreeRDP/aardwolf);
      ``rdp_window`` instead drives a local remote-display CLIENT WINDOW (the
      faithful Citrix analog — a Citrix Workspace / Parallels window captured
      and injected at the host OS level).

    Every non-``web`` field is optional and defaults to None, so an existing
    ``web`` deployment (or an empty config) is byte-for-byte unchanged.
    """

    #: Which backend to drive: ``web`` (default) | ``windows`` | ``rdp``.
    kind: str = "web"

    #: The GUI URL to drive (the app under automation). None => the caller's
    #: own default (e.g. ``replay`` serves the bundled MockMed demo).
    #: ``web`` only.
    url: Optional[str] = None
    #: Run the browser headed (visible). Default headless. ``web`` only.
    headed: bool = False

    # -- windows (WAA HTTP agent) --------------------------------------------
    #: Base URL of the in-guest WAA agent (e.g. ``http://localhost:5001`` over
    #: the standard SSH tunnel). REQUIRED for ``kind: windows``.
    agent_url: Optional[str] = None
    #: Optional bearer token for a token-authenticated WAA agent (sent as
    #: ``Authorization: Bearer <token>``). None => no auth header (loopback).
    agent_token: Optional[str] = None
    #: SHA-256 fingerprint of the in-guest agent's TLS certificate. When set,
    #: the Windows backend pins the exact certificate before sending screenshots
    #: or UIA/input requests; a mismatch fails at the TLS handshake.
    agent_tls_pin: Optional[str] = None

    # -- rdp (network RDP via FreeRDP/aardwolf) ------------------------------
    #: RDP host/IP for a network RDP session. REQUIRED for ``kind: rdp`` unless
    #: ``rdp_window`` is given instead (the local remote-display path).
    rdp_host: Optional[str] = None
    rdp_username: Optional[str] = None
    rdp_password: Optional[str] = None
    rdp_domain: Optional[str] = None
    rdp_port: int = 3389

    # -- rdp (local remote-display client window — the Citrix analog) --------
    #: Owner-app substring of the local client WINDOW to drive (e.g.
    #: ``"Citrix"`` / ``"Parallels"``). When set (and ``rdp_host`` is not), the
    #: ``rdp`` backend captures and injects into that on-screen window instead
    #: of opening a network RDP session (macOS host).
    rdp_window: Optional[str] = None
    #: Optional window-title substring disambiguating multiple windows of the
    #: same owner (used with ``rdp_window``).
    rdp_window_title: Optional[str] = None


class EffectsConfig(BaseModel):
    """Which system of record to verify consequential writes against.

    ``kind`` selects the concrete
    :class:`~openadapt_flow.runtime.effects.EffectVerifier`. ``none`` (default)
    wires no verifier — a bundle that declares NO effects then replays exactly
    as before, but a step that DOES declare effects HALTs (fail-safe: an
    unverifiable consequential write is never silently accepted).

    Effect-verifier kit conventions (``docs/EFFECT_KIT.md``):

    - **Secrets are references, never literals.** ``auth`` /
      ``access_token_env`` / ``sql_password_env`` name environment variables
      staged by the operator; a missing variable fails LOUD at construction.
    - **Run-parameter binding is explicit.** Every ``*_params`` /
      ``*_exprs`` mapping takes the same ``{param: name}`` / ``{literal: v}``
      :class:`~openadapt_flow.runtime.effects.ValueExpr` form bundles use (a
      bare string is a literal), and is resolved against the governed run
      parameters (``--params-file`` / ``--param``) when the verifier is
      BUILT — so one bundle + one deployment YAML ships with its verification
      bound to the record each run actually writes.
    """

    #: ``none`` | ``rest`` | ``fhir`` | ``sql`` | ``file`` | ``document-hash``.
    kind: str = "none"

    # -- rest (JSON REST system of record, e.g. MockMed /api/db) -------------
    base_url: Optional[str] = None
    records_path: str = "/api/db"
    records_key: Optional[str] = "records"
    #: Secret-isolated auth headers for the records read (bearer_env /
    #: header+value_env / basic_env). Never a credential literal.
    auth: Optional[AuthRef] = None
    #: Values for ``{placeholder}``s in ``records_path``, resolved (and
    #: URL-quoted) at construction — e.g.
    #: ``records_path: "/api/resource/Loan Application?filters=...{applicant}..."``
    #: with ``path_params: {applicant: {param: applicant}}``. Empty -> the
    #: path is used verbatim (no formatting), byte-identical to before.
    path_params: dict[str, ValueExpr] = Field(default_factory=dict)

    # -- fhir (FHIR R4 search, e.g. OpenEMR) ---------------------------------
    resource_type: str = "Observation"
    search_params: dict[str, str] = Field(default_factory=dict)
    field_paths: Optional[dict[str, str]] = None
    #: OAuth2 bearer token literal. DEPRECATED for committed configs — prefer
    #: ``access_token_env`` so the YAML never carries the secret.
    access_token: Optional[str] = None
    #: Name of the env var holding the OAuth2 bearer token (secret-isolated;
    #: wins over ``access_token``; missing var fails loud).
    access_token_env: Optional[str] = None
    verify_tls: bool = True
    #: FHIR search params whose VALUES bind to run parameters (merged over
    #: ``search_params`` after resolution) — e.g.
    #: ``search_param_exprs: {patient: {param: patient_id}}``.
    search_param_exprs: dict[str, ValueExpr] = Field(default_factory=dict)

    # -- sql (read-only SELECT against the app database) ---------------------
    #: The single read-only SELECT returning candidate records (validated by
    #: the kit's statement whitelist; a mutating query refuses to construct).
    #: Use the driver's native parameter placeholders for dynamic values.
    sql_query: Optional[str] = None
    #: DB-API parameter values for ``sql_query`` (literal or ``{param: ...}``).
    sql_query_params: dict[str, ValueExpr] = Field(default_factory=dict)
    #: Path to a SQLite database file (stdlib driver; the CI-proven path).
    sqlite_database: Optional[str] = None
    #: Alternatively: an importable DB-API driver module name (``pymysql``,
    #: ``mariadb``, ``psycopg``) ...
    sql_driver: Optional[str] = None
    #: ... plus its non-secret ``connect(**kwargs)`` arguments ...
    sql_connect_args: dict[str, Any] = Field(default_factory=dict)
    #: ... and the env var holding the database password (injected as the
    #: ``password`` connect kwarg; secret-isolated, fails loud when missing).
    sql_password_env: Optional[str] = None

    # -- file (file / SFTP arrival; local directory via YAML) ----------------
    #: (uses the shared ``root``) filename glob for candidate records.
    file_pattern: str = "*"
    #: Minimum byte size for a conforming arrival (size > 0 by default).
    file_min_size: int = 1
    #: Freshness window in seconds (``fresh`` flag); None -> always fresh.
    file_mtime_window_s: Optional[float] = None
    #: Optional regex probed against each candidate's leading content.
    file_content_probe: Optional[str] = None

    # -- document-hash (filesystem document store) ---------------------------
    root: Optional[str] = None
    glob: str = "*"

    # -- shared --------------------------------------------------------------
    timeout_s: float = 5.0
    poll_interval_s: float = 0.2

    @field_validator(
        "path_params", "search_param_exprs", "sql_query_params", mode="before"
    )
    @classmethod
    def _coerce_value_exprs(cls, v: Any) -> Any:
        """A bare YAML string is a literal (the same coercion ``Effect`` uses)."""
        if isinstance(v, dict):
            return {
                k: ({"literal": val} if isinstance(val, str) else val)
                for k, val in v.items()
            }
        return v


class ActuationConfig(BaseModel):
    """The API/tool actuation tier (top of the capability ladder).

    When enabled, a step carrying an ``ir.ApiBinding`` has its write PERFORMED
    via the API (deterministic, $0, no GUI) and confirmed by the effect
    verifier. Disabled (default) => every step actuates through the GUI ladder.
    """

    #: Wire an :class:`~openadapt_flow.runtime.actuators.ApiActuator`.
    api: bool = False
    #: API base URL for relative ``ApiBinding.url_template``s.
    base_url: str = ""
    timeout_s: float = 5.0


class RuntimeSection(BaseModel):
    """Runtime posture: durability and model-egress opt-in."""

    #: Tier-3 durable runtime: checkpoint each verified step, durably pause on
    #: halt, resumable via ``resume``. Off by default.
    durable: bool = False
    #: EGRESS OPT-IN (PHI audit REM-3): permit wiring an off-box model
    #: grounder / identity-VLM / state-verifier. Off by default => fully local,
    #: zero outbound calls.
    allow_model_grounding: bool = False


class PolicySection(BaseModel):
    """The safety policy that certifies a bundle for this deployment."""

    #: Policy YAML path, or a shipped built-in name (permissive, clinical-write).
    policy: Optional[str] = None


class DeploymentConfig(BaseModel):
    """The whole-deployment configuration (one ``deployment.yaml``)."""

    #: Human-readable deployment name (audit / logs only).
    name: str = "deployment"
    backend: BackendConfig = Field(default_factory=BackendConfig)
    actuation: ActuationConfig = Field(default_factory=ActuationConfig)
    effects: EffectsConfig = Field(default_factory=EffectsConfig)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    policy: PolicySection = Field(default_factory=PolicySection)


def load_deployment(source: str | Path) -> DeploymentConfig:
    """Load a :class:`DeploymentConfig` from a YAML file.

    Raises:
        FileNotFoundError: If ``source`` is not an existing file.
        ValueError: If the YAML is malformed or violates the schema.
    """
    import yaml

    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"deployment config {source!r} is not an existing file")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:  # pragma: no cover - passthrough
        raise ValueError(f"could not parse deployment YAML {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(
            f"deployment config {path} must be a YAML mapping, got "
            f"{type(data).__name__}"
        )
    try:
        return DeploymentConfig.model_validate(data)
    except Exception as e:
        raise ValueError(f"invalid deployment config {path}: {e}") from e


def _resolve_config_exprs(
    section: str,
    exprs: Mapping[str, ValueExpr],
    params: Optional[Mapping[str, str]],
) -> dict[str, str]:
    """Resolve a config's ``ValueExpr`` mapping against the run's params.

    Fail-safe is fail-LOUD here: a ``{param: ...}`` reference that the run did
    not supply raises (the verifier would otherwise silently probe the wrong
    record), and the message names the missing parameter.
    """
    resolved: dict[str, str] = {}
    for key, expr in exprs.items():
        value = expr.resolve(dict(params or {}))
        if value is None:
            raise ValueError(
                f"effects.{section}[{key!r}] references run parameter "
                f"{expr.param!r}, which was not supplied — pass it via "
                "--param / --params-file (a verifier is never wired against "
                "an unresolved selector)"
            )
        resolved[key] = value
    return resolved


def _require_env(name: str, what: str) -> str:
    """Read a secret env var named by the config; fail loud when absent."""
    import os

    value = os.environ.get(name, "")
    if not value:
        raise ValueError(
            f"{what} references environment variable {name!r}, which is not "
            "set (or empty) — refusing to wire an unauthenticated/broken "
            "effect verifier"
        )
    return value


def build_effect_verifier(
    cfg: EffectsConfig, params: Optional[Mapping[str, str]] = None
) -> Optional[Any]:
    """Construct the configured ``EffectVerifier`` (or None for ``kind: none``).

    Args:
        cfg: The deployment's ``effects`` section.
        params: The governed run parameters (``--params-file`` / ``--param``
            values). Configs that bind ``{param: ...}`` references
            (``path_params`` / ``search_param_exprs`` / ``sql_query_params``)
            are resolved against these AT CONSTRUCTION, so the verifier probes
            the record THIS run writes. A config with no references ignores
            ``params`` entirely (fully back-compatible).

    Raises:
        ValueError: on an unknown ``kind``, a missing required field, an
            unresolved ``{param: ...}`` reference, or a missing secret env var
            (fail loud rather than wire a broken verifier).
    """
    kind = (cfg.kind or "none").strip().lower()
    if kind in ("none", ""):
        return None

    if kind == "rest":
        if not cfg.base_url:
            raise ValueError("effects.kind 'rest' requires effects.base_url")
        from openadapt_flow.runtime.effects import RestRecordVerifier

        headers = cfg.auth.resolve_headers() if cfg.auth is not None else None
        records_path = cfg.records_path
        if cfg.path_params:
            from urllib.parse import quote

            values = {
                key: quote(value, safe="")
                for key, value in _resolve_config_exprs(
                    "path_params", cfg.path_params, params
                ).items()
            }
            try:
                records_path = records_path.format(**values)
            except (KeyError, IndexError, ValueError) as e:
                raise ValueError(
                    f"effects.records_path template does not match "
                    f"effects.path_params: {e!r}"
                ) from e
        return RestRecordVerifier(
            cfg.base_url,
            records_path=records_path,
            records_key=cfg.records_key,
            headers=headers,
            timeout_s=cfg.timeout_s,
            poll_interval_s=cfg.poll_interval_s,
        )

    if kind == "fhir":
        if not cfg.base_url:
            raise ValueError("effects.kind 'fhir' requires effects.base_url")
        from openadapt_flow.runtime.effects import FhirEffectVerifier

        access_token = cfg.access_token
        if cfg.access_token_env:
            access_token = _require_env(
                cfg.access_token_env, "effects.access_token_env"
            )
        search_params = dict(cfg.search_params)
        if cfg.search_param_exprs:
            search_params.update(
                _resolve_config_exprs(
                    "search_param_exprs", cfg.search_param_exprs, params
                )
            )
        return FhirEffectVerifier(
            cfg.base_url,
            resource_type=cfg.resource_type,
            search_params=search_params or None,
            field_paths=cfg.field_paths,
            access_token=access_token,
            verify_tls=cfg.verify_tls,
            timeout_s=cfg.timeout_s,
            poll_interval_s=cfg.poll_interval_s,
        )

    if kind == "sql":
        if not cfg.sql_query:
            raise ValueError("effects.kind 'sql' requires effects.sql_query")
        from openadapt_flow.runtime.effects import SqlRecordVerifier

        connect: Callable[[], Any]
        if cfg.sqlite_database:
            import sqlite3

            database = cfg.sqlite_database
            connect = lambda: sqlite3.connect(database)  # noqa: E731
        elif cfg.sql_driver:
            import importlib

            module = importlib.import_module(cfg.sql_driver)
            connect_args = dict(cfg.sql_connect_args)
            if cfg.sql_password_env:
                connect_args["password"] = _require_env(
                    cfg.sql_password_env, "effects.sql_password_env"
                )
            connect = lambda: module.connect(**connect_args)  # noqa: E731
        else:
            raise ValueError(
                "effects.kind 'sql' requires effects.sqlite_database or "
                "effects.sql_driver (+ sql_connect_args)"
            )
        return SqlRecordVerifier(
            connect,
            cfg.sql_query,
            query_params=(
                _resolve_config_exprs("sql_query_params", cfg.sql_query_params, params)
                or None
            ),
            timeout_s=cfg.timeout_s,
            poll_interval_s=cfg.poll_interval_s,
        )

    if kind == "file":
        if not cfg.root:
            raise ValueError("effects.kind 'file' requires effects.root")
        from openadapt_flow.runtime.effects import FileArrivalVerifier

        return FileArrivalVerifier(
            cfg.root,
            pattern=cfg.file_pattern,
            min_size=cfg.file_min_size,
            mtime_window_s=cfg.file_mtime_window_s,
            content_probe=cfg.file_content_probe,
        )

    if kind in ("document-hash", "document_hash", "doc-hash"):
        if not cfg.root:
            raise ValueError("effects.kind 'document-hash' requires effects.root")
        from openadapt_flow.runtime.effects import DocumentHashVerifier

        return DocumentHashVerifier(cfg.root, glob=cfg.glob)

    raise ValueError(
        f"unknown effects.kind {cfg.kind!r} "
        "(expected: none | rest | fhir | sql | file | document-hash)"
    )


def build_api_actuator(cfg: ActuationConfig) -> Optional[Any]:
    """Construct the configured ``ApiActuator`` (or None when ``api`` is off)."""
    if not cfg.api:
        return None
    from openadapt_flow.runtime.actuators import ApiActuator

    return ApiActuator(cfg.base_url, timeout_s=cfg.timeout_s)

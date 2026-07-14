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
from typing import Any, Optional

from pydantic import BaseModel, Field


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
    #: Placeholder for the agent's pinned TLS certificate fingerprint. RESERVED
    #: for the in-flight TLS-pin work (openadapt-flow#112); NOT wired into the
    #: backend on this branch (main's WindowsBackend has no TLS-pin parameter),
    #: so setting it today has no effect. Documented follow-up.
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
    """

    #: ``none`` | ``rest`` | ``fhir`` | ``document-hash``.
    kind: str = "none"

    # -- rest (JSON REST system of record, e.g. MockMed /api/db) -------------
    base_url: Optional[str] = None
    records_path: str = "/api/db"
    records_key: Optional[str] = "records"

    # -- fhir (FHIR R4 search, e.g. OpenEMR) ---------------------------------
    resource_type: str = "Observation"
    search_params: dict[str, str] = Field(default_factory=dict)
    field_paths: Optional[dict[str, str]] = None
    access_token: Optional[str] = None
    verify_tls: bool = True

    # -- document-hash (filesystem document store) ---------------------------
    root: Optional[str] = None
    glob: str = "*"

    # -- shared --------------------------------------------------------------
    timeout_s: float = 5.0
    poll_interval_s: float = 0.2


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


def build_effect_verifier(cfg: EffectsConfig) -> Optional[Any]:
    """Construct the configured ``EffectVerifier`` (or None for ``kind: none``).

    Raises:
        ValueError: on an unknown ``kind`` or a missing required field for the
            selected verifier (fail loud rather than wire a broken verifier).
    """
    kind = (cfg.kind or "none").strip().lower()
    if kind in ("none", ""):
        return None

    if kind == "rest":
        if not cfg.base_url:
            raise ValueError("effects.kind 'rest' requires effects.base_url")
        from openadapt_flow.runtime.effects import RestRecordVerifier

        return RestRecordVerifier(
            cfg.base_url,
            records_path=cfg.records_path,
            records_key=cfg.records_key,
            timeout_s=cfg.timeout_s,
            poll_interval_s=cfg.poll_interval_s,
        )

    if kind == "fhir":
        if not cfg.base_url:
            raise ValueError("effects.kind 'fhir' requires effects.base_url")
        from openadapt_flow.runtime.effects import FhirEffectVerifier

        return FhirEffectVerifier(
            cfg.base_url,
            resource_type=cfg.resource_type,
            search_params=cfg.search_params or None,
            field_paths=cfg.field_paths,
            access_token=cfg.access_token,
            verify_tls=cfg.verify_tls,
            timeout_s=cfg.timeout_s,
            poll_interval_s=cfg.poll_interval_s,
        )

    if kind in ("document-hash", "document_hash", "doc-hash"):
        if not cfg.root:
            raise ValueError("effects.kind 'document-hash' requires effects.root")
        from openadapt_flow.runtime.effects import DocumentHashVerifier

        return DocumentHashVerifier(cfg.root, glob=cfg.glob)

    raise ValueError(
        f"unknown effects.kind {cfg.kind!r} "
        "(expected: none | rest | fhir | document-hash)"
    )


def build_api_actuator(cfg: ActuationConfig) -> Optional[Any]:
    """Construct the configured ``ApiActuator`` (or None when ``api`` is off)."""
    if not cfg.api:
        return None
    from openadapt_flow.runtime.actuators import ApiActuator

    return ApiActuator(cfg.base_url, timeout_s=cfg.timeout_s)

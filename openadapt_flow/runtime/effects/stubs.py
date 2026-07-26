"""Stubbed adapter interfaces: declared, documented, deliberately NOT built.

Each class here is a named seat at the adapter table (the matrix in
``docs/EFFECT_KIT.md``) whose implementation is intentionally deferred. A stub
FAILS LOUD at construction -- ``effects.kind: audit-feed`` in a deployment
raises immediately with guidance, so a planned capability can never be
mistaken for a working one (no silently-passing placeholder verifier, ever).

Why stub instead of omit: the interface each future adapter must satisfy is
part of the platform contract NOW (constructor surface, substrate name,
tier), so a customer planning against it -- or implementing it as a plugin via
:data:`~openadapt_flow.runtime.effects.adapter.ENTRY_POINT_GROUP` -- targets a
stable shape.

Current status of the neighbors these stubs point to:

- FHIR is NOT a stub: :class:`~openadapt_flow.runtime.effects.fhir.FhirEffectVerifier`
  is a supported adapter (resource/entity binding via ``search_param_exprs``).
- SFTP arrival is programmatic-only today: inject a paramiko-compatible
  ``transport`` into
  :class:`~openadapt_flow.runtime.effects.file_arrival.FileArrivalVerifier`.
  The :class:`SftpArrivalVerifier` stub reserves the DECLARATIVE
  (YAML-wireable, host/key-config) surface.
"""

from __future__ import annotations

from typing import Any, NoReturn, Optional

from openadapt_flow.verification import VerificationTier


class StubAdapterError(NotImplementedError):
    """A planned adapter was configured but is not implemented yet.

    Raised at CONSTRUCTION (never at verify time): a deployment naming a
    stubbed kind refuses to start, rather than starting and halting every
    consequential step with a misleading verdict.
    """


def _refuse(kind: str, guidance: str) -> NoReturn:
    raise StubAdapterError(
        f"effects.kind {kind!r} is a PLANNED adapter, not yet implemented. "
        f"{guidance} See the adapter matrix in docs/EFFECT_KIT.md; to ship "
        "your own implementation now, register a plugin under the "
        "'openadapt_flow.effect_verifiers' entry-point group."
    )


class SftpArrivalVerifier:
    """PLANNED: declarative SFTP file-arrival verification.

    Intended surface: ``SftpArrivalVerifier(host, *, port=22, username,
    key_env or password_env, root, pattern, mtime_window_s, content_probe)``
    -- the declarative twin of ``FileArrivalVerifier`` with the connection
    owned by the adapter (secret-isolated credentials, test_connection =
    listing the root). Until then, SFTP is supported PROGRAMMATICALLY by
    injecting a paramiko-compatible ``transport`` into
    :class:`~openadapt_flow.runtime.effects.file_arrival.FileArrivalVerifier`.
    """

    substrate = "sftp"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _refuse(
            "sftp",
            "Today: inject a paramiko-compatible transport into "
            "FileArrivalVerifier (programmatic; contract-proven against a "
            "fake transport in CI).",
        )


class AuditFeedVerifier:
    """PLANNED: verification against the system of record's AUDIT/EVENT feed.

    Intended surface: consume an append-only audit trail (an event log, a
    webhook capture, a CDC stream snapshot) and judge "exactly one
    creation event for THIS entity within the settlement window" -- the
    strongest duplicate-write oracle for systems whose read API hides
    versioning. Constructor shape: ``AuditFeedVerifier(source, *,
    event_selector, entity_binding, window_s, auth)``.
    """

    substrate = "audit-feed"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _refuse(
            "audit-feed",
            "Today: point the REST/GraphQL/SQL adapter at an audit TABLE or "
            "endpoint if the system of record exposes one (the shared judge "
            "already handles event-shaped records).",
        )


class ReadOnlySessionVerifier:
    """PLANNED: same-application read-back through a SEPARATELY AUTHENTICATED
    read-only session.

    Intended surface: a second, independently credentialed session (different
    user, read-only role) re-reads the written record through the
    application's own front door -- stronger than same-session screen
    read-back (tier 2, ``independent-session``) but weaker than an
    independent system (tier 1), because it still trusts the application's
    read path. Constructor shape: ``ReadOnlySessionVerifier(backend_factory,
    *, credentials_env, renavigation, readback)``.
    """

    substrate = "readonly-session"
    verification_tier = VerificationTier.INDEPENDENT_SESSION

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _refuse(
            "readonly-session",
            "Today: the onscreen adapter's different-path read-back "
            "(tier 3) is the closest supported oracle; an independent "
            "REST/SQL/FHIR read (tier 1) is stronger where any API exists.",
        )


#: The stubbed declarative kinds ``build_effect_verifier`` recognizes; mapping
#: kind -> stub class (constructing any of them raises StubAdapterError).
STUB_KINDS: dict[str, type] = {
    "sftp": SftpArrivalVerifier,
    "audit-feed": AuditFeedVerifier,
    "audit_feed": AuditFeedVerifier,
    "readonly-session": ReadOnlySessionVerifier,
    "readonly_session": ReadOnlySessionVerifier,
}


def construct_stub(kind: str) -> Optional[Any]:
    """Instantiate the stub for ``kind`` (raises StubAdapterError) or return
    ``None`` when ``kind`` is not a stubbed kind."""
    cls = STUB_KINDS.get(kind)
    if cls is None:
        return None
    return cls()

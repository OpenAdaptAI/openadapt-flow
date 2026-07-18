"""The thin OPERATOR CONSOLE: a localhost-only web UI over the artifacts the
engine already writes.

The console is a read-only PROJECTION of on-disk state -- compiled workflow
bundles (``workflow.json`` + manifest + template crops), run directories
(``report.json`` + step screenshots + durable pause/approval records), and the
versioned skill library (``skills.json``) -- rendered for the operator who
governs production workflows. It invents NO new engine semantics:

- Every number it shows is computed by the SAME callables the CLI uses
  (``policy.evaluate_policy`` / ``policy.lint_workflow`` / the identity- and
  effect-coverage helpers in ``openadapt_flow.policy``).
- Every governance ACTION it offers is one of the existing verbs (``teach`` /
  ``approve`` / ``resume`` / ``certify`` and the skill library's
  ``promote`` / ``quarantine``), either shelled out to the ``openadapt-flow``
  CLI or invoked through the same library entry point. A verb the console
  cannot execute safely (e.g. ``teach``, which needs a fix demonstration) is
  rendered as the exact CLI command for the operator to copy -- never faked.
- The server binds 127.0.0.1 ONLY and starts READ-ONLY: mutating endpoints
  refuse with the rendered command unless the operator opted in with
  ``--allow-actions``, and even then every mutation shows exactly what it will
  run before a confirm.

Serve it with ``openadapt-flow console`` (requires the ``console`` extra:
``pip install 'openadapt-flow[console]'``).
"""

from __future__ import annotations

__all__ = ["create_app", "serve"]


def __getattr__(name: str):  # PEP 562: keep fastapi/uvicorn imports lazy
    if name == "create_app":
        from openadapt_flow.console.app import create_app

        return create_app
    if name == "serve":
        from openadapt_flow.console.server import serve

        return serve
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

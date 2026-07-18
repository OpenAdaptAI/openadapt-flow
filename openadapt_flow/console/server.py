"""Serve the operator console on loopback ONLY.

The bind address is hardcoded to ``127.0.0.1`` -- the console has no auth
layer, so it must never listen on a routable interface. Exposing it beyond the
operator's machine is deliberately not supported (put it behind your own
authenticated tunnel if you must).
"""

from __future__ import annotations

from pathlib import Path

#: The only address the console ever binds. Not configurable.
LOOPBACK_HOST = "127.0.0.1"

DEFAULT_PORT = 7863


def serve(
    bundles_root: Path | str,
    runs_root: Path | str,
    skills_root: Path | str | None = None,
    *,
    allow_actions: bool = False,
    port: int = DEFAULT_PORT,
) -> None:
    """Build the app and serve it on ``http://127.0.0.1:<port>`` (blocking)."""
    import uvicorn

    from openadapt_flow.console.app import create_app

    app = create_app(bundles_root, runs_root, skills_root, allow_actions=allow_actions)
    uvicorn.run(app, host=LOOPBACK_HOST, port=port, log_level="info")

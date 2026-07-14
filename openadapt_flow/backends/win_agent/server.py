"""In-guest Windows agent server (WAA-contract HTTP shim, session 1).

Runs INSIDE the Windows VM's *interactive* desktop session and exposes exactly
the endpoints ``openadapt_flow.backends.windows_backend.WindowsBackend`` calls,
matching the Windows Agent Arena Flask contract (the WAADirect pattern):

    GET  /screenshot        -> raw PNG bytes of the desktop (Content-Type
                               image/png; NOT base64 JSON)
    POST /execute_windows   -> exec() of BARE Python with pyautogui /
                               uiautomation importable. Body is
                               ``{"command": "<python statements>"}`` -- NOT
                               wrapped in ``python -c "..."``. The response
                               echoes captured stdout so a UIA read (the
                               ``<<OAFLOW_STRUCTURED>>...`` sentinel the
                               backend emits) can travel back.
    GET  /health            -> ``{"status": "ok", ...}`` liveness + which
                               desktop session the process is attached to.

Why a separate in-session server at all (the session-0 problem)
---------------------------------------------------------------
``prlctl exec`` (and any Windows service) runs as ``NT AUTHORITY\\SYSTEM`` in
session 0, which is isolated from the logged-on user's desktop. An mss/BitBlt
screenshot there captures a blank/non-existent desktop and pyautogui SendInput
goes nowhere -- the automation silently drives the wrong desktop. This server
MUST therefore run in the interactive console session (session 1). The
canonical way to start it from SYSTEM is the ``session1_launch.py`` launcher
(WTSQueryUserToken -> CreateProcessAsUserW with ``lpDesktop=winsta0\\default``);
for an unattended VM the ``run_agent.bat`` + logon scheduled-task recipe in this
package's ``README.md`` starts it in-session at user logon.

Hardening (vs the original ``scripts/desktop/waa_shim.py``)
-----------------------------------------------------------
* **Loopback by default.** ``/execute_windows`` is arbitrary remote code
  execution by contract, so the default bind is ``127.0.0.1`` -- reachable only
  from inside the guest (e.g. an in-guest SSH/port-forward). Exposing it on the
  guest's LAN interface (``--host 0.0.0.0``, needed for a host->guest
  ``WindowsBackend``) is an explicit opt-in.
* **Optional bearer token.** The PHI at-rest audit flagged this shim as
  unauthenticated. When a token is configured (``--token`` or the
  ``OAFLOW_AGENT_TOKEN`` env var) every ``/screenshot`` and ``/execute_windows``
  request must carry ``Authorization: Bearer <token>`` or is rejected 401. The
  comparison is constant-time. ``/health`` stays unauthenticated (liveness only,
  no desktop bytes, no exec).

Self-contained by construction
------------------------------
Only the Python standard library is imported at module load (no Flask), so the
guest needs no third-party web framework and CI on macOS/Linux imports this
module freely. The heavy, Windows-only pieces (mss/Pillow for the screenshot,
pyautogui/uiautomation used by the exec'd commands) import LAZILY inside the
request handlers, and the desktop grabber is injectable so tests exercise the
full HTTP roundtrip with a fake frame.
"""

from __future__ import annotations

import argparse
import hmac
import io
import json
import os
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

# PNG magic used to validate/return frames.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Env var the token is read from when ``--token`` is not passed (keeps the
# secret off the process command line / argv where feasible).
TOKEN_ENV_VAR = "OAFLOW_AGENT_TOKEN"

GrabFn = Callable[[], bytes]


@dataclass
class AgentConfig:
    """Runtime configuration for the in-guest agent server.

    Args:
        host: Bind address. Defaults to loopback (``127.0.0.1``) -- the
            arbitrary-exec endpoint is not exposed off-host unless this is set
            to ``0.0.0.0`` (or the guest IP) explicitly.
        port: TCP port (matches the WAA default the SSH tunnel expects).
        token: Optional bearer token. When set, ``/screenshot`` and
            ``/execute_windows`` require ``Authorization: Bearer <token>``.
            When None the server is unauthenticated (loopback-only is then the
            only safeguard).
    """

    host: str = "127.0.0.1"
    port: int = 5000
    token: Optional[str] = None

    def authed(self) -> bool:
        """True when a bearer token is required."""
        return bool(self.token)


def _grab_desktop_png() -> bytes:
    """Capture the full virtual desktop as PNG bytes (mss + Pillow).

    Imported lazily and only on the screenshot path so the module loads on any
    OS. ``monitors[0]`` is the union of all monitors, so multi-monitor / DPI
    layouts are captured whole with absolute coordinates.
    """
    import mss  # noqa: PLC0415 - Windows-only, imported lazily by design
    from PIL import Image  # noqa: PLC0415

    with mss.mss() as sct:
        mon = sct.monitors[0]
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _active_console_session() -> int:
    """Which console session this process is attached to (-1 if unknown)."""
    try:
        import ctypes  # noqa: PLC0415

        return int(ctypes.windll.kernel32.WTSGetActiveConsoleSessionId())
    except Exception:  # noqa: BLE001 - non-Windows / probe failure
        return -1


def make_handler_class(
    config: AgentConfig, grab_fn: GrabFn = _grab_desktop_png
) -> type[BaseHTTPRequestHandler]:
    """Build the request-handler class bound to ``config`` and ``grab_fn``.

    ``grab_fn`` is injectable so tests drive the real HTTP roundtrip with a
    deterministic fake frame (no mss / no live desktop).
    """

    class AgentHandler(BaseHTTPRequestHandler):
        server_version = "OAFlowWinAgent/1.0"

        def log_message(self, *args: object) -> None:  # noqa: D401 - silence
            """Suppress the default stderr access log (noisy in-guest)."""

        # -- helpers ---------------------------------------------------------

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _authorized(self) -> bool:
            """Constant-time bearer-token check (True when auth disabled)."""
            if not config.authed():
                return True
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not header.startswith(prefix):
                return False
            presented = header[len(prefix) :].strip()
            return hmac.compare_digest(presented, config.token or "")

        def _reject_unauthorized(self) -> None:
            self._send_json(401, {"status": "error", "error": "unauthorized"})

        # -- routes ----------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path == "/health":
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "agent": "openadapt_flow.win_agent",
                        "active_console_session": _active_console_session(),
                        "auth_required": config.authed(),
                    },
                )
                return
            if self.path == "/screenshot":
                if not self._authorized():
                    self._reject_unauthorized()
                    return
                try:
                    png = grab_fn()
                except Exception as e:  # noqa: BLE001 - report, never crash loop
                    self._send_json(
                        500,
                        {
                            "status": "error",
                            "error": str(e),
                            "trace": traceback.format_exc(),
                        },
                    )
                    return
                if not png.startswith(_PNG_SIGNATURE):
                    self._send_json(
                        500, {"status": "error", "error": "grabber did not return PNG"}
                    )
                    return
                self._send(200, png, "image/png")
                return
            self._send_json(404, {"status": "error", "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            if self.path != "/execute_windows":
                self._send_json(404, {"status": "error", "error": "not found"})
                return
            if not self._authorized():
                self._reject_unauthorized()
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw or b"{}")
            except Exception:  # noqa: BLE001
                self._send_json(400, {"status": "error", "error": "invalid JSON body"})
                return
            command = data.get("command")
            if not isinstance(command, str):
                self._send_json(
                    400, {"status": "error", "error": "command must be a string"}
                )
                return
            self._exec_command(command)

        def _exec_command(self, command: str) -> None:
            """exec() bare Python; return 200 + captured stdout, 500 on error.

            The command runs with a fresh module-like namespace. Its stdout is
            captured and echoed in the response body so a UIA read snippet's
            ``<<OAFLOW_STRUCTURED>>...<<END_OAFLOW_STRUCTURED>>`` sentinel
            reaches the backend. A raised exception becomes HTTP 500 with the
            traceback, so a wrong-write surfaces as an ERROR rather than a
            silent no-op (the runtime halts on a non-200).
            """
            import contextlib  # noqa: PLC0415

            # pyautogui's fail-safe raises when the cursor reaches a screen
            # corner; the compiled replay legitimately drives the cursor
            # anywhere, so disable it for this process (best-effort).
            try:
                import pyautogui  # noqa: PLC0415

                pyautogui.FAILSAFE = False
            except Exception:  # noqa: BLE001 - not always present at exec time
                pass

            scope: dict = {"__name__": "__oaflow_agent_exec__"}
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    exec(command, scope)  # noqa: S102 - the WAA contract IS remote exec
            except Exception as e:  # noqa: BLE001
                self._send_json(
                    500,
                    {
                        "status": "error",
                        "error": str(e),
                        "trace": traceback.format_exc(),
                        "output": out.getvalue(),
                    },
                )
                return
            self._send_json(200, {"status": "ok", "output": out.getvalue()})

    return AgentHandler


def create_server(
    config: Optional[AgentConfig] = None,
    *,
    grab_fn: GrabFn = _grab_desktop_png,
) -> ThreadingHTTPServer:
    """Build (but do not start) the threaded agent HTTP server.

    Args:
        config: Bind/auth configuration (defaults to loopback, no token).
        grab_fn: Desktop-capture callable returning PNG bytes (injectable for
            tests).

    Returns:
        A ``ThreadingHTTPServer`` bound to ``config.host:config.port``. Call
        ``serve_forever()`` (usually on a daemon thread) to run it, or use it
        as a context manager.
    """
    config = config or AgentConfig()
    handler = make_handler_class(config, grab_fn)
    return ThreadingHTTPServer((config.host, config.port), handler)


def main(argv: Optional[list[str]] = None) -> None:
    """CLI entry point: run the agent server until interrupted."""
    parser = argparse.ArgumentParser(
        description="OpenAdapt-flow in-guest Windows agent"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default loopback; use 0.0.0.0 to expose to the host)",
    )
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--token",
        default=os.environ.get(TOKEN_ENV_VAR),
        help=(
            "optional bearer token required on /screenshot and "
            f"/execute_windows (falls back to ${TOKEN_ENV_VAR})"
        ),
    )
    args = parser.parse_args(argv)
    config = AgentConfig(host=args.host, port=args.port, token=args.token)
    server = create_server(config)
    print(
        f"[win-agent] listening on http://{config.host}:{config.port} "
        f"(auth={'on' if config.authed() else 'OFF'}, "
        f"session={_active_console_session()})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()

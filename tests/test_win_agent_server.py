"""Roundtrip tests for the in-guest win_agent server (no live VM/desktop).

The stdlib server is started on a loopback ephemeral port and exercised with
real ``requests`` calls, so the full HTTP contract WindowsBackend depends on is
proven end to end. The desktop grabber is injected (a fake PNG) so no mss / no
real desktop is needed — the suite runs on macOS/Linux CI.
"""

from __future__ import annotations

import struct
import threading
from collections.abc import Iterator

import pytest
import requests

from openadapt_flow.backends.win_agent import AgentConfig, create_server

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _fake_png() -> bytes:
    """Minimal valid-enough PNG: signature + IHDR with a 4x2 size."""
    ihdr = struct.pack(">II", 4, 2)
    return _PNG_SIGNATURE + b"\x00\x00\x00\x0dIHDR" + ihdr + b"\x00" * 8


class RunningAgent:
    """A started agent server plus its base URL (context-managed)."""

    def __init__(self, config: AgentConfig, grab_fn=_fake_png) -> None:
        self.server = create_server(config, grab_fn=grab_fn)
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def agent() -> Iterator[RunningAgent]:
    a = RunningAgent(AgentConfig(host="127.0.0.1", port=0))
    yield a
    a.close()


@pytest.fixture()
def authed_agent() -> Iterator[RunningAgent]:
    a = RunningAgent(AgentConfig(host="127.0.0.1", port=0, token="s3cret"))
    yield a
    a.close()


# -- health -------------------------------------------------------------------


def test_health_ok_and_unauthenticated(agent: RunningAgent) -> None:
    r = requests.get(f"{agent.url}/health", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["auth_required"] is False


def test_health_open_even_when_token_set(authed_agent: RunningAgent) -> None:
    # Liveness must not require the token (no desktop bytes, no exec).
    r = requests.get(f"{authed_agent.url}/health", timeout=5)
    assert r.status_code == 200
    assert r.json()["auth_required"] is True


# -- screenshot ---------------------------------------------------------------


def test_screenshot_returns_raw_png(agent: RunningAgent) -> None:
    r = requests.get(f"{agent.url}/screenshot", timeout=5)
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "image/png"
    assert r.content.startswith(_PNG_SIGNATURE)


def test_screenshot_500_when_grabber_not_png() -> None:
    a = RunningAgent(AgentConfig(port=0), grab_fn=lambda: b"not a png")
    try:
        r = requests.get(f"{a.url}/screenshot", timeout=5)
        assert r.status_code == 500
        assert r.json()["status"] == "error"
    finally:
        a.close()


def test_screenshot_500_when_grabber_raises() -> None:
    def boom() -> bytes:
        raise RuntimeError("no desktop")

    a = RunningAgent(AgentConfig(port=0), grab_fn=boom)
    try:
        r = requests.get(f"{a.url}/screenshot", timeout=5)
        assert r.status_code == 500
        assert "no desktop" in r.json()["error"]
    finally:
        a.close()


# -- execute_windows ----------------------------------------------------------


def test_execute_windows_runs_bare_python_and_echoes_stdout(
    agent: RunningAgent,
) -> None:
    r = requests.post(
        f"{agent.url}/execute_windows",
        json={"command": "print('<<OAFLOW_STRUCTURED>>42<<END_OAFLOW_STRUCTURED>>')"},
        timeout=5,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "<<OAFLOW_STRUCTURED>>42<<END_OAFLOW_STRUCTURED>>" in body["output"]


def test_execute_windows_500_on_exception_with_traceback(agent: RunningAgent) -> None:
    # A failing command must surface as an ERROR (non-200), never a silent no-op.
    r = requests.post(
        f"{agent.url}/execute_windows",
        json={"command": "raise ValueError('boom')"},
        timeout=5,
    )
    assert r.status_code == 500
    body = r.json()
    assert body["status"] == "error"
    assert "boom" in body["error"]
    assert "Traceback" in body["trace"]


def test_execute_windows_400_on_non_string_command(agent: RunningAgent) -> None:
    r = requests.post(f"{agent.url}/execute_windows", json={"command": 123}, timeout=5)
    assert r.status_code == 400


def test_execute_windows_400_on_bad_json(agent: RunningAgent) -> None:
    r = requests.post(
        f"{agent.url}/execute_windows",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        timeout=5,
    )
    assert r.status_code == 400


# -- auth ---------------------------------------------------------------------


def test_execute_requires_token_when_configured(authed_agent: RunningAgent) -> None:
    # No header -> 401.
    r = requests.post(
        f"{authed_agent.url}/execute_windows",
        json={"command": "print('x')"},
        timeout=5,
    )
    assert r.status_code == 401
    # Wrong token -> 401.
    r = requests.post(
        f"{authed_agent.url}/execute_windows",
        json={"command": "print('x')"},
        headers={"Authorization": "Bearer wrong"},
        timeout=5,
    )
    assert r.status_code == 401
    # Correct token -> 200.
    r = requests.post(
        f"{authed_agent.url}/execute_windows",
        json={"command": "print('x')"},
        headers={"Authorization": "Bearer s3cret"},
        timeout=5,
    )
    assert r.status_code == 200


def test_screenshot_requires_token_when_configured(authed_agent: RunningAgent) -> None:
    r = requests.get(f"{authed_agent.url}/screenshot", timeout=5)
    assert r.status_code == 401
    r = requests.get(
        f"{authed_agent.url}/screenshot",
        headers={"Authorization": "Bearer s3cret"},
        timeout=5,
    )
    assert r.status_code == 200


def test_windows_backend_talks_to_authed_agent(authed_agent: RunningAgent) -> None:
    # The real WindowsBackend, with the matching token, drives the agent.
    from openadapt_flow.backends import WindowsBackend

    backend = WindowsBackend(authed_agent.url, auth_token="s3cret")
    assert backend.probe() is True
    # Without the token, the action path fails loudly (never a silent no-op).
    unauth = WindowsBackend(authed_agent.url)
    assert unauth.probe() is False
    with pytest.raises(RuntimeError):
        unauth.click(1, 1)

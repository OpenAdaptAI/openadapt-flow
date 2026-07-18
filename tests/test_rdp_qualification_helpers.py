"""Unit coverage for the live RDP qualification's refusal/readiness helpers."""

from __future__ import annotations

import io
import subprocess

import pytest
from PIL import Image, ImageDraw

from tests.e2e import test_parallels_rdp_e2e as rdp


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _png(*, background: int, taskbar: bool = False, marker: bool = False) -> bytes:
    image = Image.new("RGB", (320, 200), (background,) * 3)
    draw = ImageDraw.Draw(image)
    if taskbar:
        draw.rectangle((0, 184, 319, 199), fill=(235, 235, 235))
    if marker:
        draw.rectangle((120, 50, 200, 130), fill=(245, 245, 245))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_parse_query_user_preserves_exact_account_session_and_state():
    stdout = (
        " USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME\n"
        " abrichr               console             1  Active      none   7/14/2026\n"
        ">oaflowq_ab12          rdp-tcp#0           3  Active          .  7/17/2026\n"
        " disconnected                             8  Disc         1:02  7/17/2026\n"
    )
    assert rdp._parse_query_user(stdout) == [
        rdp._UserSession("abrichr", "console", 1, "Active"),
        rdp._UserSession("oaflowq_ab12", "rdp-tcp#0", 3, "Active"),
        rdp._UserSession("disconnected", None, 8, "Disc"),
    ]


def test_parse_query_user_refuses_unknown_row_shape():
    with pytest.raises(ValueError, match="unrecognized query-user row"):
        rdp._parse_query_user("unsafe session output without an id\n")


class _SessionVM:
    def __init__(
        self, query_outputs: list[str], explorer_outputs: list[str] | None = None
    ):
        self.query_outputs = iter(query_outputs)
        self.explorer_outputs = iter(explorer_outputs or [])
        self.commands: list[str] = []

    def exec_cmd(self, command: str, timeout: float = 0):
        self.commands.append(command)
        if command == "query user":
            return _completed(next(self.query_outputs), returncode=1)
        if command.startswith("logoff "):
            return _completed()
        raise AssertionError(command)

    def exec_ps(self, command: str, timeout: float = 0):
        assert "Get-Process explorer" in command
        return _completed(next(self.explorer_outputs))


def test_logoff_preexisting_sessions_uses_only_exact_interactive_ids():
    active = " abrichr               console             1  Active      none   now\n"
    vm = _SessionVM([active, "No User exists for *\n"])
    sessions = rdp._logoff_preexisting_interactive_sessions(vm, timeout_s=1)
    assert sessions == [rdp._UserSession("abrichr", "console", 1, "Active")]
    assert vm.commands == ["query user", "logoff 1", "query user"]


def test_logoff_refuses_unrecognized_session_type_before_mutation():
    vm = _SessionVM(
        [" user                  ica-tcp#1           4  Active  none now\n"]
    )
    with pytest.raises(AssertionError, match="unrecognized interactive session"):
        rdp._logoff_preexisting_interactive_sessions(vm, timeout_s=1)
    assert vm.commands == ["query user"]


def test_wait_user_shell_requires_exact_account_and_explorer_session():
    active = " oaflowq_ab12          rdp-tcp#0           3  Active       . now\n"
    vm = _SessionVM([active], ["3\n"])
    assert rdp._wait_user_shell(vm, "OAFLOWQ_AB12", timeout_s=1) == 3


def test_wait_user_shell_refuses_explorer_from_different_session(monkeypatch):
    active = " oaflowq_ab12          rdp-tcp#0           3  Active       . now\n"
    vm = _SessionVM([active], ["1\n"])
    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(rdp.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(rdp.time, "sleep", lambda _seconds: None)
    with pytest.raises(AssertionError, match="explorer_session_ids"):
        rdp._wait_user_shell(vm, "oaflowq_ab12", timeout_s=1)


def test_qualification_desktop_rejects_welcome_and_accepts_light_taskbar():
    welcome = _png(background=18, marker=True)
    desktop = _png(background=0, taskbar=True)
    assert rdp._painted_readiness(welcome) is True
    assert rdp._qualification_desktop_ready(welcome) is False
    assert rdp._qualification_desktop_ready(desktop) is True


class _FrameBackend:
    def __init__(self, frames: list[bytes]):
        self.frames = iter(frames)

    def screenshot(self) -> bytes:
        return next(self.frames)


def test_wait_qualification_desktop_requires_transition_and_stability():
    welcome = _png(background=18, marker=True)
    desktop = _png(background=0, taskbar=True)
    backend = _FrameBackend([welcome, desktop, desktop, desktop])
    assert (
        rdp._wait_qualification_desktop(
            backend,
            welcome,
            timeout_s=1,
            stable_frames=3,
            settle_s=0,
        )
        == desktop
    )

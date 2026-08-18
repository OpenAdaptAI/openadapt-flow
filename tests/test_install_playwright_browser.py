"""Tests for the bounded Playwright installer used by GitHub Actions."""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from scripts import install_playwright_browser


def test_external_install_retries_once_then_succeeds() -> None:
    calls: list[tuple[tuple[str, ...], int]] = []
    outcomes = iter((124, 0))
    sleeps: list[float] = []

    def runner(command: Sequence[str], timeout_seconds: int) -> int:
        calls.append((tuple(command), timeout_seconds))
        return next(outcomes)

    result = install_playwright_browser.install_with_retry(
        attempts=2,
        timeout_seconds=600,
        retry_delay_seconds=5,
        runner=runner,
        sleeper=sleeps.append,
    )

    assert result == 0
    assert calls == [
        (install_playwright_browser.PLAYWRIGHT_INSTALL, 600),
        (install_playwright_browser.PLAYWRIGHT_INSTALL, 600),
    ]
    assert sleeps == [5]


def test_external_install_returns_the_final_failure() -> None:
    attempts = 0

    def runner(_command: Sequence[str], _timeout_seconds: int) -> int:
        nonlocal attempts
        attempts += 1
        return 7

    result = install_playwright_browser.install_with_retry(
        attempts=2,
        timeout_seconds=600,
        retry_delay_seconds=1,
        runner=runner,
        sleeper=lambda _seconds: None,
    )

    assert result == 7
    assert attempts == 2


def test_attempt_timeout_terminates_the_process() -> None:
    result = install_playwright_browser.run_attempt(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_seconds=1,
    )

    assert result == 124


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_posix_timeout_kills_sigterm_ignoring_child(tmp_path: Path) -> None:
    lock_path = tmp_path / "child.lock"
    pid_path = tmp_path / "child.pid"
    child_program = """
import fcntl
import os
from pathlib import Path
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
with Path(sys.argv[1]).open("w") as lock_file:
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
"""
    parent_program = """
from pathlib import Path
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]])
deadline = time.monotonic() + 10
while not Path(sys.argv[3]).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("child did not acquire its lock")
    time.sleep(0.01)
time.sleep(30)
"""

    result = install_playwright_browser.run_attempt(
        [
            sys.executable,
            "-c",
            parent_program,
            child_program,
            str(lock_path),
            str(pid_path),
        ],
        timeout_seconds=2,
    )

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    lock_released = False
    try:
        import fcntl

        with lock_path.open("w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                lock_released = True
    finally:
        if not lock_released:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert result == 124
    assert lock_released, "timed-out descendant still holds the installer lock"


def test_posix_partial_group_kill_uses_exact_privileged_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal_signals: list[tuple[int, signal.Signals]] = []
    privileged_signals: list[tuple[int, signal.Signals]] = []
    group_exit = iter((False, False, True))

    class FakeProcess:
        pid = 2267

    def fake_killpg(process_group_id: int, process_signal: signal.Signals) -> None:
        normal_signals.append((process_group_id, process_signal))

    def fake_sudo_signal(process_group_id: int, process_signal: signal.Signals) -> bool:
        privileged_signals.append((process_group_id, process_signal))
        return True

    monkeypatch.setattr(install_playwright_browser.os, "killpg", fake_killpg)
    monkeypatch.setattr(
        install_playwright_browser,
        "_wait_for_process_group_exit",
        lambda _process: next(group_exit),
    )
    monkeypatch.setattr(
        install_playwright_browser,
        "_sudo_signal_process_group",
        fake_sudo_signal,
    )
    process = cast("install_playwright_browser.subprocess.Popen[bytes]", FakeProcess())

    install_playwright_browser._terminate_process_group(process, platform="posix")

    assert normal_signals == [
        (2267, signal.SIGTERM),
        (2267, signal.SIGKILL),
    ]
    assert privileged_signals == [(2267, signal.SIGKILL)]


def test_privileged_fallback_is_bounded_and_targets_only_the_exact_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(
        command: list[str], **kwargs: Any
    ) -> install_playwright_browser.subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return install_playwright_browser.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(install_playwright_browser.subprocess, "run", fake_run)

    assert install_playwright_browser._sudo_signal_process_group(2267, signal.SIGKILL)
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == ["sudo", "-n", "/bin/kill", "-SIGKILL", "--", "-2267"]
    assert kwargs["timeout"] == install_playwright_browser.SUDO_SIGNAL_TIMEOUT_SECONDS


def test_windows_timeout_terminates_the_complete_child_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taskkill_calls: list[tuple[list[str], dict[str, Any]]] = []

    class FakeProcess:
        pid = 4815

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            raise AssertionError("taskkill must terminate the Windows tree")

        def wait(self, timeout: int) -> int:
            assert timeout == install_playwright_browser.PROCESS_EXIT_TIMEOUT_SECONDS
            return 1

    def fake_run(
        command: list[str], **kwargs: Any
    ) -> install_playwright_browser.subprocess.CompletedProcess[str]:
        taskkill_calls.append((command, kwargs))
        return install_playwright_browser.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(install_playwright_browser.subprocess, "run", fake_run)
    process = cast("install_playwright_browser.subprocess.Popen[bytes]", FakeProcess())

    install_playwright_browser._terminate_process_group(process, platform="nt")

    assert len(taskkill_calls) == 1
    command, kwargs = taskkill_calls[0]
    assert command == ["taskkill", "/PID", "4815", "/T", "/F"]
    assert kwargs["timeout"] == 15


def test_windows_taskkill_failure_uses_bounded_parent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kill_calls = 0
    wait_timeouts: list[int] = []

    class FakeProcess:
        pid = 9137

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            nonlocal kill_calls
            kill_calls += 1

        def wait(self, timeout: int) -> int:
            wait_timeouts.append(timeout)
            return 1

    def fake_run(
        command: list[str], **_kwargs: Any
    ) -> install_playwright_browser.subprocess.CompletedProcess[str]:
        return install_playwright_browser.subprocess.CompletedProcess(command, 5)

    monkeypatch.setattr(install_playwright_browser.subprocess, "run", fake_run)
    process = cast("install_playwright_browser.subprocess.Popen[bytes]", FakeProcess())

    install_playwright_browser._terminate_process_group(process, platform="nt")

    assert kill_calls == 1
    assert wait_timeouts == [install_playwright_browser.PROCESS_EXIT_TIMEOUT_SECONDS]


def test_windows_final_wait_is_bounded_and_forces_the_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kill_calls = 0
    wait_timeouts: list[int] = []

    class FakeProcess:
        pid = 7214

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            nonlocal kill_calls
            kill_calls += 1

        def wait(self, timeout: int) -> int:
            wait_timeouts.append(timeout)
            if len(wait_timeouts) == 1:
                raise install_playwright_browser.subprocess.TimeoutExpired(
                    "installer", timeout
                )
            return 1

    def fake_run(
        command: list[str], **_kwargs: Any
    ) -> install_playwright_browser.subprocess.CompletedProcess[str]:
        return install_playwright_browser.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(install_playwright_browser.subprocess, "run", fake_run)
    process = cast("install_playwright_browser.subprocess.Popen[bytes]", FakeProcess())

    install_playwright_browser._terminate_process_group(process, platform="nt")

    assert kill_calls == 1
    assert wait_timeouts == [
        install_playwright_browser.PROCESS_EXIT_TIMEOUT_SECONDS,
        install_playwright_browser.PROCESS_EXIT_TIMEOUT_SECONDS,
    ]

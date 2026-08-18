"""Tests for the bounded Playwright installer used by GitHub Actions."""

from __future__ import annotations

import sys
from collections.abc import Sequence
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

        def wait(self) -> int:
            return 1

    def fake_run(command: list[str], **kwargs: Any) -> None:
        taskkill_calls.append((command, kwargs))

    monkeypatch.setattr(install_playwright_browser.subprocess, "run", fake_run)
    process = cast("install_playwright_browser.subprocess.Popen[bytes]", FakeProcess())

    install_playwright_browser._terminate_process_group(process, platform="nt")

    assert len(taskkill_calls) == 1
    command, kwargs = taskkill_calls[0]
    assert command == ["taskkill", "/PID", "4815", "/T", "/F"]
    assert kwargs["timeout"] == 15

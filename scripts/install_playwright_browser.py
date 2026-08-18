#!/usr/bin/env python3
"""Install the Playwright Chromium runtime with a bounded external retry.

The browser and OS-package download is an external dependency. A stalled
download must not hold a hosted runner for GitHub's six-hour default job
timeout. This helper bounds each attempt, terminates its complete process
group, and retries once. It does not retry tests or any product operation.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence

PLAYWRIGHT_INSTALL = ("playwright", "install", "--with-deps", "chromium")


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the timed-out installer and all of its child processes."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.kill()
        process.wait()
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_attempt(command: Sequence[str], timeout_seconds: int) -> int:
    """Run one installer attempt and return 124 when its time bound expires."""
    try:
        process = subprocess.Popen(
            command,
            start_new_session=os.name != "nt",
        )
    except FileNotFoundError:
        return 127
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        return 124


def install_with_retry(
    *,
    attempts: int,
    timeout_seconds: int,
    retry_delay_seconds: int,
    runner: Callable[[Sequence[str], int], int] = run_attempt,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Run only the external installer again after a bounded failure."""
    for attempt in range(1, attempts + 1):
        print(
            f"Playwright install attempt {attempt}/{attempts} "
            f"(timeout={timeout_seconds}s)",
            flush=True,
        )
        return_code = runner(PLAYWRIGHT_INSTALL, timeout_seconds)
        if return_code == 0:
            return 0
        if attempt == attempts:
            print(
                f"::error::Playwright install failed after {attempts} "
                f"bounded attempts (last exit={return_code})",
                flush=True,
            )
            return return_code
        print(
            f"::warning::Playwright install attempt {attempt} failed "
            f"(exit={return_code}); retrying external download",
            flush=True,
        )
        sleeper(retry_delay_seconds)
    raise AssertionError("attempt loop did not return")


def positive_int(value: str) -> int:
    """Parse a strictly positive command-line integer."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=positive_int, default=2)
    parser.add_argument(
        "--attempt-timeout-seconds",
        type=positive_int,
        default=600,
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=positive_int,
        default=5,
    )
    args = parser.parse_args()
    return install_with_retry(
        attempts=args.attempts,
        timeout_seconds=args.attempt_timeout_seconds,
        retry_delay_seconds=args.retry_delay_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())

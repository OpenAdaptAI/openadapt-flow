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
WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
PROCESS_EXIT_TIMEOUT_SECONDS = 5
PROCESS_GROUP_POLL_INTERVAL_SECONDS = 0.05
SUDO_SIGNAL_TIMEOUT_SECONDS = 5


def _wait_for_exit(process: subprocess.Popen[bytes]) -> bool:
    """Wait briefly for a terminated process and report whether it exited."""
    try:
        process.wait(timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return True


def _process_group_exists(process_group_id: int) -> bool:
    """Report whether a POSIX process group still has any members."""
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A privileged descendant can remain after its unprivileged group
        # leader exits. The group exists even though this user cannot signal it.
        return True
    return True


def _wait_for_process_group_exit(
    process: subprocess.Popen[bytes],
) -> bool:
    """Wait a bounded interval for every member of a POSIX group to exit."""
    deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
    while True:
        process.poll()  # Reap the group leader so its zombie does not retain the PGID.
        if not _process_group_exists(process.pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(PROCESS_GROUP_POLL_INTERVAL_SECONDS, remaining))


def _sudo_signal_process_group(
    process_group_id: int,
    process_signal: signal.Signals,
) -> bool:
    """Signal one exact privileged POSIX process group without prompting."""
    try:
        result = subprocess.run(
            [
                "sudo",
                "-n",
                "/bin/kill",
                f"-{process_signal.name}",
                "--",
                f"-{process_group_id}",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SUDO_SIGNAL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    *,
    platform: str = os.name,
) -> None:
    """Terminate the timed-out installer and all of its child processes."""
    if platform == "nt":
        if process.poll() is not None:
            return
        taskkill_succeeded = False
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            taskkill_succeeded = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        if not taskkill_succeeded and process.poll() is None:
            process.kill()
        if not _wait_for_exit(process) and process.poll() is None:
            process.kill()
            _wait_for_exit(process)
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        _sudo_signal_process_group(process.pid, signal.SIGTERM)
    if _wait_for_process_group_exit(process):
        return

    # The group leader can exit after SIGTERM while an unprivileged child
    # ignores it or a privileged apt descendant survives it. Signal the exact
    # group again rather than trusting only the leader's exit state.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        _sudo_signal_process_group(process.pid, signal.SIGKILL)
    if _wait_for_process_group_exit(process):
        return

    # killpg can report success after signaling only the same-UID members of a
    # mixed-ownership group. A final bounded, non-interactive exact-PGID signal
    # handles the root-owned apt process that Playwright starts on Linux CI.
    _sudo_signal_process_group(process.pid, signal.SIGKILL)
    _wait_for_process_group_exit(process)


def run_attempt(command: Sequence[str], timeout_seconds: int) -> int:
    """Run one installer attempt and return 124 when its time bound expires."""
    try:
        if os.name == "nt":
            process = subprocess.Popen(
                command,
                creationflags=WINDOWS_CREATE_NEW_PROCESS_GROUP,
            )
        else:
            process = subprocess.Popen(command, start_new_session=True)
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

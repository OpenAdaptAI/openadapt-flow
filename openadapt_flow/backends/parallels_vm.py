"""Programmatic control of a Parallels Desktop VM (Apple-Silicon host).

The Phase-2 desktop benchmark runs against a local Parallels Windows 11 VM
(native Apple-Silicon virtualization — the WAA/QEMU nested-virt stack cannot
run on M-series, so Parallels replaces it). This module is the fully
programmatic control plane the pipeline drives; there are no manual GUI steps.

Everything is built on ``prlctl``:

* lifecycle: ``status`` / ``start`` / ``stop`` / ``suspend`` / ``resume``
* reversibility: ``snapshot`` / ``list_snapshots`` / ``revert`` (never delete
  the user's snapshots — reverts only)
* in-guest execution: ``exec`` runs as ``NT AUTHORITY\\SYSTEM`` in session 0
* host-side screen capture: ``capture`` (``prlctl capture`` — bypasses the
  session-0 desktop-isolation that blocks an in-guest ``BitBlt``)
* file transfer: ``push_file`` (``prlctl exec`` hangs on long arguments, so
  files move over a short-lived host HTTP server + in-guest ``curl``)
* the WAA HTTP shim: ``launch_shim`` places ``waa_shim.py`` in the interactive
  console session (session 1) via ``session1_launch.py`` +
  ``CreateProcessAsUser`` — session 0 cannot screenshot or drive the desktop.

Importing this module has no side effects and needs no live VM, so the CI
mock tests import it freely; every real operation shells out lazily.
"""

from __future__ import annotations

import functools
import http.server
import os
import re
import socketserver
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

# The user's existing VM (see docs/desktop/PHASE2.md). Overridable per call.
DEFAULT_VM_UUID = "{d4f9c29a-52e1-4793-9334-7e971c3d0ab3}"
DEFAULT_PRLCTL = "/usr/local/bin/prlctl"
SHIM_PORT = 5000
_SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts",
                           "desktop")
# In-guest install locations (forward slashes: Python + curl accept them and
# they survive the host shell without backslash mangling).
GUEST_DIR = "C:/oa"
GUEST_PY = r"C:\Program Files\Python312-arm64\python.exe"


@dataclass
class SnapshotInfo:
    """One entry from ``prlctl snapshot-list``."""

    snapshot_id: str
    current: bool
    name: str = ""


class ParallelsError(RuntimeError):
    """A ``prlctl`` invocation failed."""


class ParallelsVM:
    """Thin, fully-programmatic wrapper over ``prlctl`` for one VM.

    Args:
        uuid: VM UUID (braces included) or name.
        prlctl: Path to the ``prlctl`` binary.
        python_guest: In-guest Python interpreter path.
    """

    def __init__(
        self,
        uuid: str = DEFAULT_VM_UUID,
        *,
        prlctl: str = DEFAULT_PRLCTL,
        python_guest: str = GUEST_PY,
    ) -> None:
        self.uuid = uuid
        self.prlctl = prlctl
        self.python_guest = python_guest

    # -- low-level -----------------------------------------------------------

    def _run(
        self, args: list[str], *, timeout: float = 120.0, check: bool = True
    ) -> subprocess.CompletedProcess:
        """Invoke ``prlctl <args>`` and return the completed process.

        Raises:
            ParallelsError: If ``check`` and the return code is nonzero.
        """
        proc = subprocess.run(
            [self.prlctl, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise ParallelsError(
                f"prlctl {' '.join(args)} -> rc={proc.returncode}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    # -- lifecycle -----------------------------------------------------------

    def status(self) -> str:
        """Return the VM power state (running/paused/suspended/stopped)."""
        proc = self._run(["list", "--all", "-o", "status,uuid"], check=False)
        for line in proc.stdout.splitlines():
            if self.uuid in line:
                return line.split()[0].strip()
        # Fall back to name match.
        proc = self._run(["list", "--all"], check=False)
        for line in proc.stdout.splitlines():
            if self.uuid in line:
                return line.split()[1].strip()
        return "unknown"

    def start(self) -> None:
        self._run(["start", self.uuid])

    def stop(self, *, force: bool = False) -> None:
        args = ["stop", self.uuid]
        if force:
            args.append("--kill")
        self._run(args)

    def suspend(self) -> None:
        """Suspend the VM (saves state; leaves nothing running)."""
        self._run(["suspend", self.uuid])

    def resume(self) -> None:
        self._run(["resume", self.uuid], check=False)

    def set_pause_idle(self, on: bool) -> None:
        """Toggle Parallels' pause-when-idle (must be OFF for headless runs)."""
        self._run(["set", self.uuid, "--pause-idle", "on" if on else "off"],
                  check=False)

    def ensure_running(self, *, settle_s: float = 6.0) -> None:
        """Bring the VM to a running state from any state, idempotently.

        Handles Parallels' auto-pause (``paused`` -> ``resume``) and disables
        pause-idle so a headless benchmark run is not silently frozen.
        """
        state = self.status()
        if state in ("suspended", "paused"):
            self.resume()
        elif state in ("stopped", "unknown"):
            self.start()
        self.set_pause_idle(False)
        # A resume can be immediately re-paused; force it running.
        for _ in range(3):
            if self.status() == "running":
                break
            self.resume()
            time.sleep(1)
        time.sleep(settle_s)

    # -- snapshots (reversibility) ------------------------------------------

    def snapshot(self, name: str, description: str = "") -> str:
        """Create a snapshot and return its id.

        Snapshots are the drift-reset and warm-boot mechanism: every run
        reverts to a clean ``opendental-ready`` state in seconds.
        """
        args = ["snapshot", self.uuid, "-n", name]
        if description:
            args += ["-d", description]
        proc = self._run(args)
        m = re.search(r"\{[0-9a-fA-F-]+\}", proc.stdout)
        if not m:
            raise ParallelsError(f"could not parse snapshot id: {proc.stdout}")
        return m.group(0)

    def list_snapshots(self) -> list[SnapshotInfo]:
        """Return all snapshots (``*`` marks the current one)."""
        proc = self._run(["snapshot-list", self.uuid], check=False)
        out: list[SnapshotInfo] = []
        for line in proc.stdout.splitlines():
            # Columns are PARENT_SNAPSHOT_ID then SNAPSHOT_ID; the current
            # snapshot is marked ``*`` before its SNAPSHOT_ID (the LAST id on
            # the line). Take the last match so the parent id is not mistaken
            # for the snapshot id.
            matches = re.findall(r"(\*?)(\{[0-9a-fA-F-]+\})", line)
            if matches:
                star, sid = matches[-1]
                out.append(SnapshotInfo(snapshot_id=sid, current=bool(star)))
        return out

    def revert(self, snapshot_id: str) -> None:
        """Revert to a snapshot (the per-run clean-state reset)."""
        self._run(["snapshot-switch", self.uuid, "-i", snapshot_id])

    # -- in-guest execution --------------------------------------------------

    def exec(
        self, args: list[str], *, timeout: float = 120.0, check: bool = False
    ) -> subprocess.CompletedProcess:
        """Run a program in-guest as SYSTEM (``prlctl exec``).

        NOTE: ``prlctl exec`` hangs on very long single arguments — keep
        commands short and move file payloads with :meth:`push_file`.
        """
        return self._run(["exec", self.uuid, *args], timeout=timeout,
                         check=check)

    def exec_cmd(self, cmdline: str, *, timeout: float = 120.0
                 ) -> subprocess.CompletedProcess:
        """Run ``cmd /c <cmdline>`` in-guest (quoting preserved by cmd)."""
        return self.exec(["cmd", "/c", cmdline], timeout=timeout)

    def exec_ps(self, script: str, *, timeout: float = 120.0
                ) -> subprocess.CompletedProcess:
        """Run a short PowerShell command in-guest."""
        return self.exec(["powershell", "-NoProfile", "-Command", script],
                         timeout=timeout)

    # -- host-side capture ---------------------------------------------------

    def capture(self, local_path: str) -> str:
        """Capture the VM screen host-side to ``local_path`` (PNG).

        Independent of in-guest state — the ground-truth screenshot even when
        the shim is down.
        """
        self._run(["capture", self.uuid, "--file", local_path])
        return local_path

    # -- networking ----------------------------------------------------------

    def guest_ip(self) -> str:
        """Return the guest's shared-network IPv4 (skips APIPA 169.254)."""
        proc = self.exec_cmd("ipconfig")
        addrs = re.findall(r"IPv4[^\n:]*:\s*([0-9.]+)", proc.stdout)
        for a in addrs:
            if not a.startswith("169.254"):
                return a
        if addrs:
            return addrs[0]
        raise ParallelsError("no guest IPv4 found")

    def host_ip(self, guest_ip: Optional[str] = None) -> str:
        """Return the Mac's IP on the guest's subnet (for host->guest HTTP)."""
        guest_ip = guest_ip or self.guest_ip()
        prefix = guest_ip.rsplit(".", 1)[0] + "."
        out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                addr = line.split()[1]
                if addr.startswith(prefix):
                    return addr
        return prefix + "2"  # Parallels host is conventionally .2

    # -- file transfer -------------------------------------------------------

    def push_file(
        self,
        local_path: str,
        guest_path: str,
        *,
        host_ip: Optional[str] = None,
        port: int = 0,
    ) -> None:
        """Copy a host file into the guest over a short-lived HTTP server.

        ``prlctl exec`` hangs on long arguments, ruling out base64-in-argv;
        an in-guest ``curl`` from a throwaway host server is robust and fast.
        Port 0 (default) binds an ephemeral port so concurrent pushes never
        collide.
        """
        host_ip = host_ip or self.host_ip()
        directory = os.path.dirname(os.path.abspath(local_path))
        name = os.path.basename(local_path)
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=directory
        )
        httpd = socketserver.TCPServer((host_ip, port), handler)
        bound_port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://{host_ip}:{bound_port}/{name}"
            proc = self.exec_cmd(f"curl -s -o {guest_path} {url}")
            if proc.returncode != 0:
                raise ParallelsError(f"push_file curl failed: {proc.stderr}")
        finally:
            httpd.shutdown()
            httpd.server_close()

    # -- the WAA shim (session 1) -------------------------------------------

    def _shim_paths(self) -> tuple[str, str]:
        shim = os.path.abspath(os.path.join(_SCRIPT_DIR, "waa_shim.py"))
        launcher = os.path.abspath(os.path.join(_SCRIPT_DIR,
                                                "session1_launch.py"))
        return shim, launcher

    def kill_shim(self) -> None:
        """Kill any in-guest Python (frees the shim port)."""
        self.exec_cmd("taskkill /F /IM python.exe /IM pythonw.exe 2>nul & "
                      "echo done")

    def launch_shim(
        self,
        *,
        port: int = SHIM_PORT,
        host_ip: Optional[str] = None,
        wait_s: float = 20.0,
    ) -> str:
        """Deploy + start the WAA shim in session 1; return its URL.

        Steps (all programmatic): ensure guest dir, push shim + launcher,
        open the firewall, kill stale Python, run the session-1 launcher as
        SYSTEM, then poll until the shim answers a real PNG.
        """
        host_ip = host_ip or self.host_ip()
        shim, launcher = self._shim_paths()
        self.exec_cmd(f"if not exist {GUEST_DIR} mkdir {GUEST_DIR}")
        self.push_file(shim, f"{GUEST_DIR}/waa_shim.py", host_ip=host_ip)
        self.push_file(launcher, f"{GUEST_DIR}/session1_launch.py",
                       host_ip=host_ip)
        self.exec(["netsh", "advfirewall", "firewall", "add", "rule",
                   "name=OAShim", "dir=in", "action=allow", "protocol=TCP",
                   f"localport={port}"])
        self.kill_shim()
        time.sleep(2)
        # Run the launcher as SYSTEM; it CreateProcessAsUser's the shim into
        # the interactive console session so mss/pyautogui address the real
        # desktop. Forward-slash script paths dodge host-shell mangling.
        self.exec([self.python_guest, f"{GUEST_DIR}/session1_launch.py",
                   f"{GUEST_DIR}/waa_shim.py", "--port", str(port)])
        url = f"http://{self.guest_ip()}:{port}"
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self._shim_alive(url):
                return url
            time.sleep(1.5)
        raise ParallelsError(f"shim did not come up at {url}")

    def _shim_alive(self, url: str) -> bool:
        try:
            import requests

            r = requests.get(f"{url}/screenshot", timeout=8)
            return r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
        except Exception:  # noqa: BLE001
            return False

    def shim_url(self, *, port: int = SHIM_PORT) -> str:
        """URL WindowsBackend should target (``http://<guest-ip>:<port>``)."""
        return f"http://{self.guest_ip()}:{port}"

# In-guest Windows agent (`win_agent`)

A tiny, self-contained HTTP server that runs **inside the Windows VM's
interactive desktop session (session 1)** and exposes exactly the endpoints
`WindowsBackend` calls. It is the piece that solves the **session-0 problem**:
`prlctl exec` (and any Windows service) runs as `NT AUTHORITY\SYSTEM` in
session 0, which is isolated from the logged-on desktop — a screenshot taken
there is blank and injected input goes nowhere. The agent must live in
session 1 so `mss`/`pyautogui`/`uiautomation` address the *real* desktop.

## Contract

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/screenshot` | raw PNG bytes of the live desktop (`Content-Type: image/png`) |
| `POST` | `/execute_windows` | `exec()` of **bare** Python; body `{"command": "..."}` — never `python -c`-wrapped. Response echoes captured stdout so UIA reads travel back. |
| `GET` | `/health` | liveness + `active_console_session` (unauthenticated) |

Only the Python **standard library** is imported at load; `mss`/Pillow (for the
screenshot) and `pyautogui`/`uiautomation` (used by the exec'd commands) import
lazily. No Flask required in the guest.

## Security (loopback default + optional bearer token)

`/execute_windows` is arbitrary remote code execution *by contract*, so:

- **Default bind is `127.0.0.1`** — reachable only from inside the guest. Expose
  it to the Parallels host (`WindowsBackend` over the shared network) only with
  an explicit `--host 0.0.0.0`.
- **Optional bearer token** (the PHI audit flagged the original shim as
  unauthenticated). Set `--token <secret>` or the `OAFLOW_AGENT_TOKEN` env var;
  then `/screenshot` and `/execute_windows` require
  `Authorization: Bearer <secret>` (constant-time compare) or return `401`.
  `WindowsBackend(server_url=..., auth_token=<secret>)` sends the header.

**Whenever you set `--host 0.0.0.0`, set a token.**

## Launching it in session 1

### A. From SYSTEM (`prlctl exec`) — programmatic, used by the harness
`openadapt_flow.backends.parallels_vm.ParallelsVM.launch_agent()` deploys
`server.py` into the guest and starts it via `scripts/desktop/session1_launch.py`
(`WTSQueryUserToken` → `CreateProcessAsUserW` with `lpDesktop=winsta0\default`),
then polls `/health`. This is what the opt-in e2e harness uses.

### B. At user logon — unattended VM
1. Copy this folder into the guest, e.g. `C:\oa\win_agent\`.
2. Register a logon scheduled task **running as the interactive user** (not
   SYSTEM), highest privileges:

   ```bat
   schtasks /Create /TN OAFlowWinAgent /SC ONLOGON /RL HIGHEST ^
     /TR "C:\oa\win_agent\run_agent.bat" /F
   ```

   To expose it to the host with auth, set the vars first (persist them for the
   task's user with `setx OAFLOW_AGENT_HOST 0.0.0.0` /
   `setx OAFLOW_AGENT_TOKEN <secret>`), then create the task.
3. Log on (or `schtasks /Run /TN OAFlowWinAgent`). Confirm with
   `curl http://127.0.0.1:5000/health` inside the guest — `active_console_session`
   must be a real session id (not `-1`/`0`).

`run_agent.bat` honors `OAFLOW_AGENT_PY`, `OAFLOW_AGENT_HOST`,
`OAFLOW_AGENT_PORT`, `OAFLOW_AGENT_TOKEN`.

## Relationship to `scripts/desktop/waa_shim.py`
The original `waa_shim.py` (Flask, binds `0.0.0.0`, no auth) remains the
default for the existing desktop benchmark. This package is the **hardened,
dependency-free, auth-capable** successor used by `launch_agent()` and the
snapshot-safe e2e; both speak the identical `WindowsBackend` contract.

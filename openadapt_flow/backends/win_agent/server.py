"""In-guest Windows agent server (WAA-contract HTTP shim, session 1).

Runs INSIDE the Windows VM's *interactive* desktop session and exposes exactly
the endpoints ``openadapt_flow.backends.windows_backend.WindowsBackend`` calls,
matching the Windows Agent Arena Flask contract (the WAADirect pattern):

    GET  /screenshot        -> raw PNG bytes of the desktop (Content-Type
                               image/png; NOT base64 JSON)
    POST /context/identity  -> PHI-free foreground-app + logon-session identity
    POST /input             -> bounded physical input operations (typed JSON)
    POST /input/guarded     -> frame/context/focus check + bounded input in one
                               serialized request
    POST /uia/locator-at    -> stable UIA locator at a demonstrated point
    POST /uia/text-at-point -> structured row text for identity verification
    POST /uia/find          -> zero / unique / ambiguous exact candidates
    POST /uia/act           -> unique native UIA action + delivery receipt
    POST /execute_windows   -> legacy arbitrary Python execution, DISABLED by
                               default and available only with the explicit
                               ``--allow-legacy-exec`` development switch.
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
* **Typed actions by default.** Production observation and actuation use a
  bounded JSON schema. The legacy arbitrary-exec compatibility route is off
  unless an operator explicitly enables it for local development.
* **Loopback by default.** The default bind is ``127.0.0.1``. Exposing the
  agent on the guest LAN interface is an explicit opt-in.
* **Optional bearer token.** The PHI at-rest audit flagged this shim as
  unauthenticated. When a token is configured (``--token`` or the
  ``OAFLOW_AGENT_TOKEN`` env var) every ``/screenshot`` and ``/execute_windows``
  request must carry ``Authorization: Bearer <token>`` or is rejected 401. The
  comparison is constant-time. ``/health`` stays unauthenticated (liveness only,
  no desktop bytes, no exec).
* **TLS in transit (encryption + pinned server identity).** The channel carries
  PHI (screenshots of the patient chart, the commands that read/write it), so
  the 2026 HIPAA Security Rule requires it be encrypted. When a cert/key pair is
  configured (``--certfile`` / ``--keyfile``, provisioned per run by the control
  plane) the listener serves **HTTPS**; the client pins the certificate's
  SHA-256 fingerprint (see ``tls.py`` for the trust model). Encryption and
  token-auth are independent factors -- ``--token`` is still required to expose
  the channel off loopback. Cert minting lives on the control plane
  (``cryptography``); the guest needs only stdlib ``ssl`` to wrap its socket.

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
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import struct
import subprocess
import time
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional

# PNG magic used to validate/return frames.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Env var the token is read from when ``--token`` is not passed (keeps the
# secret off the process command line / argv where feasible).
TOKEN_ENV_VAR = "OAFLOW_AGENT_TOKEN"

# Env vars the TLS cert/key paths are read from when the flags are not passed.
CERTFILE_ENV_VAR = "OAFLOW_AGENT_CERTFILE"
KEYFILE_ENV_VAR = "OAFLOW_AGENT_KEYFILE"

_FRAME_GEOMETRY_HEADER = "X-OpenAdapt-Frame-Geometry"
_FRAME_BINDING_HEADER = "X-OpenAdapt-Frame-Binding-SHA256"


@dataclass(frozen=True)
class MonitorGeometry:
    """One physical-pixel monitor rectangle in virtual-desktop coordinates."""

    device: str
    left: int
    top: int
    width: int
    height: int
    dpi_x: int
    dpi_y: int
    primary: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "dpi_x": self.dpi_x,
            "dpi_y": self.dpi_y,
            "primary": self.primary,
        }


@dataclass(frozen=True)
class FrameGeometry:
    """Exact physical-pixel coordinate space for one captured desktop frame."""

    origin_x: int
    origin_y: int
    width: int
    height: int
    monitors: tuple[MonitorGeometry, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "coordinate_space": "physical_virtual_desktop",
            "dpi_awareness": "per_monitor_v2",
            "origin_x": self.origin_x,
            "origin_y": self.origin_y,
            "width": self.width,
            "height": self.height,
            "monitors": [monitor.to_payload() for monitor in self.monitors],
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def frame_to_virtual(self, x: int, y: int) -> tuple[int, int]:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(
                f"frame point {(x, y)!r} is outside {(self.width, self.height)!r}"
            )
        return self.origin_x + x, self.origin_y + y

    def virtual_to_frame(self, x: int, y: int) -> tuple[int, int]:
        frame_x = x - self.origin_x
        frame_y = y - self.origin_y
        if not (0 <= frame_x < self.width and 0 <= frame_y < self.height):
            raise ValueError(
                f"virtual point {(x, y)!r} is outside the captured desktop"
            )
        return frame_x, frame_y

    @classmethod
    def from_payload(cls, value: object) -> "FrameGeometry":
        if not isinstance(value, dict):
            raise ValueError("frame geometry must be an object")
        required = {
            "version",
            "coordinate_space",
            "dpi_awareness",
            "origin_x",
            "origin_y",
            "width",
            "height",
            "monitors",
        }
        if set(value) != required:
            raise ValueError("frame geometry has an invalid field set")
        if value["version"] != 1:
            raise ValueError("unsupported frame geometry version")
        if value["coordinate_space"] != "physical_virtual_desktop":
            raise ValueError("frame geometry is not physical virtual-desktop space")
        if value["dpi_awareness"] != "per_monitor_v2":
            raise ValueError("frame geometry is not Per-Monitor-v2 aware")

        def integer(name: str, *, positive: bool = False) -> int:
            item = value[name]
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"frame geometry {name} must be an integer")
            if abs(item) > 1_000_000 or (positive and item <= 0):
                raise ValueError(f"frame geometry {name} is out of bounds")
            return item

        origin_x = integer("origin_x")
        origin_y = integer("origin_y")
        width = integer("width", positive=True)
        height = integer("height", positive=True)
        raw_monitors = value["monitors"]
        if not isinstance(raw_monitors, list) or not 1 <= len(raw_monitors) <= 32:
            raise ValueError("frame geometry needs 1-32 monitors")
        monitors: list[MonitorGeometry] = []
        monitor_keys = {
            "device",
            "left",
            "top",
            "width",
            "height",
            "dpi_x",
            "dpi_y",
            "primary",
        }
        for raw in raw_monitors:
            if not isinstance(raw, dict) or set(raw) != monitor_keys:
                raise ValueError("monitor geometry has an invalid field set")
            device = raw["device"]
            if not isinstance(device, str) or not 1 <= len(device) <= 128:
                raise ValueError("monitor device must be a bounded string")

            def monitor_integer(name: str, *, positive: bool = False) -> int:
                item = raw[name]
                if isinstance(item, bool) or not isinstance(item, int):
                    raise ValueError(f"monitor {name} must be an integer")
                if abs(item) > 1_000_000 or (positive and item <= 0):
                    raise ValueError(f"monitor {name} is out of bounds")
                return item

            primary = raw["primary"]
            if not isinstance(primary, bool):
                raise ValueError("monitor primary must be boolean")
            monitor = MonitorGeometry(
                device=device,
                left=monitor_integer("left"),
                top=monitor_integer("top"),
                width=monitor_integer("width", positive=True),
                height=monitor_integer("height", positive=True),
                dpi_x=monitor_integer("dpi_x", positive=True),
                dpi_y=monitor_integer("dpi_y", positive=True),
                primary=primary,
            )
            if not (48 <= monitor.dpi_x <= 960 and 48 <= monitor.dpi_y <= 960):
                raise ValueError("monitor DPI is outside the supported range")
            if not (
                origin_x <= monitor.left
                and origin_y <= monitor.top
                and monitor.left + monitor.width <= origin_x + width
                and monitor.top + monitor.height <= origin_y + height
            ):
                raise ValueError("monitor rectangle is outside the virtual desktop")
            monitors.append(monitor)
        if sum(monitor.primary for monitor in monitors) != 1:
            raise ValueError("frame geometry must contain one primary monitor")
        if len({monitor.device.casefold() for monitor in monitors}) != len(monitors):
            raise ValueError("monitor device identifiers must be unique")
        ordered = tuple(
            sorted(monitors, key=lambda item: (item.left, item.top, item.device))
        )
        return cls(origin_x, origin_y, width, height, ordered)


@dataclass(frozen=True)
class CapturedDesktopFrame:
    """A PNG and the exact virtual-desktop geometry captured with it."""

    png: bytes
    geometry: FrameGeometry


def encode_frame_geometry_header(geometry: FrameGeometry) -> str:
    return base64.urlsafe_b64encode(geometry.canonical_bytes()).decode("ascii")


def decode_frame_geometry_header(value: str) -> FrameGeometry:
    if not isinstance(value, str) or not value:
        raise ValueError("missing frame geometry header")
    try:
        raw = base64.b64decode(value, altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except Exception as exc:
        raise ValueError("invalid frame geometry header") from exc
    return FrameGeometry.from_payload(payload)


def frame_binding_sha256(png: bytes, geometry: FrameGeometry) -> str:
    return hashlib.sha256(geometry.canonical_bytes() + b"\0" + png).hexdigest()


GrabFn = Callable[[], bytes | CapturedDesktopFrame]
InputFn = Callable[[dict[str, Any]], dict[str, Any]]
UiaFn = Callable[[str, dict[str, Any]], dict[str, Any]]
ContextFn = Callable[[], dict[str, Any]]

_MAX_BODY_BYTES = 1_048_576
_MAX_TEXT_CHARS = 65_536
_MAX_UIA_NODES = 5_000
_MAX_UIA_CANDIDATES = 16
_MAX_UIA_DEPTH = 24

_ROLE_TO_CONTROL = {
    "button": "ButtonControl",
    "link": "HyperlinkControl",
    "menuitem": "MenuItemControl",
    "tab": "TabItemControl",
    "listitem": "ListItemControl",
    "checkbox": "CheckBoxControl",
    "radio": "RadioButtonControl",
    "textbox": "EditControl",
}
_CONTROL_TO_ROLE = {value: key for key, value in _ROLE_TO_CONTROL.items()}
_ACTIONABLE_TYPES = frozenset(
    {
        *_CONTROL_TO_ROLE,
        "SplitButtonControl",
    }
)


class AgentRequestError(ValueError):
    """A bounded typed request was invalid or safely refused."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _exact_object(
    value: object,
    *,
    required: frozenset[str] = frozenset(),
    optional: frozenset[str] = frozenset(),
    label: str = "request",
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentRequestError(400, "invalid_schema", f"{label} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing or unknown:
        raise AgentRequestError(
            400,
            "invalid_schema",
            f"{label} has missing={sorted(missing)} unknown={sorted(unknown)}",
        )
    return value


def _bounded_int(value: object, label: str, *, limit: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > limit:
        raise AgentRequestError(
            400, "invalid_schema", f"{label} must be a bounded integer"
        )
    return value


def _png_size(png: bytes) -> tuple[int, int]:
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG frame")
    width, height = struct.unpack(">II", png[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("PNG frame has invalid dimensions")
    return int(width), int(height)


def _synthetic_frame_geometry(png: bytes) -> FrameGeometry:
    """Compatibility geometry for an injected byte-only test/legacy grabber."""

    width, height = _png_size(png)
    return FrameGeometry(
        origin_x=0,
        origin_y=0,
        width=width,
        height=height,
        monitors=(
            MonitorGeometry(
                device="DISPLAY1",
                left=0,
                top=0,
                width=width,
                height=height,
                dpi_x=96,
                dpi_y=96,
                primary=True,
            ),
        ),
    )


def _coerce_captured_frame(value: bytes | CapturedDesktopFrame) -> CapturedDesktopFrame:
    frame = (
        CapturedDesktopFrame(value, _synthetic_frame_geometry(value))
        if isinstance(value, bytes)
        else value
    )
    if not isinstance(frame, CapturedDesktopFrame):
        raise TypeError("desktop grabber returned an unsupported frame type")
    size = _png_size(frame.png)
    if size != (frame.geometry.width, frame.geometry.height):
        raise ValueError("captured PNG dimensions do not match its frame geometry")
    # Re-parse our own payload so a hand-built injected geometry cannot bypass
    # the same exact validation used for HTTP input.
    geometry = FrameGeometry.from_payload(frame.geometry.to_payload())
    return CapturedDesktopFrame(frame.png, geometry)


def _frame_geometry_payload(value: object) -> FrameGeometry:
    try:
        return FrameGeometry.from_payload(value)
    except ValueError as exc:
        raise AgentRequestError(400, "invalid_schema", str(exc)) from exc


def _require_per_monitor_v2() -> None:
    """Make the current Windows agent thread use physical monitor pixels."""

    if os.name != "nt":
        raise RuntimeError("Per-Monitor-v2 desktop geometry requires Windows")
    import ctypes  # noqa: PLC0415 - Windows-only, lazy by design

    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise RuntimeError("Windows DPI APIs are unavailable")
    user32 = win_dll("user32", use_last_error=True)
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    getter = getattr(user32, "GetThreadDpiAwarenessContext", None)
    equal = getattr(user32, "AreDpiAwarenessContextsEqual", None)
    if setter is None or getter is None or equal is None:
        raise RuntimeError("Windows Per-Monitor-v2 DPI APIs are unavailable")
    context = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    setter.argtypes = [ctypes.c_void_p]
    setter.restype = ctypes.c_void_p
    getter.argtypes = []
    getter.restype = ctypes.c_void_p
    equal.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    equal.restype = ctypes.c_int
    if not setter(context):
        last_error = getattr(ctypes, "get_last_error", lambda: 0)
        raise OSError(last_error(), "cannot enable Per-Monitor-v2 DPI")
    if not equal(getter(), context):
        raise RuntimeError("Windows thread did not enter Per-Monitor-v2 DPI mode")


def _current_frame_geometry() -> FrameGeometry:
    """Read the exact Windows virtual desktop and per-monitor DPI topology."""

    _require_per_monitor_v2()
    import ctypes  # noqa: PLC0415 - Windows-only, lazy by design
    from ctypes import wintypes  # noqa: PLC0415

    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise RuntimeError("Windows monitor APIs are unavailable")
    user32 = win_dll("user32", use_last_error=True)

    class MonitorInfoExW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    monitors: list[MonitorGeometry] = []
    callback_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    monitor_enum_proc = callback_factory(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    user32.GetMonitorInfoW.argtypes = [
        wintypes.HMONITOR,
        ctypes.POINTER(MonitorInfoExW),
    ]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        monitor_enum_proc,
        wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    shcore = None
    try:
        shcore = win_dll("shcore", use_last_error=True)
    except OSError:
        pass

    def callback(
        monitor: Any,
        _hdc: Any,
        _rect: Any,
        _data: Any,
    ) -> bool:
        info = MonitorInfoExW()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        dpi_x = wintypes.UINT(96)
        dpi_y = wintypes.UINT(96)
        if shcore is not None:
            get_dpi = getattr(shcore, "GetDpiForMonitor", None)
            if get_dpi is not None:
                get_dpi.argtypes = [
                    wintypes.HMONITOR,
                    ctypes.c_int,
                    ctypes.POINTER(wintypes.UINT),
                    ctypes.POINTER(wintypes.UINT),
                ]
                get_dpi.restype = ctypes.c_long
                if get_dpi(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) != 0:
                    dpi_x.value = 96
                    dpi_y.value = 96
        rect = info.rcMonitor
        monitors.append(
            MonitorGeometry(
                device=str(info.szDevice),
                left=int(rect.left),
                top=int(rect.top),
                width=int(rect.right - rect.left),
                height=int(rect.bottom - rect.top),
                dpi_x=int(dpi_x.value),
                dpi_y=int(dpi_y.value),
                primary=bool(info.dwFlags & 1),
            )
        )
        return True

    callback_ref = monitor_enum_proc(callback)
    if not user32.EnumDisplayMonitors(None, None, callback_ref, 0):
        last_error = getattr(ctypes, "get_last_error", lambda: 0)
        raise OSError(last_error(), "cannot enumerate Windows monitors")
    if not monitors:
        raise RuntimeError("Windows reported no active monitors")
    get_metric = user32.GetSystemMetrics
    get_metric.argtypes = [ctypes.c_int]
    get_metric.restype = ctypes.c_int
    geometry = FrameGeometry(
        origin_x=int(get_metric(76)),  # SM_XVIRTUALSCREEN
        origin_y=int(get_metric(77)),  # SM_YVIRTUALSCREEN
        width=int(get_metric(78)),  # SM_CXVIRTUALSCREEN
        height=int(get_metric(79)),  # SM_CYVIRTUALSCREEN
        monitors=tuple(monitors),
    )
    return FrameGeometry.from_payload(geometry.to_payload())


def _normalize_virtual_point(
    x: int, y: int, geometry: FrameGeometry
) -> tuple[int, int]:
    """Normalize a physical virtual-desktop point for absolute SendInput."""

    if not (
        geometry.origin_x <= x < geometry.origin_x + geometry.width
        and geometry.origin_y <= y < geometry.origin_y + geometry.height
    ):
        raise ValueError("virtual input point is outside the captured desktop")
    normalized_x = (
        0
        if geometry.width == 1
        else round((x - geometry.origin_x) * 65535 / (geometry.width - 1))
    )
    normalized_y = (
        0
        if geometry.height == 1
        else round((y - geometry.origin_y) * 65535 / (geometry.height - 1))
    )
    return normalized_x, normalized_y


def _send_virtual_pointer_sequence(
    events: list[tuple[int, int, int]], geometry: FrameGeometry
) -> None:
    """Send virtual-desktop-aware absolute pointer events through SendInput."""

    if _current_frame_geometry() != geometry:
        raise AgentRequestError(
            409,
            "stale_geometry",
            "virtual desktop geometry changed at the SendInput boundary",
        )
    import ctypes  # noqa: PLC0415 - Windows-only, lazy by design
    from ctypes import wintypes  # noqa: PLC0415

    class MouseInput(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class InputUnion(ctypes.Union):
        _fields_ = [("mi", MouseInput)]

    class Input(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [("type", wintypes.DWORD), ("union", InputUnion)]

    mouse_move = 0x0001
    mouse_absolute = 0x8000
    mouse_virtual_desktop = 0x4000
    base_flags = mouse_move | mouse_absolute | mouse_virtual_desktop
    inputs: list[Input] = []
    for virtual_x, virtual_y, edge_flags in events:
        normalized_x, normalized_y = _normalize_virtual_point(
            virtual_x, virtual_y, geometry
        )
        inputs.append(
            Input(
                type=0,  # INPUT_MOUSE
                mi=MouseInput(
                    dx=normalized_x,
                    dy=normalized_y,
                    mouseData=0,
                    dwFlags=base_flags | edge_flags,
                    time=0,
                    dwExtraInfo=0,
                ),
            )
        )
    if not inputs:
        return
    array_type = Input * len(inputs)
    array = array_type(*inputs)
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise RuntimeError("Windows SendInput is unavailable")
    user32 = win_dll("user32", use_last_error=True)
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    sent = int(user32.SendInput(len(array), array, ctypes.sizeof(Input)))
    if sent != len(array):
        last_error = getattr(ctypes, "get_last_error", lambda: 0)
        raise OSError(last_error(), f"SendInput delivered {sent}/{len(array)} events")


def _delivery_receipt(
    operation: str,
    *,
    native: bool,
    target_fingerprint: Optional[str] = None,
) -> dict[str, Any]:
    """Receipt for input delivery only -- never an outcome assertion."""
    return {
        "status": "delivered",
        "receipt_id": secrets.token_hex(12),
        "operation": operation,
        "native": native,
        "target_fingerprint": target_fingerprint,
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        "outcome_verified": False,
    }


def _perform_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one bounded physical-input request in the interactive session."""
    data = _exact_object(
        payload,
        required=frozenset({"action"}),
        optional=frozenset(
            {
                "x",
                "y",
                "double",
                "button",
                "end_x",
                "end_y",
                "text",
                "interval_s",
                "keys",
                "horizontal_notches",
                "vertical_notches",
                "frame_geometry",
            }
        ),
        label="input request",
    )
    action = data["action"]
    if action not in {"click", "drag", "type_text", "press", "scroll"}:
        raise AgentRequestError(400, "unsupported_action", "unsupported input action")

    if action == "click":
        expected = {"action", "x", "y", "double", "button", "frame_geometry"}
        if set(data) - expected or not {"x", "y", "frame_geometry"}.issubset(data):
            raise AgentRequestError(400, "invalid_schema", "invalid click fields")
        frame_x = _bounded_int(data["x"], "x")
        frame_y = _bounded_int(data["y"], "y")
        geometry = _frame_geometry_payload(data["frame_geometry"])
        double = data.get("double", False)
        if not isinstance(double, bool):
            raise AgentRequestError(400, "invalid_schema", "double must be boolean")
        button = data.get("button", "left")
        if button not in {"left", "right"}:
            raise AgentRequestError(
                400, "invalid_schema", "button must be left or right"
            )
        if double and button != "left":
            raise AgentRequestError(
                400, "invalid_schema", "double right click is unsupported"
            )
        current_geometry = _current_frame_geometry()
        if current_geometry != geometry:
            raise AgentRequestError(
                409,
                "stale_geometry",
                "virtual desktop geometry changed before pointer input",
            )
        try:
            virtual_x, virtual_y = geometry.frame_to_virtual(frame_x, frame_y)
        except ValueError as exc:
            raise AgentRequestError(400, "invalid_schema", str(exc)) from exc
        down = 0x0008 if button == "right" else 0x0002
        up = 0x0010 if button == "right" else 0x0004
        edges = [(virtual_x, virtual_y, down), (virtual_x, virtual_y, up)]
        if double:
            edges.extend([(virtual_x, virtual_y, down), (virtual_x, virtual_y, up)])
        _send_virtual_pointer_sequence(edges, geometry)
        return _delivery_receipt(
            "physical_double_click"
            if double
            else ("physical_right_click" if button == "right" else "physical_click"),
            native=False,
        )

    if action == "drag":
        if set(data) != {
            "action",
            "x",
            "y",
            "end_x",
            "end_y",
            "frame_geometry",
        }:
            raise AgentRequestError(400, "invalid_schema", "invalid drag fields")
        frame_x = _bounded_int(data["x"], "x")
        frame_y = _bounded_int(data["y"], "y")
        frame_end_x = _bounded_int(data["end_x"], "end_x")
        frame_end_y = _bounded_int(data["end_y"], "end_y")
        geometry = _frame_geometry_payload(data["frame_geometry"])
        current_geometry = _current_frame_geometry()
        if current_geometry != geometry:
            raise AgentRequestError(
                409,
                "stale_geometry",
                "virtual desktop geometry changed before pointer input",
            )
        try:
            virtual_x, virtual_y = geometry.frame_to_virtual(frame_x, frame_y)
            virtual_end_x, virtual_end_y = geometry.frame_to_virtual(
                frame_end_x, frame_end_y
            )
        except ValueError as exc:
            raise AgentRequestError(400, "invalid_schema", str(exc)) from exc
        _send_virtual_pointer_sequence(
            [
                (virtual_x, virtual_y, 0),
                (virtual_x, virtual_y, 0x0002),
                (virtual_end_x, virtual_end_y, 0),
                (virtual_end_x, virtual_end_y, 0x0004),
            ],
            geometry,
        )
        return _delivery_receipt("physical_drag", native=False)

    if action == "type_text":
        expected = {"action", "text", "interval_s"}
        if set(data) - expected or "text" not in data:
            raise AgentRequestError(400, "invalid_schema", "invalid type_text fields")
        text = data["text"]
        interval = data.get("interval_s", 0.05)
        if not isinstance(text, str) or len(text) > _MAX_TEXT_CHARS:
            raise AgentRequestError(
                400, "invalid_schema", "text exceeds the bounded string contract"
            )
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not 0 <= float(interval) <= 1
        ):
            raise AgentRequestError(
                400, "invalid_schema", "interval_s must be between 0 and 1"
            )
        import pyautogui  # noqa: PLC0415 - Windows-only, lazy by design

        pyautogui.FAILSAFE = False
        if text:
            if text.isascii():
                pyautogui.write(text, interval=float(interval))
            else:
                encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
                ps = (
                    "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString("
                    f"[Convert]::FromBase64String('{encoded}')))"
                )
                completed = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True,
                    timeout=10,
                )
                if completed.returncode != 0:
                    raise RuntimeError("Set-Clipboard failed")
                time.sleep(0.2)
                pyautogui.hotkey("ctrl", "v")
        return _delivery_receipt("physical_type_text", native=False)

    if action == "press":
        if set(data) != {"action", "keys"}:
            raise AgentRequestError(400, "invalid_schema", "invalid press fields")
        keys = data["keys"]
        if (
            not isinstance(keys, list)
            or not 1 <= len(keys) <= 4
            or any(not isinstance(key, str) or not 1 <= len(key) <= 32 for key in keys)
        ):
            raise AgentRequestError(
                400, "invalid_schema", "keys must contain 1-4 bounded strings"
            )
        import pyautogui  # noqa: PLC0415 - Windows-only, lazy by design

        pyautogui.FAILSAFE = False
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        return _delivery_receipt("physical_press", native=False)

    if set(data) != {"action", "horizontal_notches", "vertical_notches"}:
        raise AgentRequestError(400, "invalid_schema", "invalid scroll fields")
    horizontal = _bounded_int(
        data["horizontal_notches"], "horizontal_notches", limit=1000
    )
    vertical = _bounded_int(data["vertical_notches"], "vertical_notches", limit=1000)
    import pyautogui  # noqa: PLC0415 - Windows-only, lazy by design

    pyautogui.FAILSAFE = False
    if vertical:
        pyautogui.scroll(vertical)
    if horizontal:
        pyautogui.hscroll(horizontal)
    return _delivery_receipt("physical_scroll", native=False)


def _control_value(control: object, attr: str, default: object = "") -> object:
    try:
        return getattr(control, attr, default)
    except Exception:  # noqa: BLE001 - accessibility providers are fallible
        return default


def _parent(control: object) -> Optional[object]:
    method = _control_value(control, "GetParentControl", None)
    try:
        return method() if callable(method) else None
    except Exception:  # noqa: BLE001
        return None


def _window_name(control: object) -> Optional[str]:
    node: Optional[object] = control
    for _ in range(_MAX_UIA_DEPTH):
        if str(_control_value(node, "ControlTypeName", "")) == "WindowControl":
            value = " ".join(str(_control_value(node, "Name", "") or "").split())
            return value or None
        node = _parent(node)
        if node is None:
            break
    return None


def _role(control: object) -> Optional[str]:
    control_type = str(_control_value(control, "ControlTypeName", ""))
    if control_type == "SplitButtonControl":
        return "button"
    return _CONTROL_TO_ROLE.get(control_type)


def _bounds(control: object) -> Optional[list[int]]:
    rect: Any = _control_value(control, "BoundingRectangle", None)
    try:
        if rect is None or rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
    except Exception:  # noqa: BLE001
        return None


def _locator_from_control(control: object) -> Optional[dict[str, Any]]:
    automation_id = str(_control_value(control, "AutomationId", "") or "") or None
    name = " ".join(str(_control_value(control, "Name", "") or "").split()) or None
    role = _role(control)
    if not automation_id and not (role and name):
        return None
    return {
        "automation_id": automation_id,
        "role": role,
        "name": name,
        "window_name": _window_name(control),
    }


def _locator_payload(value: object) -> dict[str, Any]:
    data = _exact_object(
        value,
        optional=frozenset({"automation_id", "role", "name", "window_name"}),
        label="locator",
    )
    out: dict[str, Any] = {}
    for key in ("automation_id", "role", "name", "window_name"):
        item = data.get(key)
        if item is not None and (
            not isinstance(item, str) or not 1 <= len(item) <= 512
        ):
            raise AgentRequestError(
                400, "invalid_schema", f"locator.{key} must be a bounded string or null"
            )
        out[key] = item or None
    if not out["automation_id"] and not (out["role"] and out["name"]):
        raise AgentRequestError(
            400, "invalid_locator", "locator needs automation_id or exact role+name"
        )
    if out["role"] and out["role"] not in _ROLE_TO_CONTROL:
        raise AgentRequestError(400, "invalid_locator", "unsupported locator role")
    return out


def _runtime_id(control: object) -> list[int]:
    """Return UIA's per-element runtime id as a bounded JSON value."""
    method = _control_value(control, "GetRuntimeId", None)
    try:
        value = list(method()) if callable(method) else []
    except Exception:  # noqa: BLE001 - some providers omit RuntimeId
        return []
    if len(value) > 64 or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        return []
    return value


def _target_fingerprint(control: object, candidate: dict[str, Any]) -> str:
    """Bind resolution to this exact live UIA element and geometry.

    The locator alone is intentionally insufficient: a closed/reopened window
    can expose the same AutomationId/name while referring to a different live
    control. RuntimeId/process/handle and bounds make that replacement (or a
    move that would invalidate the recorded verification point) stale.
    """
    identity = {
        "locator": {
            key: candidate.get(key)
            for key in ("automation_id", "role", "name", "window_name")
        },
        "bounds": candidate.get("bounds"),
        "runtime_id": _runtime_id(control),
        "process_id": _control_value(control, "ProcessId", 0),
        "native_window_handle": _control_value(control, "NativeWindowHandle", 0),
        "class_name": str(_control_value(control, "ClassName", "") or ""),
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _candidate(control: object) -> Optional[dict[str, Any]]:
    locator = _locator_from_control(control)
    bounds = _bounds(control)
    if locator is None or bounds is None:
        return None
    role = locator["role"]
    operations = {
        "button": ["invoke"],
        "link": ["invoke"],
        "menuitem": ["invoke"],
        "textbox": ["focus"],
        "checkbox": ["toggle"],
        "radio": ["select"],
        "tab": ["select"],
        "listitem": ["select"],
    }.get(role, [])
    candidate = {
        **locator,
        "bounds": bounds,
        "point": [int((bounds[0] + bounds[2]) / 2), int((bounds[1] + bounds[3]) / 2)],
        "supported_operations": operations,
    }
    candidate["fingerprint"] = _target_fingerprint(control, candidate)
    return candidate


def _find_candidates(
    locator: dict[str, Any], auto: Any
) -> tuple[list[tuple[Any, dict[str, Any]]], bool]:
    try:
        root = auto.GetRootControl()
    except Exception as exc:  # noqa: BLE001
        raise AgentRequestError(
            503, "uia_unavailable", "UI Automation is unavailable"
        ) from exc
    stack: list[tuple[Any, int]] = [(root, 0)]
    found: list[tuple[Any, dict[str, Any]]] = []
    visited = 0
    truncated = False
    while stack:
        control, depth = stack.pop()
        visited += 1
        if visited > _MAX_UIA_NODES:
            truncated = True
            break
        candidate = _candidate(control)
        if candidate is not None:
            matches = True
            for key in ("automation_id", "role", "name", "window_name"):
                expected = locator.get(key)
                if expected is not None and candidate.get(key) != expected:
                    matches = False
                    break
            if matches:
                found.append((control, candidate))
                if len(found) >= _MAX_UIA_CANDIDATES:
                    truncated = True
                    break
        if depth >= _MAX_UIA_DEPTH:
            continue
        children = _control_value(control, "GetChildren", None)
        try:
            values = list(children()) if callable(children) else []
        except Exception:  # noqa: BLE001
            values = []
        stack.extend((child, depth + 1) for child in reversed(values))
    return found, truncated


def _structured_text_at(auto: Any, x: int, y: int) -> Optional[str]:
    try:
        element = auto.ControlFromPoint(x, y)
    except Exception:  # noqa: BLE001
        return None
    if element is None:
        return None
    row = element
    found_row = False
    for _ in range(6):
        if str(_control_value(row, "ControlTypeName", "")) in {
            "DataItemControl",
            "ListItemControl",
            "TreeItemControl",
            "TableRowControl",
        }:
            found_row = True
            break
        row = _parent(row)
        if row is None:
            break
    if not found_row or row is None:
        return None
    own = element
    for _ in range(6):
        parent = _parent(own)
        if parent is None:
            own = None
            break
        if parent is row:
            break
        own = parent
    parts: list[str] = []
    children = _control_value(row, "GetChildren", None)
    try:
        for child in children() if callable(children) else []:
            if own is not None and child is own:
                continue
            name = str(_control_value(child, "Name", "") or "")
            if name:
                parts.append(name)
    except Exception:  # noqa: BLE001
        pass
    if not parts:
        name = str(_control_value(row, "Name", "") or "")
        if name:
            parts.append(name)
    value = " ".join(" ".join(parts).split())
    return value or None


def _perform_uia_initialized(
    auto: Any, operation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Perform one typed UIA operation in an initialized COM apartment."""
    if operation in {"locator-at", "text-at-point", "focused-at-point"}:
        data = _exact_object(payload, required=frozenset({"x", "y"}), label=operation)
        x = _bounded_int(data["x"], "x")
        y = _bounded_int(data["y"], "y")
        if operation == "text-at-point":
            return {"status": "ok", "text": _structured_text_at(auto, x, y)}
        try:
            element = auto.ControlFromPoint(x, y)
        except Exception as exc:  # noqa: BLE001
            raise AgentRequestError(
                503, "uia_unavailable", "UI Automation is unavailable"
            ) from exc
        if element is None:
            return {"status": "ok", "locator": None}
        actionable: Optional[object] = None
        node: Optional[object] = element
        for _ in range(6):
            if str(_control_value(node, "ControlTypeName", "")) in _ACTIONABLE_TYPES:
                actionable = node
                break
            node = _parent(node)
            if node is None:
                break
        if operation == "focused-at-point":
            point_control = actionable or element
            try:
                focused = auto.GetFocusedControl()
            except Exception as exc:  # noqa: BLE001
                raise AgentRequestError(
                    503, "uia_unavailable", "focused UIA control is unavailable"
                ) from exc
            if focused is None:
                return {"status": "ok", "focused": False}
            focused_actionable: Optional[object] = None
            node = focused
            for _ in range(6):
                if (
                    str(_control_value(node, "ControlTypeName", ""))
                    in _ACTIONABLE_TYPES
                ):
                    focused_actionable = node
                    break
                node = _parent(node)
                if node is None:
                    break
            focused_control = focused_actionable or focused
            point_candidate = _candidate(point_control)
            focused_candidate = _candidate(focused_control)
            if point_candidate is None or focused_candidate is None:
                return {"status": "ok", "focused": False}
            matches = hmac.compare_digest(
                point_candidate["fingerprint"],
                focused_candidate["fingerprint"],
            )
            return {
                "status": "ok",
                "focused": matches,
                "target_fingerprint": (
                    point_candidate["fingerprint"] if matches else None
                ),
            }
        return {"status": "ok", "locator": _locator_from_control(actionable or element)}

    data = _exact_object(
        payload,
        required=frozenset({"locator"}),
        optional=frozenset({"operation", "expected_fingerprint"}),
        label=f"uia {operation}",
    )
    locator = _locator_payload(data["locator"])
    found, truncated = _find_candidates(locator, auto)
    candidates = [candidate for _, candidate in found]
    if operation == "find":
        match = (
            "ambiguous"
            if len(found) > 1 or truncated
            else "unique"
            if len(found) == 1
            else "not_found"
        )
        return {
            "status": "ok",
            "match": match,
            "candidate_count": len(found),
            "truncated": truncated,
            "candidates": candidates,
        }
    if operation != "act":
        raise AgentRequestError(404, "not_found", "unknown UIA operation")
    if len(found) != 1 or truncated:
        code = "ambiguous_target" if found or truncated else "target_not_found"
        raise AgentRequestError(409, code, "UIA target is not uniquely resolvable")
    requested = data.get("operation")
    expected_fingerprint = data.get("expected_fingerprint")
    if requested not in {"click", "double_click"}:
        raise AgentRequestError(400, "unsupported_action", "unsupported UIA action")
    if not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64:
        raise AgentRequestError(
            400, "invalid_schema", "expected_fingerprint is required"
        )
    control, candidate = found[0]
    if not hmac.compare_digest(expected_fingerprint, candidate["fingerprint"]):
        raise AgentRequestError(
            409, "stale_target", "UIA target changed after resolution"
        )
    if requested == "double_click":
        raise AgentRequestError(
            409, "native_action_unavailable", "native double-click is unavailable"
        )
    role = candidate.get("role")
    try:
        if role in {"button", "link", "menuitem"}:
            control.GetInvokePattern().Invoke()
            delivered = "uia_invoke"
        elif role == "textbox":
            if control.SetFocus() is False:
                raise AgentRequestError(
                    409, "native_action_failed", "native UIA focus was rejected"
                )
            delivered = "uia_focus"
        elif role == "checkbox":
            control.GetTogglePattern().Toggle()
            delivered = "uia_toggle"
        elif role in {"radio", "tab", "listitem"}:
            control.GetSelectionItemPattern().Select()
            delivered = "uia_select"
        else:
            raise AgentRequestError(
                409, "native_action_unavailable", "no native action pattern"
            )
    except AgentRequestError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AgentRequestError(
            409, "native_action_failed", "native UIA action failed"
        ) from exc
    return {
        "status": "ok",
        "candidate_count": 1,
        "receipt": _delivery_receipt(
            delivered,
            native=True,
            target_fingerprint=candidate["fingerprint"],
        ),
    }


def _perform_uia(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Perform one typed UIA operation on one COM-affine server thread."""
    try:
        import uiautomation as auto  # noqa: PLC0415 - Windows-only, lazy
    except Exception as exc:  # noqa: BLE001
        raise AgentRequestError(
            503, "uia_unavailable", "UI Automation is unavailable"
        ) from exc
    if os.name == "nt":
        # The guest interpreter is installed under Program Files. comtypes
        # otherwise tries to emit generated UIAutomationCore wrappers beside
        # the package and the interactive (non-admin) desktop user gets EACCES.
        # Official comtypes behavior supports gen_dir=None for in-memory-only
        # wrapper generation, preserving least privilege without preinstall
        # mutations or an elevation requirement.
        import comtypes.client  # noqa: PLC0415

        comtypes.client.gen_dir = None
    try:
        with auto.UIAutomationInitializerInThread():
            return _perform_uia_initialized(auto, operation, payload)
    except AgentRequestError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider/COM failures are bounded
        raise AgentRequestError(
            503, "uia_unavailable", "UI Automation is unavailable"
        ) from exc


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
        certfile: PEM certificate path. When set (with ``keyfile``) the listener
            serves **HTTPS** -- the PHI-bearing channel is encrypted in transit
            and the client pins this cert's fingerprint. Provisioned per run by
            the control plane (``win_agent.tls.generate_self_signed_cert``).
        keyfile: PEM private-key path matching ``certfile``. Required with it.
        allow_legacy_exec: Expose the arbitrary-Python compatibility route.
            False by default; production uses only bounded typed operations.
    """

    host: str = "127.0.0.1"
    port: int = 5000
    token: Optional[str] = None
    certfile: Optional[str] = None
    keyfile: Optional[str] = None
    allow_legacy_exec: bool = False

    def authed(self) -> bool:
        """True when a bearer token is required."""
        return bool(self.token)

    def tls_enabled(self) -> bool:
        """True when the listener serves HTTPS (a cert/key pair is set)."""
        return bool(self.certfile and self.keyfile)

    def __post_init__(self) -> None:
        """Reject a half-configured TLS pair (fail closed, never silent HTTP)."""
        if bool(self.certfile) != bool(self.keyfile):
            raise ValueError(
                "TLS needs BOTH certfile and keyfile (got only one) -- refusing "
                "to fall back to plaintext HTTP for a PHI channel"
            )


def _grab_desktop_png() -> CapturedDesktopFrame:
    """Capture one frame with its exact physical virtual-desktop topology."""

    import mss  # noqa: PLC0415 - Windows-only, imported lazily by design
    from PIL import Image  # noqa: PLC0415

    before = _current_frame_geometry()
    with mss.mss() as sct:
        mon = sct.monitors[0]
        mss_geometry = (
            int(mon["left"]),
            int(mon["top"]),
            int(mon["width"]),
            int(mon["height"]),
        )
        expected_geometry = (
            before.origin_x,
            before.origin_y,
            before.width,
            before.height,
        )
        if mss_geometry != expected_geometry:
            raise RuntimeError(
                "mss virtual desktop does not match the Per-Monitor-v2 topology"
            )
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
    after = _current_frame_geometry()
    if after != before:
        raise RuntimeError("monitor topology changed during desktop capture")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    frame = CapturedDesktopFrame(buf.getvalue(), before)
    return _coerce_captured_frame(frame)


def _active_console_session() -> int:
    """Which console session this process is attached to (-1 if unknown)."""
    try:
        import ctypes  # noqa: PLC0415

        # ``ctypes.windll`` exists only on Windows; access it dynamically so
        # this stays importable + type-checkable on macOS/Linux CI.
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return -1
        return int(windll.kernel32.WTSGetActiveConsoleSessionId())
    except Exception:  # noqa: BLE001 - non-Windows / probe failure
        return -1


def _bounded_application_identifier(name: str) -> Optional[str]:
    """Canonicalize one executable basename into the public context-id syntax."""

    stem = os.path.splitext(os.path.basename(name))[0]
    ascii_name = (
        unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    )
    identifier = "".join(
        char.casefold()
        if char.isascii() and (char.isalnum() or char in "._:/-")
        else "-"
        for char in ascii_name
    )
    identifier = "-".join(part for part in identifier.split("-") if part)
    identifier = identifier.strip("-._:/")[:128]
    if not identifier or not identifier[0].isalnum():
        return None
    return identifier


def _foreground_window_identity() -> Optional[dict[str, Any]]:
    """Observe one PHI-free exact foreground-window/process identity."""

    if os.name != "nt":
        return None
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            return None
        user32 = win_dll("user32", use_last_error=True)
        kernel32 = win_dll("kernel32", use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        if (
            not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            or not pid.value
        ):
            return None
        process = kernel32.OpenProcess(0x1000, False, pid.value)
        if not process:
            return None
        try:
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                process, 0, path, ctypes.byref(size)
            ):
                return None
            owner = _bounded_application_identifier(path.value)
            if owner is None:
                return None
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                process,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            creation_ticks = (int(created.dwHighDateTime) << 32) | int(
                created.dwLowDateTime
            )
            window_value = getattr(hwnd, "value", hwnd)
            return {
                "window_id": str(int(window_value)),
                "pid": int(pid.value),
                "process_start_time": str(creation_ticks),
                "owner": owner,
            }
        finally:
            kernel32.CloseHandle(process)
    except Exception:  # noqa: BLE001 - unavailable means unverifiable
        return None


def _foreground_application_identity() -> Optional[str]:
    """Observe the foreground executable without reading its window title."""

    window = _foreground_window_identity()
    return str(window["owner"]) if window is not None else None


def _native_session_digest() -> Optional[str]:
    """Hash the live machine + interactive Windows logon-session identity."""

    if os.name != "nt":
        return None
    try:
        import ctypes  # noqa: PLC0415
        import importlib  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        class Luid(ctypes.Structure):
            _fields_ = [
                ("LowPart", wintypes.DWORD),
                ("HighPart", wintypes.LONG),
            ]

        class TokenStatistics(ctypes.Structure):
            _fields_ = [
                ("TokenId", Luid),
                ("AuthenticationId", Luid),
                ("ExpirationTime", ctypes.c_longlong),
                ("TokenType", wintypes.DWORD),
                ("ImpersonationLevel", wintypes.DWORD),
                ("DynamicCharged", wintypes.DWORD),
                ("DynamicAvailable", wintypes.DWORD),
                ("GroupCount", wintypes.DWORD),
                ("PrivilegeCount", wintypes.DWORD),
                ("ModifiedId", Luid),
            ]

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            return None
        kernel32 = win_dll("kernel32", use_last_error=True)
        advapi32 = win_dll("advapi32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.ProcessIdToSessionId.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        session_id = wintypes.DWORD()
        if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
            return None
        if int(session_id.value) != _active_console_session():
            return None

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            return None
        try:
            stats = TokenStatistics()
            returned = wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                token,
                10,  # TokenStatistics
                ctypes.byref(stats),
                ctypes.sizeof(stats),
                ctypes.byref(returned),
            ):
                return None
        finally:
            kernel32.CloseHandle(token)

        winreg = importlib.import_module("winreg")
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            machine_guid = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        authentication_id = (
            f"{int(stats.AuthenticationId.HighPart)}:"
            f"{int(stats.AuthenticationId.LowPart)}"
        )
        material = (
            f"windows-session-v1\0{machine_guid}\0{int(session_id.value)}"
            f"\0{authentication_id}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001 - unavailable means unverifiable
        return None


def _execution_context_identity() -> dict[str, Any]:
    """Return PHI-free identities observed from live Windows OS state."""

    window = _foreground_window_identity()
    return {
        "status": "ok",
        "application": window["owner"] if window is not None else None,
        "session": _native_session_digest(),
        "workflow_state": None,
        "window": window,
    }


def make_handler_class(
    config: AgentConfig,
    grab_fn: GrabFn = _grab_desktop_png,
    input_fn: InputFn = _perform_input,
    uia_fn: UiaFn = _perform_uia,
    context_fn: ContextFn = _execution_context_identity,
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

        def _send(
            self,
            status: int,
            body: bytes,
            ctype: str,
            *,
            headers: Optional[dict[str, str]] = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
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

        @staticmethod
        def _capture_frame() -> CapturedDesktopFrame:
            return _coerce_captured_frame(grab_fn())

        @staticmethod
        def _expected_geometry(value: object) -> FrameGeometry:
            return _frame_geometry_payload(value)

        @staticmethod
        def _require_geometry_match(
            expected: FrameGeometry, current: FrameGeometry
        ) -> None:
            if expected != current:
                raise AgentRequestError(
                    409,
                    "stale_geometry",
                    "virtual desktop geometry changed after frame capture",
                )

        def _prepare_direct_input(self, data: dict[str, Any]) -> dict[str, Any]:
            action = data.get("action")
            if action not in {"click", "drag"}:
                return data
            expected_value = data.get("expected_frame_geometry")
            expected_frame = data.get("expected_frame_sha256")
            if expected_value is None or expected_frame is None:
                raise AgentRequestError(
                    400,
                    "invalid_schema",
                    "coordinate input requires an exact frame and geometry",
                )
            if (
                not isinstance(expected_frame, str)
                or len(expected_frame) != 64
                or any(char not in "0123456789abcdef" for char in expected_frame)
            ):
                raise AgentRequestError(
                    400,
                    "invalid_schema",
                    "expected_frame_sha256 must be lowercase SHA-256",
                )
            expected = self._expected_geometry(expected_value)
            allowed = set(data) - {
                "expected_frame_geometry",
                "expected_frame_sha256",
            }
            prepared = {key: data[key] for key in allowed}
            current = self._capture_frame()
            if not hmac.compare_digest(
                hashlib.sha256(current.png).hexdigest(), expected_frame
            ):
                raise AgentRequestError(
                    409, "stale_frame", "desktop frame changed before pointer input"
                )
            self._require_geometry_match(expected, current.geometry)
            prepared["frame_geometry"] = current.geometry.to_payload()
            return prepared

        def _prepare_uia(
            self, operation: str, data: dict[str, Any]
        ) -> tuple[dict[str, Any], Optional[FrameGeometry]]:
            if operation == "act":
                return data, None
            geometry_value = data.get("frame_geometry")
            if geometry_value is None:
                raise AgentRequestError(
                    400,
                    "invalid_schema",
                    "UIA frame operation requires frame_geometry",
                )
            expected = self._expected_geometry(geometry_value)
            current = self._capture_frame().geometry
            self._require_geometry_match(expected, current)
            prepared = {
                key: value for key, value in data.items() if key != "frame_geometry"
            }
            if operation in {"locator-at", "text-at-point", "focused-at-point"}:
                frame_x = _bounded_int(prepared.get("x"), "x")
                frame_y = _bounded_int(prepared.get("y"), "y")
                try:
                    virtual_x, virtual_y = current.frame_to_virtual(frame_x, frame_y)
                except ValueError as exc:
                    raise AgentRequestError(400, "invalid_schema", str(exc)) from exc
                prepared["x"] = virtual_x
                prepared["y"] = virtual_y
            return prepared, current

        @staticmethod
        def _map_uia_result(
            operation: str,
            result: dict[str, Any],
            geometry: Optional[FrameGeometry],
        ) -> dict[str, Any]:
            if operation != "find" or geometry is None:
                return result
            candidates = result.get("candidates")
            if not isinstance(candidates, list):
                return result
            mapped_candidates: list[object] = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    mapped_candidates.append(candidate)
                    continue
                mapped = dict(candidate)
                point = candidate.get("point")
                bounds = candidate.get("bounds")
                try:
                    if (
                        isinstance(point, list)
                        and len(point) == 2
                        and all(
                            isinstance(item, int) and not isinstance(item, bool)
                            for item in point
                        )
                    ):
                        frame_point = geometry.virtual_to_frame(point[0], point[1])
                        mapped["point"] = [frame_point[0], frame_point[1]]
                    if (
                        isinstance(bounds, list)
                        and len(bounds) == 4
                        and all(
                            isinstance(item, int) and not isinstance(item, bool)
                            for item in bounds
                        )
                    ):
                        left = bounds[0] - geometry.origin_x
                        top = bounds[1] - geometry.origin_y
                        right = bounds[2] - geometry.origin_x
                        bottom = bounds[3] - geometry.origin_y
                        if not (
                            0 <= left < right <= geometry.width
                            and 0 <= top < bottom <= geometry.height
                        ):
                            raise ValueError(
                                "UIA bounds are outside the captured frame"
                            )
                        mapped["bounds"] = [left, top, right, bottom]
                except ValueError as exc:
                    raise AgentRequestError(
                        409, "stale_geometry", "UIA geometry is outside the frame"
                    ) from exc
                mapped_candidates.append(mapped)
            return {**result, "candidates": mapped_candidates}

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
                        "capabilities": [
                            "screenshot",
                            "context_identity_v1",
                            "typed_input_v1",
                            "guarded_input_v1",
                            "frame_geometry_v1",
                            "frame_observation_v1",
                            "uia_v1",
                            *(["legacy_exec"] if config.allow_legacy_exec else []),
                        ],
                    },
                )
                return
            if self.path == "/screenshot":
                if not self._authorized():
                    self._reject_unauthorized()
                    return
                try:
                    frame = self._capture_frame()
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
                self._send(
                    200,
                    frame.png,
                    "image/png",
                    headers={
                        _FRAME_GEOMETRY_HEADER: encode_frame_geometry_header(
                            frame.geometry
                        ),
                        _FRAME_BINDING_HEADER: frame_binding_sha256(
                            frame.png, frame.geometry
                        ),
                    },
                )
                return
            self._send_json(404, {"status": "error", "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            typed_routes = {
                "/input": ("input", ""),
                "/input/guarded": ("guarded-input", ""),
                "/context/identity": ("context", "identity"),
                "/uia/locator-at": ("uia", "locator-at"),
                "/uia/text-at-point": ("uia", "text-at-point"),
                "/uia/find": ("uia", "find"),
                "/uia/act": ("uia", "act"),
            }
            is_legacy = self.path == "/execute_windows" and config.allow_legacy_exec
            if self.path not in typed_routes and not is_legacy:
                self._send_json(404, {"status": "error", "error": "not found"})
                return
            if not self._authorized():
                self._reject_unauthorized()
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "code": "invalid_content_length",
                        "error": "Content-Length must be an integer",
                    },
                )
                return
            if length < 0:
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "code": "invalid_content_length",
                        "error": "Content-Length must be non-negative",
                    },
                )
                return
            if length > _MAX_BODY_BYTES:
                self._send_json(
                    413,
                    {
                        "status": "error",
                        "code": "body_too_large",
                        "error": "request body exceeds the bounded contract",
                    },
                )
                return
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw or b"{}")
            except Exception:  # noqa: BLE001
                self._send_json(400, {"status": "error", "error": "invalid JSON body"})
                return
            if not isinstance(data, dict):
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "code": "invalid_schema",
                        "error": "body must be an object",
                    },
                )
                return
            if self.path in typed_routes:
                kind, operation = typed_routes[self.path]
                try:
                    if kind == "context":
                        _exact_object(data, label="context identity request")
                    if kind == "guarded-input":
                        guarded = _exact_object(
                            data,
                            required=frozenset(
                                {
                                    "expected_frame_sha256",
                                    "expected_frame_geometry",
                                    "expected_context",
                                    "input",
                                }
                            ),
                            optional=frozenset({"focus_point"}),
                            label="guarded input request",
                        )
                        expected_frame = guarded["expected_frame_sha256"]
                        if (
                            not isinstance(expected_frame, str)
                            or len(expected_frame) != 64
                            or any(
                                char not in "0123456789abcdef"
                                for char in expected_frame
                            )
                        ):
                            raise AgentRequestError(
                                400,
                                "invalid_schema",
                                "expected_frame_sha256 must be lowercase SHA-256",
                            )
                        expected_geometry = self._expected_geometry(
                            guarded["expected_frame_geometry"]
                        )
                        expected_context = _exact_object(
                            guarded["expected_context"],
                            required=frozenset(
                                {"application", "session", "workflow_state"}
                            ),
                            label="expected_context",
                        )
                        for key, value in expected_context.items():
                            if value is not None and (
                                not isinstance(value, str) or not 1 <= len(value) <= 128
                            ):
                                raise AgentRequestError(
                                    400,
                                    "invalid_schema",
                                    f"expected_context.{key} is invalid",
                                )
                        input_payload = _exact_object(
                            guarded["input"],
                            optional=frozenset(
                                {
                                    "action",
                                    "x",
                                    "y",
                                    "double",
                                    "button",
                                    "end_x",
                                    "end_y",
                                    "text",
                                    "interval_s",
                                    "keys",
                                    "horizontal_notches",
                                    "vertical_notches",
                                }
                            ),
                            label="guarded input payload",
                        )
                        try:
                            current_frame = self._capture_frame()
                        except Exception as exc:
                            raise AgentRequestError(
                                503,
                                "capture_unavailable",
                                "guarded frame capture is unavailable",
                            ) from exc
                        if not hmac.compare_digest(
                            hashlib.sha256(current_frame.png).hexdigest(),
                            expected_frame,
                        ):
                            raise AgentRequestError(
                                409,
                                "stale_frame",
                                "desktop frame changed after identity verification",
                            )
                        self._require_geometry_match(
                            expected_geometry, current_frame.geometry
                        )
                        current_context = context_fn()
                        for key, expected in expected_context.items():
                            if current_context.get(key) != expected:
                                raise AgentRequestError(
                                    409,
                                    "stale_context",
                                    "execution context changed after identity "
                                    "verification",
                                )
                        focus_point = guarded.get("focus_point")
                        action = input_payload.get("action")
                        if action in {"type_text", "press"}:
                            focus = _exact_object(
                                focus_point,
                                required=frozenset({"x", "y"}),
                                label="focus_point",
                            )
                            try:
                                virtual_focus = current_frame.geometry.frame_to_virtual(
                                    _bounded_int(focus["x"], "focus_point.x"),
                                    _bounded_int(focus["y"], "focus_point.y"),
                                )
                            except ValueError as exc:
                                raise AgentRequestError(
                                    400, "invalid_schema", str(exc)
                                ) from exc
                            focus_result = uia_fn(
                                "focused-at-point",
                                {
                                    "x": virtual_focus[0],
                                    "y": virtual_focus[1],
                                },
                            )
                            if focus_result.get("focused") is not True:
                                raise AgentRequestError(
                                    409,
                                    "stale_focus",
                                    "focused UIA target changed after identity "
                                    "verification",
                                )
                        elif focus_point is not None:
                            raise AgentRequestError(
                                400,
                                "invalid_schema",
                                "focus_point is only valid for keyboard input",
                            )
                        result = input_fn(
                            {
                                **input_payload,
                                "frame_geometry": current_frame.geometry.to_payload(),
                            }
                            if action in {"click", "drag"}
                            else input_payload
                        )
                    else:
                        if kind == "input":
                            result = input_fn(self._prepare_direct_input(data))
                        elif kind == "context":
                            result = context_fn()
                        else:
                            prepared, geometry = self._prepare_uia(operation, data)
                            result = self._map_uia_result(
                                operation, uia_fn(operation, prepared), geometry
                            )
                except AgentRequestError as exc:
                    self._send_json(
                        exc.status,
                        {"status": "error", "code": exc.code, "error": str(exc)},
                    )
                    return
                except Exception:  # noqa: BLE001 - bounded generic error, no traceback/PHI
                    self._send_json(
                        500,
                        {
                            "status": "error",
                            "code": "operation_failed",
                            "error": "typed agent operation failed",
                        },
                    )
                    return
                self._send_json(200, result)
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
    input_fn: InputFn = _perform_input,
    uia_fn: UiaFn = _perform_uia,
    context_fn: ContextFn = _execution_context_identity,
) -> HTTPServer:
    """Build (but do not start) the COM-affine agent HTTP server.

    Args:
        config: Bind/auth configuration (defaults to loopback, no token).
        grab_fn: Desktop-capture callable returning PNG bytes (injectable for
            tests).

    Returns:
        An ``HTTPServer`` bound to ``config.host:config.port``. When ``config``
        carries a cert/key pair the listening socket is wrapped in TLS (the
        server speaks HTTPS). Requests are serialized on the serve thread
        because UIA controls are COM-apartment-bound.
    """
    config = config or AgentConfig()
    handler = make_handler_class(config, grab_fn, input_fn, uia_fn, context_fn)
    # UIAutomation controls and patterns are apartment/thread-affine. A
    # single-threaded HTTPServer keeps every request on the serve_forever
    # thread; _perform_uia initializes that thread's COM apartment per request.
    server = HTTPServer((config.host, config.port), handler)
    if config.tls_enabled():
        # Keep this deployed script genuinely self-contained: launch_agent()
        # copies only server.py into the guest, not the openadapt_flow package.
        # The control plane mints the cert; the guest needs stdlib ssl only.
        import ssl  # noqa: PLC0415

        assert config.certfile is not None and config.keyfile is not None
        for path in (config.certfile, config.keyfile):
            if not os.path.exists(path):
                raise FileNotFoundError(f"TLS material not found: {path}")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(config.certfile, config.keyfile)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server


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
    parser.add_argument(
        "--certfile",
        default=os.environ.get(CERTFILE_ENV_VAR),
        help=(
            "PEM certificate; with --keyfile serves HTTPS (encrypt PHI in "
            f"transit). Falls back to ${CERTFILE_ENV_VAR}."
        ),
    )
    parser.add_argument(
        "--keyfile",
        default=os.environ.get(KEYFILE_ENV_VAR),
        help=f"PEM private key matching --certfile (falls back to ${KEYFILE_ENV_VAR})",
    )
    parser.add_argument(
        "--allow-legacy-exec",
        action="store_true",
        help="DEVELOPMENT ONLY: expose the arbitrary-Python compatibility route",
    )
    args = parser.parse_args(argv)
    config = AgentConfig(
        host=args.host,
        port=args.port,
        token=args.token,
        certfile=args.certfile,
        keyfile=args.keyfile,
        allow_legacy_exec=args.allow_legacy_exec,
    )
    server = create_server(config)
    scheme = "https" if config.tls_enabled() else "http"
    print(
        f"[win-agent] listening on {scheme}://{config.host}:{config.port} "
        f"(tls={'on' if config.tls_enabled() else 'OFF'}, "
        f"auth={'on' if config.authed() else 'OFF'}, "
        f"legacy_exec={'on' if config.allow_legacy_exec else 'OFF'}, "
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

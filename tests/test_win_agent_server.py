"""Roundtrip tests for the in-guest win_agent server (no live VM/desktop).

The stdlib server is started on a loopback ephemeral port and exercised with
real ``requests`` calls, so the full HTTP contract WindowsBackend depends on is
proven end to end. The desktop grabber is injected (a fake PNG) so no mss / no
real desktop is needed — the suite runs on macOS/Linux CI.
"""

from __future__ import annotations

import hashlib
import struct
import sys
import threading
import types
from collections.abc import Iterator

import pytest
import requests

from openadapt_flow.backend import (
    DisplayTopologyChanged,
    FrameObservationBackend,
    FreshActuationRequired,
)
from openadapt_flow.backends.win_agent import AgentConfig, create_server
from openadapt_flow.backends.win_agent import server as win_agent_server
from openadapt_flow.backends.win_agent.server import (
    AgentRequestError,
    CapturedDesktopFrame,
    FrameGeometry,
    MonitorGeometry,
    _normalize_virtual_point,
    _perform_input,
    _perform_uia,
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _test_context(
    *,
    application: str = "accuro",
    session: str = "a" * 64,
    window_id: str = "4096",
    pid: int = 321,
    process_start_time: str = "133801632000000000",
) -> dict:
    return {
        "status": "ok",
        "application": application,
        "session": session,
        "workflow_state": None,
        "window": {
            "window_id": window_id,
            "pid": pid,
            "process_start_time": process_start_time,
            "owner": application,
        },
    }


def _fake_png() -> bytes:
    """Minimal valid-enough PNG: signature + IHDR with a 4x2 size."""
    ihdr = struct.pack(">II", 4, 2)
    return _PNG_SIGNATURE + b"\x00\x00\x00\x0dIHDR" + ihdr + b"\x00" * 8


def _fake_png_size(width: int, height: int) -> bytes:
    ihdr = struct.pack(">II", width, height)
    return _PNG_SIGNATURE + b"\x00\x00\x00\x0dIHDR" + ihdr + b"\x00" * 8


def _geometry(
    *,
    origin_x: int = 0,
    origin_y: int = 0,
    width: int = 4,
    height: int = 2,
    dpi: int = 96,
    device: str = "DISPLAY1",
) -> FrameGeometry:
    return FrameGeometry(
        origin_x=origin_x,
        origin_y=origin_y,
        width=width,
        height=height,
        monitors=(
            MonitorGeometry(
                device=device,
                left=origin_x,
                top=origin_y,
                width=width,
                height=height,
                dpi_x=dpi,
                dpi_y=dpi,
                primary=True,
            ),
        ),
    )


def _negative_origin_geometry() -> FrameGeometry:
    return FrameGeometry.from_payload(
        {
            "version": 1,
            "coordinate_space": "physical_virtual_desktop",
            "dpi_awareness": "per_monitor_v2",
            "origin_x": -1920,
            "origin_y": -200,
            "width": 4480,
            "height": 1640,
            "monitors": [
                {
                    "device": "DISPLAY2",
                    "left": -1920,
                    "top": -200,
                    "width": 1920,
                    "height": 1080,
                    "dpi_x": 144,
                    "dpi_y": 144,
                    "primary": False,
                },
                {
                    "device": "DISPLAY1",
                    "left": 0,
                    "top": 0,
                    "width": 2560,
                    "height": 1440,
                    "dpi_x": 192,
                    "dpi_y": 192,
                    "primary": True,
                },
            ],
        }
    )


class RunningAgent:
    """A started agent server plus its base URL (context-managed)."""

    def __init__(
        self,
        config: AgentConfig,
        grab_fn=_fake_png,
        *,
        input_fn=None,
        uia_fn=None,
        context_fn=None,
    ) -> None:
        kwargs = {"grab_fn": grab_fn}
        if input_fn is not None:
            kwargs["input_fn"] = input_fn
        if uia_fn is not None:
            kwargs["uia_fn"] = uia_fn
        if context_fn is not None:
            kwargs["context_fn"] = context_fn
        self.server = create_server(config, **kwargs)
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def agent() -> Iterator[RunningAgent]:
    a = RunningAgent(AgentConfig(host="127.0.0.1", port=0, allow_legacy_exec=True))
    yield a
    a.close()


@pytest.fixture()
def authed_agent() -> Iterator[RunningAgent]:
    a = RunningAgent(
        AgentConfig(
            host="127.0.0.1",
            port=0,
            token="s3cret",
            allow_legacy_exec=True,
        )
    )
    yield a
    a.close()


@pytest.fixture()
def typed_agent() -> Iterator[RunningAgent]:
    def input_fn(payload):
        return {
            "status": "delivered",
            "receipt_id": "input-1",
            "operation": f"physical_{payload['action']}",
            "native": False,
            "target_fingerprint": None,
            "delivered_at": "2026-07-17T00:00:00+00:00",
            "outcome_verified": False,
        }

    def uia_fn(operation, payload):
        if operation == "focused-at-point":
            return {"status": "ok", "focused": True}
        if operation == "find":
            return {
                "status": "ok",
                "match": "ambiguous",
                "candidate_count": 2,
                "truncated": False,
                "candidates": [{"fingerprint": "a" * 64}, {"fingerprint": "b" * 64}],
            }
        if operation == "act":
            return {
                "status": "ok",
                "candidate_count": 1,
                "receipt": {
                    "status": "delivered",
                    "receipt_id": "uia-1",
                    "operation": "uia_invoke",
                    "native": True,
                    "target_fingerprint": "a" * 64,
                    "delivered_at": "2026-07-17T00:00:00+00:00",
                    "outcome_verified": False,
                },
            }
        return {"status": "ok", "locator": None, "text": None}

    a = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        input_fn=input_fn,
        uia_fn=uia_fn,
        context_fn=_test_context,
    )
    yield a
    a.close()


# -- health -------------------------------------------------------------------


def test_health_ok_and_unauthenticated(agent: RunningAgent) -> None:
    r = requests.get(f"{agent.url}/health", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["auth_required"] is False


def test_default_agent_disables_arbitrary_exec_and_advertises_typed_contract(
    typed_agent: RunningAgent,
) -> None:
    health = requests.get(f"{typed_agent.url}/health", timeout=5).json()
    assert "context_identity_v1" in health["capabilities"]
    assert "typed_input_v1" in health["capabilities"]
    assert "uia_v1" in health["capabilities"]
    assert "frame_observation_v1" in health["capabilities"]
    assert "legacy_exec" not in health["capabilities"]
    response = requests.post(
        f"{typed_agent.url}/execute_windows",
        json={"command": "print('must not execute')"},
        timeout=5,
    )
    assert response.status_code == 404


def test_typed_input_and_uia_receipts_never_claim_outcome(
    typed_agent: RunningAgent,
) -> None:
    context = requests.post(
        f"{typed_agent.url}/context/identity",
        json={},
        timeout=5,
    )
    assert context.status_code == 200
    assert set(context.json()) == {
        "status",
        "application",
        "session",
        "workflow_state",
        "window",
    }
    assert "title" not in context.text.casefold()
    expected_echo = requests.post(
        f"{typed_agent.url}/context/identity",
        json={"expected": "accuro"},
        timeout=5,
    )
    assert expected_echo.status_code == 400

    delivered = requests.post(
        f"{typed_agent.url}/input",
        json={
            "action": "click",
            "x": 1,
            "y": 1,
            "double": False,
            "expected_frame_sha256": hashlib.sha256(_fake_png()).hexdigest(),
            "expected_frame_geometry": _geometry().to_payload(),
        },
        timeout=5,
    )
    assert delivered.status_code == 200
    assert delivered.json()["outcome_verified"] is False

    found = requests.post(
        f"{typed_agent.url}/uia/find",
        json={
            "locator": {"automation_id": "duplicate"},
            "frame_geometry": _geometry().to_payload(),
        },
        timeout=5,
    ).json()
    assert found["match"] == "ambiguous"
    assert found["candidate_count"] == 2

    acted = requests.post(
        f"{typed_agent.url}/uia/act",
        json={
            "locator": {"automation_id": "save"},
            "expected_fingerprint": "a" * 64,
            "operation": "click",
        },
        timeout=5,
    ).json()
    assert acted["receipt"]["native"] is True
    assert acted["receipt"]["outcome_verified"] is False


def test_windows_backend_guarded_key_roundtrip(typed_agent: RunningAgent) -> None:
    from openadapt_flow.backends import WindowsBackend

    backend = WindowsBackend(typed_agent.url, viewport=(4, 2))
    observation = backend.acquire_actuation_observation()
    backend.arm_guarded_keyboard(1, 1)
    backend.bind_input_observation(observation)
    receipt = backend.press_guarded(
        "Enter",
        expected_frame_sha256=observation.frame_sha256,
    )

    assert receipt.operation == "physical_press"
    assert receipt.outcome_verified is False


def test_windows_atomic_observation_maps_negative_origin_monitor_topology() -> None:
    from openadapt_flow.backends import WindowsBackend

    geometry = _negative_origin_geometry()
    frame = CapturedDesktopFrame(
        _fake_png_size(geometry.width, geometry.height),
        geometry,
    )
    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        grab_fn=lambda: frame,
        context_fn=_test_context,
    )
    try:
        backend = WindowsBackend(agent.url)
        assert isinstance(backend, FrameObservationBackend)

        observation = backend.observe_frame()

        assert observation.viewport == (4480, 1640)
        assert observation.origin == (-1920.0, -200.0)
        assert observation.display_id == "windows-virtual-desktop"
        assert observation.display_bounds == (-1920.0, -200.0, 4480.0, 1640.0)
        assert observation.scale == (1.0, 1.0)
        assert len(observation.topology_sha256) == 64
        assert len(observation.window_identity_sha256) == 64
        assert len(observation.session_identity_sha256) == 64
    finally:
        agent.close()


def test_windows_guarded_content_change_raises_fresh_before_input() -> None:
    from openadapt_flow.backends import WindowsBackend

    state = {"frame": CapturedDesktopFrame(_fake_png(), _geometry())}
    delivered: list[dict] = []

    def input_fn(payload):
        delivered.append(payload)
        return {
            "status": "delivered",
            "receipt_id": "must-not-deliver",
            "operation": "physical_press",
            "native": False,
            "target_fingerprint": None,
            "delivered_at": "2026-08-20T00:00:00+00:00",
            "outcome_verified": False,
        }

    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        grab_fn=lambda: state["frame"],
        input_fn=input_fn,
        uia_fn=lambda operation, payload: {"status": "ok", "focused": True},
        context_fn=_test_context,
    )
    try:
        backend = WindowsBackend(agent.url)
        expected = backend.acquire_actuation_observation()
        backend.arm_guarded_keyboard(1, 1)
        backend.bind_input_observation(expected)
        state["frame"] = CapturedDesktopFrame(
            _fake_png() + b"changed-after-identity",
            _geometry(),
        )

        with pytest.raises(FreshActuationRequired) as error:
            backend.press_guarded(
                "Enter",
                expected_frame_sha256=expected.frame_sha256,
            )

        assert error.value.expected_observation is expected
        assert error.value.observed_observation is not None
        assert error.value.observed_observation.geometry_epoch == (
            expected.geometry_epoch
        )
        assert error.value.observed_observation.frame_sha256 != expected.frame_sha256
        assert delivered == []
    finally:
        agent.close()


def test_windows_guarded_topology_change_is_not_a_blind_retry() -> None:
    from openadapt_flow.backends import WindowsBackend

    original = CapturedDesktopFrame(_fake_png(), _geometry())
    state = {"frame": original}
    delivered: list[dict] = []
    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        grab_fn=lambda: state["frame"],
        input_fn=lambda payload: delivered.append(payload) or {},
        uia_fn=lambda operation, payload: {"status": "ok", "focused": True},
        context_fn=_test_context,
    )
    try:
        backend = WindowsBackend(agent.url)
        expected = backend.acquire_actuation_observation()
        backend.arm_guarded_keyboard(1, 1)
        backend.bind_input_observation(expected)
        changed_geometry = _geometry(width=6, height=3)
        state["frame"] = CapturedDesktopFrame(
            _fake_png_size(6, 3),
            changed_geometry,
        )

        with pytest.raises(DisplayTopologyChanged):
            backend.press_guarded(
                "Enter",
                expected_frame_sha256=expected.frame_sha256,
            )

        assert delivered == []
    finally:
        agent.close()


@pytest.mark.parametrize("mutation", ["frame", "context", "focus"])
def test_guarded_input_refuses_post_identity_change(mutation: str) -> None:
    state = {
        "frame": _fake_png(),
        "application": "accuro",
        "focused": True,
    }
    delivered: list[dict] = []

    def grab_fn():
        return state["frame"]

    def input_fn(payload):
        delivered.append(payload)
        return {
            "status": "delivered",
            "receipt_id": "guarded-1",
            "operation": "physical_press",
            "native": False,
            "target_fingerprint": None,
            "delivered_at": "2026-07-25T00:00:00+00:00",
            "outcome_verified": False,
        }

    def context_fn():
        return {
            "status": "ok",
            "application": state["application"],
            "session": "a" * 64,
            "workflow_state": None,
        }

    def uia_fn(operation, payload):
        assert operation == "focused-at-point"
        return {"status": "ok", "focused": state["focused"]}

    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        grab_fn=grab_fn,
        input_fn=input_fn,
        uia_fn=uia_fn,
        context_fn=context_fn,
    )
    try:
        expected_frame = hashlib.sha256(state["frame"]).hexdigest()
        if mutation == "frame":
            state["frame"] += b"changed"
        elif mutation == "context":
            state["application"] = "other-app"
        else:
            state["focused"] = False
        response = requests.post(
            f"{agent.url}/input/guarded",
            json={
                "expected_frame_sha256": expected_frame,
                "expected_frame_geometry": _geometry().to_payload(),
                "expected_context": {
                    "application": "accuro",
                    "session": "a" * 64,
                    "workflow_state": None,
                },
                "focus_point": {"x": 1, "y": 1},
                "input": {"action": "press", "keys": ["enter"]},
            },
            timeout=5,
        )
    finally:
        agent.close()

    assert response.status_code == 409
    assert delivered == []


def test_guarded_input_refuses_geometry_change_with_no_input() -> None:
    old_geometry = _geometry(dpi=96)
    new_geometry = _geometry(dpi=144)
    state = {
        "frame": CapturedDesktopFrame(_fake_png(), old_geometry),
    }
    delivered: list[dict] = []

    def input_fn(payload):
        delivered.append(payload)
        return {
            "status": "delivered",
            "receipt_id": "must-not-deliver",
            "operation": "physical_click",
            "native": False,
            "target_fingerprint": None,
            "delivered_at": "2026-08-20T00:00:00+00:00",
            "outcome_verified": False,
        }

    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        grab_fn=lambda: state["frame"],
        input_fn=input_fn,
        context_fn=lambda: {
            "status": "ok",
            "application": "accuro",
            "session": "a" * 64,
            "workflow_state": None,
        },
    )
    try:
        expected_frame = hashlib.sha256(state["frame"].png).hexdigest()
        state["frame"] = CapturedDesktopFrame(_fake_png(), new_geometry)
        response = requests.post(
            f"{agent.url}/input/guarded",
            json={
                "expected_frame_sha256": expected_frame,
                "expected_frame_geometry": old_geometry.to_payload(),
                "expected_context": {
                    "application": "accuro",
                    "session": "a" * 64,
                    "workflow_state": None,
                },
                "input": {"action": "click", "x": 1, "y": 1},
            },
            timeout=5,
        )
    finally:
        agent.close()

    assert response.status_code == 409
    assert response.json()["code"] == "stale_geometry"
    assert delivered == []


@pytest.mark.parametrize(
    ("current_frame", "expected_code"),
    [
        (CapturedDesktopFrame(_fake_png() + b"changed", _geometry()), "stale_frame"),
        (CapturedDesktopFrame(_fake_png(), _geometry(dpi=144)), "stale_geometry"),
    ],
)
def test_direct_pointer_refuses_frame_or_geometry_mismatch_with_no_input(
    current_frame: CapturedDesktopFrame,
    expected_code: str,
) -> None:
    captured = CapturedDesktopFrame(_fake_png(), _geometry())
    delivered: list[dict] = []
    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        grab_fn=lambda: current_frame,
        input_fn=lambda payload: delivered.append(payload) or {},
    )
    try:
        response = requests.post(
            f"{agent.url}/input",
            json={
                "action": "click",
                "x": 1,
                "y": 1,
                "expected_frame_sha256": hashlib.sha256(captured.png).hexdigest(),
                "expected_frame_geometry": captured.geometry.to_payload(),
            },
            timeout=5,
        )
    finally:
        agent.close()

    assert response.status_code == 409
    assert response.json()["code"] == expected_code
    assert delivered == []


def test_backend_refreshes_viewport_from_each_bound_frame() -> None:
    state = {
        "frame": CapturedDesktopFrame(_fake_png_size(4, 2), _geometry()),
    }
    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0),
        grab_fn=lambda: state["frame"],
    )
    try:
        from openadapt_flow.backends import WindowsBackend

        backend = WindowsBackend(agent.url)
        backend.screenshot()
        assert backend.viewport == (4, 2)
        state["frame"] = CapturedDesktopFrame(
            _fake_png_size(6, 3), _geometry(width=6, height=3)
        )
        backend.screenshot()
        assert backend.viewport == (6, 3)
    finally:
        agent.close()


def test_invalid_input_schema_refuses_before_loading_pyautogui(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pyautogui", None)
    with pytest.raises(AgentRequestError) as caught:
        _perform_input({"action": "click", "x": 1, "y": 2, "unknown": True})
    assert caught.value.status == 400
    assert caught.value.code == "invalid_schema"


def test_pointer_input_maps_negative_frame_origin_through_sendinput(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    geometry = _negative_origin_geometry()
    sent: list[tuple[list[tuple[int, int, int]], FrameGeometry]] = []
    monkeypatch.setattr(win_agent_server, "_current_frame_geometry", lambda: geometry)
    monkeypatch.setattr(
        win_agent_server,
        "_send_virtual_pointer_sequence",
        lambda events, current: sent.append((events, current)),
    )

    receipt = _perform_input(
        {
            "action": "click",
            "x": 100,
            "y": 250,
            "double": False,
            "button": "left",
            "frame_geometry": geometry.to_payload(),
        }
    )

    assert receipt["operation"] == "physical_click"
    assert sent == [
        (
            [(-1820, 50, 0x0002), (-1820, 50, 0x0004)],
            geometry,
        )
    ]
    assert _normalize_virtual_point(-1820, 50, geometry) == (
        round(100 * 65535 / 4479),
        round(250 * 65535 / 1639),
    )


def test_pointer_input_maps_a_secondary_monitor_drag_through_sendinput(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    geometry = _negative_origin_geometry()
    sent: list[list[tuple[int, int, int]]] = []
    monkeypatch.setattr(win_agent_server, "_current_frame_geometry", lambda: geometry)
    monkeypatch.setattr(
        win_agent_server,
        "_send_virtual_pointer_sequence",
        lambda events, _geometry: sent.append(events),
    )

    _perform_input(
        {
            "action": "drag",
            "x": 2000,
            "y": 300,
            "end_x": 4200,
            "end_y": 1200,
            "frame_geometry": geometry.to_payload(),
        }
    )

    assert sent == [
        [
            (80, 100, 0),
            (80, 100, 0x0002),
            (2280, 1000, 0),
            (2280, 1000, 0x0004),
        ]
    ]


def test_pointer_input_refuses_topology_or_dpi_change_before_sendinput(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _negative_origin_geometry()
    changed_payload = captured.to_payload()
    changed_payload["monitors"][0]["dpi_x"] = 192
    changed_payload["monitors"][0]["dpi_y"] = 192
    changed = FrameGeometry.from_payload(changed_payload)
    sent: list[object] = []
    monkeypatch.setattr(win_agent_server, "_current_frame_geometry", lambda: changed)
    monkeypatch.setattr(
        win_agent_server,
        "_send_virtual_pointer_sequence",
        lambda *_args: sent.append(object()),
    )

    with pytest.raises(AgentRequestError) as caught:
        _perform_input(
            {
                "action": "click",
                "x": 100,
                "y": 250,
                "frame_geometry": captured.to_payload(),
            }
        )

    assert caught.value.status == 409
    assert caught.value.code == "stale_geometry"
    assert sent == []


class _FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class _FakeControl:
    def __init__(
        self,
        control_type: str,
        *,
        automation_id: str = "",
        name: str = "",
        runtime_id: tuple[int, ...] = (),
        bounds: tuple[int, int, int, int] = (0, 0, 100, 40),
        parent=None,
    ) -> None:
        self.ControlTypeName = control_type
        self.AutomationId = automation_id
        self.Name = name
        self.ClassName = "WindowsForms10.TEST"
        self.ProcessId = 1234
        self.NativeWindowHandle = 0
        self.BoundingRectangle = _FakeRect(*bounds)
        self.runtime_id = runtime_id
        self.parent = parent
        self.children = []
        self.invocations = 0

    def GetRuntimeId(self):
        return list(self.runtime_id)

    def GetParentControl(self):
        return self.parent

    def GetChildren(self):
        return self.children

    def GetInvokePattern(self):
        return self

    def Invoke(self):
        self.invocations += 1


class _FakeUiaContext:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _fake_uia_module(*controls: _FakeControl):
    root = _FakeControl("PaneControl")
    window = _FakeControl(
        "WindowControl",
        name="Patient Notes",
        runtime_id=(42, 1),
        bounds=(0, 0, 800, 600),
        parent=root,
    )
    root.children = [window]
    window.children = list(controls)
    for control in controls:
        control.parent = window
    return types.SimpleNamespace(
        GetRootControl=lambda: root,
        ControlFromPoint=lambda _x, _y: controls[0] if controls else None,
        UIAutomationInitializerInThread=_FakeUiaContext,
    )


def test_real_uia_contract_refuses_stale_target_before_native_action(
    monkeypatch,
) -> None:
    button = _FakeControl(
        "ButtonControl",
        automation_id="saveButton",
        name="Save Note",
        runtime_id=(42, 99),
        bounds=(500, 450, 600, 484),
    )
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(button))
    locator = {
        "automation_id": "saveButton",
        "role": "button",
        "name": "Save Note",
        "window_name": "Patient Notes",
    }
    found = _perform_uia("find", {"locator": locator})
    assert found["match"] == "unique"
    fingerprint = found["candidates"][0]["fingerprint"]

    button.runtime_id = (42, 100)  # same locator, replaced live element
    with pytest.raises(AgentRequestError) as caught:
        _perform_uia(
            "act",
            {
                "locator": locator,
                "operation": "click",
                "expected_fingerprint": fingerprint,
            },
        )
    assert caught.value.status == 409
    assert caught.value.code == "stale_target"
    assert button.invocations == 0


def test_real_uia_contract_refuses_duplicate_candidates_without_action(
    monkeypatch,
) -> None:
    controls = [
        _FakeControl(
            "ButtonControl",
            automation_id="saveButton",
            name="Save Note",
            runtime_id=(42, index),
            bounds=(500, 450 + index * 50, 600, 484 + index * 50),
        )
        for index in (1, 2)
    ]
    monkeypatch.setitem(sys.modules, "uiautomation", _fake_uia_module(*controls))
    locator = {"automation_id": "saveButton", "window_name": "Patient Notes"}
    found = _perform_uia("find", {"locator": locator})
    assert found["match"] == "ambiguous"
    assert found["candidate_count"] == 2
    with pytest.raises(AgentRequestError) as caught:
        _perform_uia(
            "act",
            {
                "locator": locator,
                "operation": "click",
                "expected_fingerprint": found["candidates"][0]["fingerprint"],
            },
        )
    assert caught.value.code == "ambiguous_target"
    assert [control.invocations for control in controls] == [0, 0]


def test_health_open_even_when_token_set(authed_agent: RunningAgent) -> None:
    # Liveness must not require the token (no desktop bytes, no exec).
    r = requests.get(f"{authed_agent.url}/health", timeout=5)
    assert r.status_code == 200
    assert r.json()["auth_required"] is True


def test_windows_context_identity_is_live_bounded_and_authenticated() -> None:
    state = {"application": "accuro", "session": "a" * 64}

    def context_fn():
        return {
            "status": "ok",
            **state,
            "workflow_state": None,
        }

    agent = RunningAgent(
        AgentConfig(host="127.0.0.1", port=0, token="secret"),
        context_fn=context_fn,
    )
    try:
        from openadapt_flow.backends import WindowsBackend

        backend = WindowsBackend(agent.url, auth_token="secret")
        assert backend.application_identity() == "accuro"
        assert backend.session_identity() == "a" * 64
        assert backend.workflow_state_identity() is None

        state.update(application="unrelated-app", session="b" * 64)
        assert backend.application_identity() == "unrelated-app"
        assert backend.session_identity() == "b" * 64

        state.update(application="Patient Alice Example", session="NOT-A-DIGEST")
        assert backend.application_identity() is None
        assert backend.session_identity() is None

        unauthenticated = WindowsBackend(agent.url)
        assert unauthenticated.application_identity() is None
        assert unauthenticated.session_identity() is None
    finally:
        agent.close()


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

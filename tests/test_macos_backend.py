from __future__ import annotations

import io

import pytest
from PIL import Image

from openadapt_flow.backend import (
    Backend,
    IdentityBackend,
    StructuralActionBackend,
)
from openadapt_flow.backends.factory import build_backend
from openadapt_flow.backends.macos_backend import MacOSBackend, MacOSBackendError
from openadapt_flow.backends.remote_display import WindowInfo
from openadapt_flow.deployment import BackendConfig


class FakeMacClient:
    def __init__(
        self,
        *,
        capture_trusted: bool = True,
        input_trusted: bool = True,
        windows: list[WindowInfo] | None = None,
        frontmost_window_id: int | None = 41,
    ) -> None:
        self._capture_trusted = capture_trusted
        self._input_trusted = input_trusted
        self.windows = windows or [
            WindowInfo(41, "TextEdit", "oa-trial.txt", 9001, (10, 20, 400, 300))
        ]
        self._frontmost_window_id = frontmost_window_id
        self.calls: list[tuple] = []

    def capture_trusted(self) -> bool:
        return self._capture_trusted

    def input_trusted(self) -> bool:
        return self._input_trusted

    def request_capture_access(self) -> bool:
        return self._capture_trusted

    def request_input_access(self) -> bool:
        return self._input_trusted

    def find_windows(self, owner_substr, title_substr):
        return [
            window
            for window in self.windows
            if owner_substr.lower() in window.owner.lower()
            and (title_substr is None or title_substr.lower() in window.title.lower())
        ]

    def find_window(self, owner_substr, title_substr):
        matches = self.find_windows(owner_substr, title_substr)
        return matches[0] if matches else None

    def capture(self, window_id):
        image = Image.new("RGB", (800, 600), (20, 30, 40))
        output = io.BytesIO()
        image.save(output, format="PNG")
        self.calls.append(("capture", window_id))
        return output.getvalue(), 800, 600

    def frontmost_pid(self):
        for window in self.windows:
            if window.window_id == self._frontmost_window_id:
                return window.pid
        return None

    def frontmost_window_id(self):
        return self._frontmost_window_id

    def activate(self, pid):
        self.calls.append(("activate", pid))

    def mouse(self, x, y, *, button, down, click_count):
        self.calls.append(("mouse", x, y, button, down, click_count))

    def mouse_move(self, x, y):
        self.calls.append(("move", x, y))

    def type_chars(self, text):
        raise AssertionError("native backend must not use guest-scancode text")

    def type_unicode(self, text):
        self.calls.append(("unicode", text))

    def key(self, keycode, *, down, flags):
        self.calls.append(("key", keycode, down, tuple(flags)))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))


def backend(client: FakeMacClient | None = None, **kwargs) -> MacOSBackend:
    return MacOSBackend(
        client or FakeMacClient(),
        app="TextEdit",
        window_title="oa-trial",
        settle_s=0,
        foreground_settle_s=0,
        **kwargs,
    )


def test_native_backend_is_typed_but_does_not_claim_unqualified_ax() -> None:
    target = backend()
    assert isinstance(target, Backend)
    assert not isinstance(target, IdentityBackend)
    assert not isinstance(target, StructuralActionBackend)


def test_target_window_capture_and_unicode_text() -> None:
    client = FakeMacClient()
    target = backend(client)
    assert target.viewport == (800, 600)
    target.type_text("Résumé — 東京")
    assert ("capture", 41) in client.calls
    assert ("unicode", "Résumé — 東京") in client.calls


def test_control_or_meta_and_meta_use_native_command() -> None:
    client = FakeMacClient()
    target = backend(client)
    target.press("ControlOrMeta+a")
    target.press("Meta+s")
    key_calls = [call for call in client.calls if call[0] == "key"]
    assert key_calls[0][1:] == (0x00, True, ("command",))
    assert key_calls[1][1:] == (0x00, False, ("command",))
    assert key_calls[2][1:] == (0x01, True, ("command",))
    assert key_calls[3][1:] == (0x01, False, ("command",))


def test_explicit_control_stays_control() -> None:
    client = FakeMacClient()
    backend(client).press("Control+a")
    key_calls = [call for call in client.calls if call[0] == "key"]
    assert all(call[3] == ("control",) for call in key_calls)


def test_ambiguous_selector_refuses_first_match_before_capture_or_input() -> None:
    windows = [
        WindowInfo(41, "TextEdit", "oa-trial-a.txt", 1, (0, 0, 400, 300)),
        WindowInfo(42, "TextEdit", "oa-trial-b.txt", 2, (0, 0, 400, 300)),
    ]
    client = FakeMacClient(windows=windows)
    target = backend(client)
    with pytest.raises(MacOSBackendError, match="ambiguous.*2 windows"):
        target.screenshot()
    assert not client.calls


def test_capture_and_input_permissions_fail_loud() -> None:
    capture_client = FakeMacClient(capture_trusted=False)
    with pytest.raises(MacOSBackendError, match="Screen Recording"):
        backend(capture_client).screenshot()
    assert not capture_client.calls

    input_client = FakeMacClient(input_trusted=False)
    with pytest.raises(MacOSBackendError, match="Accessibility"):
        backend(input_client).type_text("must not land")
    assert not input_client.calls


def test_input_refuses_when_exact_window_is_not_topmost() -> None:
    client = FakeMacClient(frontmost_window_id=999)
    with pytest.raises(MacOSBackendError, match="not the topmost window"):
        backend(client, foreground_retries=2).press("Enter")
    assert not any(call[0] in {"key", "unicode", "mouse"} for call in client.calls)


def test_factory_requires_app_and_threads_unique_title() -> None:
    with pytest.raises(ValueError, match="requires backend.macos_app"):
        build_backend(BackendConfig(kind="macos"))

    client = FakeMacClient()
    target = build_backend(
        BackendConfig(
            kind="macos",
            macos_app="TextEdit",
            macos_window_title="oa-trial",
        ),
        macos_client=client,
    )
    assert isinstance(target, MacOSBackend)
    assert target._owner_substr == "TextEdit"
    assert target._title_substr == "oa-trial"

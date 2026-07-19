"""Offline contract tests for the Windows-host remote-display WindowClient.

The entire Win32 layer is a scripted fake (:class:`FakeWin32Api`): no test here
touches ``ctypes.windll``, a live window, SendInput, or a Windows host — they
run identically on any platform (and on the ``windows-mock`` CI job's real
Windows runner, still fully mocked). What they pin down is the load-bearing
CLIENT logic and its conformance to the exact contract the macOS client
satisfies for :class:`RemoteDisplayBackend`:

* window selection — process-executable owner matching (capture 0.6.0
  convention, exact + ``.exe``-tolerant, never substring), exact
  case-insensitive titles, class filtering, cloaked/untitled skipping, and
  the AMBIGUITY HALT on duplicate matches (the identity discipline applied
  to windows);
* coordinate mapping — client-area origin + scale-1.0 physical pixels under
  per-monitor DPI awareness, plus the SendInput 0..65535 normalization math
  and its refusal to clamp out-of-desktop points;
* the focus-verification halt path — input refused unless the exact target
  HWND holds the foreground immediately before (and re-checked after
  blocking work by) each input burst;
* capture fallback ordering — PrintWindow first, BitBlt second, both failing
  is a loud typed error (as are gone/minimized windows and DPI virtualization);
* the UIPI elevation guard — synthetic input that Windows would silently
  discard is refused, never emitted.

HONESTY: these are mock-contract tests. They prove the client implements the
WindowClient seam correctly; they do NOT prove behavior on a real Windows
host (no counted qualification batch exists — see claims.yaml
``win32-window-replay-roadmap``).
"""

from __future__ import annotations

import sys

import pytest
from PIL import Image

from openadapt_flow.backends.remote_display import (
    RemoteDisplayBackend,
    RemoteDisplayError,
)
from openadapt_flow.backends.win32_window_client import (
    CaptureFailedError,
    DpiAwarenessError,
    InputDeliveryError,
    NativeWin32Api,
    Win32WindowClient,
    Win32WindowError,
    WindowGoneError,
    WindowMinimizedError,
    normalize_to_virtual_desktop,
    owner_matches_process,
)


class FakeWindow:
    """One scripted top-level window."""

    def __init__(
        self,
        hwnd: int,
        *,
        title: str = "Accuro - Citrix Workspace",
        cls: str = "Transparent Windows Client",
        pid: int = 4242,
        image: str = "wfica32.exe",
        bounds: tuple[float, float, float, float] = (300.0, 200.0, 1280.0, 800.0),
        visible: bool = True,
        cloaked: bool = False,
        iconic: bool = False,
    ) -> None:
        self.hwnd = hwnd
        self.title = title
        self.cls = cls
        self.pid = pid
        self.image = image
        self.bounds = bounds
        self.visible = visible
        self.cloaked = cloaked
        self.iconic = iconic


# A tiny US-layout VkKeyScanW stand-in for the fake API.
def _us_vk_for_char(ch: str):
    if len(ch) != 1:
        return None
    if ch.islower() and ch.isalpha() and ch.isascii():
        return 0x41 + (ord(ch) - ord("a")), False
    if ch.isupper() and ch.isalpha() and ch.isascii():
        return 0x41 + (ord(ch) - ord("A")), True
    if ch.isdigit():
        return 0x30 + int(ch), False
    if ch == "-":
        return 0xBD, False
    if ch == " ":
        return 0x20, False
    return None  # non-US char -> Unicode fallback


class FakeWin32Api:
    """A scripted :class:`Win32Api` that records every injected input."""

    def __init__(
        self,
        windows: list[FakeWindow] | None = None,
        *,
        dpi: str = "per-monitor-v2",
        foreground: int | None = None,
        print_window_works: bool = True,
        blt_works: bool = True,
        self_elev: bool | None = False,
        elevated_pids: set[int] | None = None,
        unknown_elevation_pids: set[int] | None = None,
    ) -> None:
        self.windows = windows if windows is not None else [FakeWindow(101)]
        self.dpi = dpi
        self.foreground = foreground if foreground is not None else self.windows[0].hwnd
        self.print_window_works = print_window_works
        self.blt_works = blt_works
        self.self_elev = self_elev
        self.elevated_pids = elevated_pids or set()
        self.unknown_elevation_pids = unknown_elevation_pids or set()
        self.point_hits: dict[tuple[int, int], int] = {}
        self.calls: list[tuple] = []

    def _find(self, hwnd: int) -> FakeWindow | None:
        for w in self.windows:
            if w.hwnd == hwnd:
                return w
        return None

    # -- Win32Api ------------------------------------------------------------

    def ensure_dpi_awareness(self) -> str:
        self.calls.append(("dpi",))
        return self.dpi

    def enum_top_level_windows(self):
        return [w.hwnd for w in self.windows]

    def is_window(self, hwnd):
        return self._find(hwnd) is not None

    def is_window_visible(self, hwnd):
        w = self._find(hwnd)
        return w is not None and w.visible

    def is_iconic(self, hwnd):
        w = self._find(hwnd)
        return w is not None and w.iconic

    def is_cloaked(self, hwnd):
        w = self._find(hwnd)
        return w is not None and w.cloaked

    def window_title(self, hwnd):
        w = self._find(hwnd)
        return w.title if w else ""

    def window_class(self, hwnd):
        w = self._find(hwnd)
        return w.cls if w else ""

    def window_pid(self, hwnd):
        w = self._find(hwnd)
        return w.pid if w else 0

    def process_image_basename(self, pid):
        for w in self.windows:
            if w.pid == pid:
                return w.image
        return ""

    def client_bounds(self, hwnd):
        w = self._find(hwnd)
        return w.bounds if w else None

    def foreground_window(self):
        return self.foreground

    def root_window_at_point(self, x, y):
        hit = self.point_hits.get((int(x), int(y)))
        return hit if hit is not None else self.foreground

    def restore(self, hwnd):
        self.calls.append(("restore", hwnd))
        w = self._find(hwnd)
        if w is not None:
            w.iconic = False

    def force_foreground(self, hwnd):
        self.calls.append(("force_foreground", hwnd))
        self.foreground = hwnd

    def print_window(self, hwnd, size):
        self.calls.append(("print_window", hwnd, size))
        if not self.print_window_works:
            return None
        return Image.new("RGB", size, (10, 20, 30))

    def blt_window(self, hwnd, size):
        self.calls.append(("blt_window", hwnd, size))
        if not self.blt_works:
            return None
        return Image.new("RGB", size, (40, 50, 60))

    def send_mouse_button(self, x, y, button, down):
        self.calls.append(("mouse_button", round(x, 1), round(y, 1), button, down))

    def send_mouse_move(self, x, y):
        self.calls.append(("mouse_move", round(x, 1), round(y, 1)))

    def send_key_vk(self, vk, down):
        self.calls.append(("key_vk", vk, down))

    def send_unicode_char(self, ch, down):
        self.calls.append(("unicode", ch, down))

    def send_wheel(self, delta, horizontal):
        self.calls.append(("wheel", delta, horizontal))

    def vk_for_char(self, ch):
        return _us_vk_for_char(ch)

    def self_elevated(self):
        return self.self_elev

    def process_elevated(self, pid):
        if pid in self.unknown_elevation_pids:
            return None
        return pid in self.elevated_pids


def _client(
    api: FakeWin32Api | None = None, **kw
) -> tuple[Win32WindowClient, FakeWin32Api]:
    api = api if api is not None else FakeWin32Api()
    return Win32WindowClient(api, char_delay_s=0.0, **kw), api


def _backend(
    api: FakeWin32Api | None = None,
) -> tuple[RemoteDisplayBackend, FakeWin32Api]:
    client, api = _client(api)
    backend = RemoteDisplayBackend(client=client, owner_substr="wfica32", settle_s=0.0)
    return backend, api


# -- selection matching -------------------------------------------------------


def test_owner_matches_process_exe_tolerant_never_substring() -> None:
    assert owner_matches_process("wfica32", "wfica32.exe")
    assert owner_matches_process("wfica32.exe", "wfica32.exe")
    assert owner_matches_process("WFICA32.EXE", "wfica32.exe")
    assert owner_matches_process("wfica32.exe", "wfica32")
    assert not owner_matches_process("fica", "wfica32.exe")  # substring refused
    assert not owner_matches_process("wfica32", "wfica32b.exe")
    assert not owner_matches_process("wfica32", "")


def test_find_windows_matches_process_and_exact_title() -> None:
    client, _api = _client()
    matches = client.find_windows("wfica32", "accuro - citrix workspace")
    assert [w.window_id for w in matches] == [101]
    assert matches[0].owner == "wfica32.exe"
    assert matches[0].bounds == (300.0, 200.0, 1280.0, 800.0)
    assert matches[0].on_screen is True


def test_find_windows_skips_cloaked_invisible_and_untitled() -> None:
    api = FakeWin32Api(
        windows=[
            FakeWindow(1, cloaked=True),  # suspended UWP ghost
            FakeWindow(2, visible=False),
            FakeWindow(3, title=""),  # unnamed tool/host window
            FakeWindow(4),
        ]
    )
    client, _ = _client(api)
    assert [w.window_id for w in client.find_windows("wfica32", None)] == [4]


def test_find_windows_title_mismatch_and_partial_title_refused() -> None:
    client, _api = _client()
    assert client.find_windows("wfica32", "accuro") == []  # partial title
    assert client.find_windows("notepad", None) == []  # wrong process


def test_expected_class_filter() -> None:
    api = FakeWin32Api(
        windows=[
            FakeWindow(1, cls="Transparent Windows Client"),
            FakeWindow(2, cls="CtxICADisp", title="Accuro - Citrix Workspace"),
        ]
    )
    client = Win32WindowClient(api, expected_class="transparent windows client")
    assert [w.window_id for w in client.find_windows("wfica32", None)] == [1]


def test_duplicate_exact_windows_halt_ambiguous_at_backend() -> None:
    """Two identical Citrix session windows must HALT, never 'pick the front
    one' — the wrong-window analog of the wrong-patient refusal."""
    api = FakeWin32Api(windows=[FakeWindow(1), FakeWindow(2)])
    backend, _ = _backend(api)
    with pytest.raises(RemoteDisplayError, match="ambiguous remote-display target"):
        backend.screenshot()


def test_minimized_window_is_resolvable_but_not_on_screen() -> None:
    api = FakeWin32Api(windows=[FakeWindow(1, iconic=True)])
    client, _ = _client(api)
    matches = client.find_windows("wfica32", None)
    assert len(matches) == 1 and matches[0].on_screen is False


# -- DPI ----------------------------------------------------------------------


@pytest.mark.parametrize("level", ["system", "unaware"])
def test_dpi_virtualization_refused(level: str) -> None:
    api = FakeWin32Api(dpi=level)
    client, _ = _client(api)
    with pytest.raises(DpiAwarenessError, match="per-monitor DPI"):
        client.find_windows("wfica32", None)
    with pytest.raises(DpiAwarenessError):
        client.capture(101)


def test_dpi_established_once_and_cached() -> None:
    client, api = _client()
    client.find_windows("wfica32", None)
    client.find_windows("wfica32", None)
    assert api.calls.count(("dpi",)) == 1


# -- coordinate mapping -------------------------------------------------------


def test_backend_scale_is_unity_and_click_maps_to_client_origin() -> None:
    """capture px == client-rect px (per-monitor v2), so scale == 1.0 and a
    captured pixel maps to screen = client_origin + pixel."""
    backend, api = _backend()
    backend.screenshot()
    assert backend.viewport == (1280, 800)
    assert backend._scale == pytest.approx(1.0)
    backend.click(100, 50)
    downs = [c for c in api.calls if c[0] == "mouse_button" and c[4] is True]
    assert downs and downs[0][1] == pytest.approx(400.0)  # 300 + 100/1.0
    assert downs[0][2] == pytest.approx(250.0)  # 200 + 50/1.0


def test_double_click_emits_two_transitions() -> None:
    backend, api = _backend()
    backend.screenshot()
    backend.click(10, 10, double=True)
    downs = [c for c in api.calls if c[0] == "mouse_button" and c[4] is True]
    ups = [c for c in api.calls if c[0] == "mouse_button" and c[4] is False]
    assert len(downs) == 2 and len(ups) == 2


def test_normalize_to_virtual_desktop_math() -> None:
    # Single 1920x1080 desktop at origin: corners map to the extremes.
    virtual = (0, 0, 1920, 1080)
    assert normalize_to_virtual_desktop(0, 0, virtual) == (0, 0)
    assert normalize_to_virtual_desktop(1919, 1079, virtual) == (65535, 65535)
    nx, ny = normalize_to_virtual_desktop(960, 540, virtual)
    assert nx == round(960 * 65535 / 1919) and ny == round(540 * 65535 / 1079)
    # Multi-monitor desktop with a negative-origin (left) secondary monitor.
    virtual2 = (-1920, 0, 3840, 1080)
    assert normalize_to_virtual_desktop(-1920, 0, virtual2) == (0, 0)


def test_normalize_refuses_points_outside_virtual_desktop() -> None:
    with pytest.raises(InputDeliveryError, match="outside the virtual desktop"):
        normalize_to_virtual_desktop(2000, 10, (0, 0, 1920, 1080))
    with pytest.raises(InputDeliveryError, match="degenerate"):
        normalize_to_virtual_desktop(0, 0, (0, 0, 0, 0))


# -- focus verification / identity halts -------------------------------------


def test_input_refused_when_other_window_holds_foreground() -> None:
    """The focus-verification halt: activation is attempted, but if the exact
    target HWND does not hold the foreground the burst is refused."""
    api = FakeWin32Api(windows=[FakeWindow(1), FakeWindow(9, title="Other", pid=7)])
    api.foreground = 9

    def no_op_force_foreground(hwnd):
        api.calls.append(("force_foreground", hwnd))  # foreground stays 9

    api.force_foreground = no_op_force_foreground
    client = Win32WindowClient(api, char_delay_s=0.0)
    backend = RemoteDisplayBackend(
        client=client,
        owner_substr="wfica32",
        title_substr="Accuro - Citrix Workspace",
        settle_s=0.0,
    )
    backend.screenshot()
    with pytest.raises(RemoteDisplayError, match="not visible, app-frontmost"):
        backend.click(10, 10)
    with pytest.raises(RemoteDisplayError, match="not visible, app-frontmost"):
        backend.type_text("x")
    assert not any(c[0] in {"mouse_button", "key_vk", "unicode"} for c in api.calls)


def test_key_window_id_requires_exact_foreground_hwnd() -> None:
    """Same pid, different window foregrounded -> None (stricter than the
    macOS z-order proxy; GetForegroundWindow IS the keyboard target)."""
    api = FakeWin32Api(windows=[FakeWindow(1), FakeWindow(2, title="Second Session")])
    api.foreground = 2
    client, _ = _client(api)
    assert client.key_window_id(4242) == 2
    assert client.frontmost_pid() == 4242
    api.foreground = 1
    assert client.key_window_id(4242) == 1


def test_click_point_occluded_by_other_window_is_refused() -> None:
    backend, api = _backend()
    backend.screenshot()
    api.point_hits[(310, 210)] = 777  # another app's root window at the point
    with pytest.raises(RemoteDisplayError, match="covered by window 777"):
        backend.click(10, 10)
    assert not any(c[0] == "mouse_button" for c in api.calls)


def test_focus_lost_during_readiness_probe_halts() -> None:
    api = FakeWin32Api()

    def focus_stealing_probe(_png: bytes) -> bool:
        api.foreground = 999
        return True

    client, _ = _client(api)
    backend = RemoteDisplayBackend(
        client=client,
        owner_substr="wfica32",
        settle_s=0.0,
        readiness_probe=focus_stealing_probe,
    )
    backend.screenshot()
    with pytest.raises(RemoteDisplayError, match="readiness validation|changed during"):
        backend.click(10, 10)
    assert not any(c[0] == "mouse_button" for c in api.calls)


def test_activate_restores_minimized_then_foregrounds() -> None:
    api = FakeWin32Api(windows=[FakeWindow(1, iconic=True)])
    client, _ = _client(api)
    client.find_windows("wfica32", None)  # populates the activation hint
    client.activate(4242)
    assert ("restore", 1) in api.calls
    assert ("force_foreground", 1) in api.calls


def test_activate_without_unambiguous_hint_is_a_noop() -> None:
    api = FakeWin32Api(windows=[FakeWindow(1), FakeWindow(2)])
    client, _ = _client(api)
    client.find_windows("wfica32", None)  # two matches: no hint for this pid
    client.activate(4242)
    assert not any(c[0] in {"restore", "force_foreground"} for c in api.calls)


# -- capture fallback ordering ------------------------------------------------


def test_capture_prefers_print_window() -> None:
    client, api = _client()
    png, w, h = client.capture(101)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and (w, h) == (1280, 800)
    kinds = [c[0] for c in api.calls if c[0] in {"print_window", "blt_window"}]
    assert kinds == ["print_window"]


def test_capture_falls_back_to_bitblt_when_print_window_fails() -> None:
    api = FakeWin32Api(print_window_works=False)
    client, _ = _client(api)
    png, w, h = client.capture(101)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and (w, h) == (1280, 800)
    kinds = [c[0] for c in api.calls if c[0] in {"print_window", "blt_window"}]
    assert kinds == ["print_window", "blt_window"]


def test_capture_both_paths_failing_is_loud() -> None:
    api = FakeWin32Api(print_window_works=False, blt_works=False)
    client, _ = _client(api)
    with pytest.raises(CaptureFailedError, match="both PrintWindow and BitBlt"):
        client.capture(101)


def test_capture_window_gone_and_minimized_are_typed() -> None:
    api = FakeWin32Api(windows=[FakeWindow(1, iconic=True)])
    client, _ = _client(api)
    with pytest.raises(WindowGoneError, match="no longer exists"):
        client.capture(999)
    with pytest.raises(WindowMinimizedError, match="minimized"):
        client.capture(1)


def test_capture_size_disagreement_is_refused() -> None:
    class WrongSizeApi(FakeWin32Api):
        def print_window(self, hwnd, size):
            return Image.new("RGB", (size[0] - 4, size[1]), (0, 0, 0))

    client, _ = _client(WrongSizeApi())
    with pytest.raises(CaptureFailedError, match="disagrees with the client"):
        client.capture(101)


# -- UIPI elevation guard -----------------------------------------------------


def test_elevated_target_makes_input_untrusted_and_backend_halts() -> None:
    api = FakeWin32Api(elevated_pids={4242})
    backend, _ = _backend(api)
    backend.screenshot()  # resolves the target -> trust is evaluated against it
    with pytest.raises(RemoteDisplayError, match="silently dropped"):
        backend.click(10, 10)
    assert not any(c[0] == "mouse_button" for c in api.calls)


def test_unknown_elevation_fails_closed() -> None:
    api = FakeWin32Api(unknown_elevation_pids={4242})
    client, _ = _client(api)
    client.find_windows("wfica32", None)
    assert client.input_trusted() is False
    with pytest.raises(InputDeliveryError, match="UIPI"):
        client.mouse(10, 10, button="left", down=True, click_count=1)


def test_elevated_driver_may_drive_elevated_target() -> None:
    api = FakeWin32Api(elevated_pids={4242}, self_elev=True)
    client, _ = _client(api)
    client.find_windows("wfica32", None)
    assert client.input_trusted() is True


def test_direct_input_to_elevated_target_is_refused_at_the_client() -> None:
    """Even bypassing the backend, the client itself refuses UIPI-doomed
    injection — a dropped input must never look like success."""
    api = FakeWin32Api(elevated_pids={4242})
    client, _ = _client(api)
    client.find_windows("wfica32", None)
    for fn in (
        lambda: client.mouse(1, 1, button="left", down=True, click_count=1),
        lambda: client.mouse_move(1, 1),
        lambda: client.type_chars("x"),
        lambda: client.key(0x41, down=True, flags=[]),
        lambda: client.scroll(0, 120),
    ):
        with pytest.raises(InputDeliveryError, match="UIPI"):
            fn()
    assert not any(
        c[0] in {"mouse_button", "mouse_move", "key_vk", "unicode", "wheel"}
        for c in api.calls
    )


# -- input synthesis ----------------------------------------------------------


def test_type_chars_uses_layout_vks_with_shift_wrapping() -> None:
    client, api = _client()
    client.type_chars("Ab-")
    keys = [c for c in api.calls if c[0] == "key_vk"]
    assert keys == [
        ("key_vk", 0x10, True),  # Shift down
        ("key_vk", 0x41, True),  # A down
        ("key_vk", 0x41, False),  # A up
        ("key_vk", 0x10, False),  # Shift up
        ("key_vk", 0x42, True),  # b
        ("key_vk", 0x42, False),
        ("key_vk", 0xBD, True),  # -
        ("key_vk", 0xBD, False),
    ]
    assert not any(c[0] == "unicode" for c in api.calls)


def test_type_chars_unicode_fallback_for_unmapped_char() -> None:
    client, api = _client()
    client.type_chars("é")
    assert [c for c in api.calls if c[0] == "unicode"] == [
        ("unicode", "é", True),
        ("unicode", "é", False),
    ]


def test_key_chord_wraps_real_modifier_transitions() -> None:
    client, api = _client()
    client.key(0x41, down=True, flags=["control"])
    client.key(0x41, down=False, flags=["control"])
    assert [c for c in api.calls if c[0] == "key_vk"] == [
        ("key_vk", 0x11, True),  # Ctrl down before the key
        ("key_vk", 0x41, True),
        ("key_vk", 0x41, False),
        ("key_vk", 0x11, False),  # Ctrl up after the key (never latched)
    ]


def test_scroll_sign_convention_matches_backend() -> None:
    client, api = _client()
    client.scroll(0, 120)  # positive dy = content up -> negative wheel delta
    client.scroll(80, 0)
    wheels = [c for c in api.calls if c[0] == "wheel"]
    assert wheels == [("wheel", -360, False), ("wheel", 240, True)]


def test_resolve_key_named_then_layout() -> None:
    client, _ = _client()
    assert client.resolve_key("Enter") == (0x0D, False)
    assert client.resolve_key("pagedown") == (0x22, False)
    # chord letter resolves unshifted from the named table (macOS parity)
    assert client.resolve_key("a") == (0x41, False)
    # explicit upper-case character goes through the layout and adds Shift
    assert client.resolve_key("B") == (0x42, True)
    assert client.resolve_key("NoSuchKey") is None


def test_backend_press_routes_win32_vks() -> None:
    backend, api = _backend()
    backend.press("Enter")
    backend.press("ControlOrMeta+a")
    keys = [c for c in api.calls if c[0] == "key_vk"]
    assert ("key_vk", 0x0D, True) in keys and ("key_vk", 0x0D, False) in keys
    # Ctrl+A: modifier transitions wrap the key on both edges
    tail = keys[-4:]
    assert tail == [
        ("key_vk", 0x11, True),
        ("key_vk", 0x41, True),
        ("key_vk", 0x41, False),
        ("key_vk", 0x11, False),
    ]


# -- platform guard -----------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="native API exists on Windows")
def test_native_api_refuses_non_windows_host() -> None:
    with pytest.raises(Win32WindowError, match="requires a Windows host"):
        NativeWin32Api()


def test_module_imports_without_windows_bindings() -> None:
    """The module must be importable (and the client testable) anywhere; only
    NativeWin32Api construction touches ctypes.windll."""
    import openadapt_flow.backends.win32_window_client as mod

    assert mod.Win32WindowClient is Win32WindowClient

"""Safe attachment of the Playwright recorder to an existing Chromium tab."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest
from PIL import Image

import openadapt_flow.interactive_recorder as interactive_recorder_module
from openadapt_flow.__main__ import main
from openadapt_flow.backends.playwright_backend import (
    PlaywrightBackend,
    ScreenshotMaskStabilityError,
)
from openadapt_flow.compiler import compile_recording
from openadapt_flow.interactive_recorder import (
    BrowserAttachError,
    InteractiveRecorder,
    _secret_screenshot_selectors,
    record_interactive,
    select_attached_page,
    validate_browser_cdp_endpoint,
)


class _Page:
    def __init__(self, url: str) -> None:
        self.url = url


def _browser(*urls: str):
    return SimpleNamespace(
        contexts=[SimpleNamespace(pages=[_Page(url) for url in urls])]
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:9222",
        "http://127.0.0.1:9222",
        "http://127.9.8.7:9222",
        "ws://[::1]:9222/devtools/browser/abc",
    ],
)
def test_cdp_endpoint_accepts_only_explicit_loopback(endpoint: str) -> None:
    assert validate_browser_cdp_endpoint(endpoint) == endpoint


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("http://example.com:9222", "loopback"),
        ("http://localhost", "include a port"),
        ("ftp://localhost:9222", "must use http"),
        ("http://user:pass@localhost:9222", "must not contain credentials"),
        ("http://localhost:9222?token=secret", "must not contain credentials"),
    ],
)
def test_cdp_endpoint_refuses_unsafe_boundaries(endpoint: str, message: str) -> None:
    with pytest.raises(BrowserAttachError, match=message):
        validate_browser_cdp_endpoint(endpoint)


def test_attach_selects_the_only_tab_on_the_declared_origin() -> None:
    wanted = _Page("https://app.example.test/work/12?record=private")
    browser = SimpleNamespace(
        contexts=[
            SimpleNamespace(
                pages=[
                    _Page("chrome://settings/"),
                    _Page("https://unrelated.example.test/"),
                    wanted,
                ]
            )
        ]
    )
    assert select_attached_page(browser, app_url="https://app.example.test/") is wanted


def test_attach_requires_exact_url_when_origin_has_multiple_tabs() -> None:
    browser = _browser(
        "https://app.example.test/one?patient=SECRET_ONE#private",
        "https://app.example.test/two?patient=SECRET_TWO",
    )
    with pytest.raises(BrowserAttachError) as caught:
        select_attached_page(browser, app_url="https://app.example.test/")
    message = str(caught.value)
    assert "--browser-page-url" in message
    assert "/one" in message and "/two" in message
    assert "SECRET_ONE" not in message and "SECRET_TWO" not in message


def test_attach_exact_url_selects_one_tab_without_navigation() -> None:
    selected = "https://app.example.test/two?record=42"
    browser = _browser("https://app.example.test/one", selected)
    page = select_attached_page(
        browser,
        app_url="https://app.example.test/",
        page_url=selected,
    )
    assert page.url == selected


def test_attach_refuses_cross_origin_page_selector_without_echoing_it() -> None:
    private_url = "https://other.example.test/path?token=DO_NOT_PRINT"
    with pytest.raises(BrowserAttachError) as caught:
        select_attached_page(
            _browser("https://app.example.test/"),
            app_url="https://app.example.test/",
            page_url=private_url,
        )
    assert "same origin" in str(caught.value)
    assert private_url not in str(caught.value)
    assert "DO_NOT_PRINT" not in str(caught.value)


def test_attached_backend_uses_live_css_viewport_and_css_screenshot() -> None:
    class Page:
        viewport_size = None

        def __init__(self) -> None:
            self.screenshot_options = None

        def evaluate(self, _script):
            return {"width": 1440, "height": 900}

        def screenshot(self, **kwargs):
            self.screenshot_options = kwargs
            return b"png"

    page = Page()
    backend = PlaywrightBackend(page, screenshot_scale="css")  # type: ignore[arg-type]
    assert backend.viewport == (1440, 900)
    assert backend.screenshot() == b"png"
    assert page.screenshot_options["scale"] == "css"


def test_backend_masks_password_and_declared_secret_fields_on_every_frame() -> None:
    class Frame:
        def __init__(self, name: str) -> None:
            self.name = name

        def locator(self, selector):
            return f"locator:{self.name}:{selector}"

    class Page:
        viewport_size = {"width": 1280, "height": 800}

        def __init__(self) -> None:
            self.screenshot_options: list[dict] = []
            self.frames = [Frame("main"), Frame("child")]
            self.listeners: dict[str, list] = {}
            self.attach_on_next_screenshot = True

        def on(self, event, listener):
            self.listeners.setdefault(event, []).append(listener)

        def remove_listener(self, event, listener):
            self.listeners[event].remove(listener)

        def evaluate(self, _script):
            return None

        def screenshot(self, **kwargs):
            self.screenshot_options.append(kwargs)
            if self.attach_on_next_screenshot:
                self.attach_on_next_screenshot = False
                frame = Frame("late-child")
                self.frames.append(frame)
                for listener in self.listeners.get("frameattached", []):
                    listener(frame)
            return b"png"

    selectors = (
        "input[type='password']",
        '[name="token"], [id="token"]',
    )
    page = Page()
    backend = PlaywrightBackend(  # type: ignore[arg-type]
        page,
        screenshot_mask_selectors=selectors,
    )

    assert backend.screenshot() == b"png"
    assert backend.screenshot() == b"png"
    assert len(page.screenshot_options) == 3
    assert page.screenshot_options[0]["mask"] == [
        f"locator:{frame}:{selector}"
        for frame in ("main", "child")
        for selector in selectors
    ]
    assert page.screenshot_options[1]["mask"] == [
        f"locator:{frame}:{selector}"
        for frame in ("main", "child", "late-child")
        for selector in selectors
    ]
    assert page.screenshot_options[2]["mask"] == page.screenshot_options[1]["mask"]
    for options in page.screenshot_options:
        assert options["mask_color"] == "#000000"
    backend.stop_screenshot_mask_tracking()
    assert not any(page.listeners.values())


def test_declared_secret_selectors_use_css_string_escaping() -> None:
    selectors = _secret_screenshot_selectors({"päss", 'quote"\\line\nend'})

    assert '[name="päss"], [id="päss"]' in selectors
    assert (
        '[name="quote\\"\\\\line\\a end"], [id="quote\\"\\\\line\\a end"]' in selectors
    )
    with pytest.raises(BrowserAttachError, match="null character"):
        _secret_screenshot_selectors({"unsafe\x00field"})
    with pytest.raises(BrowserAttachError, match="Unicode surrogate"):
        _secret_screenshot_selectors({"unsafe\ud800field"})


def test_attached_recorder_api_refuses_incompatible_options(tmp_path: Path) -> None:
    with pytest.raises(BrowserAttachError, match="requires a browser CDP"):
        InteractiveRecorder(
            "https://app.example.test/",
            tmp_path / "recording",
            browser_page_url="https://app.example.test/work",
        )
    with pytest.raises(BrowserAttachError, match="headless"):
        InteractiveRecorder(
            "https://app.example.test/",
            tmp_path / "recording",
            cdp_endpoint="http://127.0.0.1:9222",
            headless=True,
        )


def test_attached_recorder_reads_geometry_and_refuses_origin_drift(
    tmp_path: Path,
) -> None:
    session = InteractiveRecorder(
        "https://app.example.test/",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session.page = SimpleNamespace(
        url="https://app.example.test/work",
        evaluate=lambda _script: {"width": 1280, "height": 800, "dpr": 2},
    )
    assert session._read_attached_geometry() == (1280, 800, 2.0)

    session.page.url = "https://other.example.test/?token=DO_NOT_PRINT"
    with pytest.raises(BrowserAttachError) as caught:
        session._read_attached_geometry()
    assert "left the declared application origin" in str(caught.value)
    assert "DO_NOT_PRINT" not in str(caught.value)


def test_attached_recorder_retains_main_frame_origin_violation(
    tmp_path: Path,
) -> None:
    out = tmp_path / "recording"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()
    frame = SimpleNamespace(url="https://other.example.test/temporary")
    session.page = SimpleNamespace(main_frame=frame)

    session._handle_frame_navigation(frame)
    frame.url = "https://app.example.test/returned"
    session._handle_frame_navigation(frame)

    assert session.done is True
    assert session._listener_error is not None
    assert "left the declared application origin" in str(session._listener_error)
    with pytest.raises(BrowserAttachError, match="left the declared"):
        session.finish()
    assert not out.exists()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_recorder_refuses_existing_output_without_changing_it(
    tmp_path: Path,
) -> None:
    out = tmp_path / "recording"
    frames = out / "frames"
    frames.mkdir(parents=True)
    (out / "meta.json").write_text('{"id":"complete-existing"}\n')
    (out / "events.jsonl").write_text('{"i":0,"kind":"key"}\n')
    (frames / "0000_before.png").write_bytes(b"SENSITIVE-FRAME")
    before = {
        path.relative_to(out): path.read_bytes()
        for path in out.rglob("*")
        if path.is_file()
    }
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )

    with pytest.raises(BrowserAttachError, match="output already exists"):
        session._prepare_recording_dir()
    session.abort()

    after = {
        path.relative_to(out): path.read_bytes()
        for path in out.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_recorder_refuses_output_created_during_promotion(
    tmp_path: Path,
) -> None:
    out = tmp_path / "recording"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()
    assert session._recording_dir is not None
    (session._recording_dir / "complete.txt").write_text("new recording")

    out.mkdir()
    original_identity = out.stat()
    with pytest.raises(BrowserAttachError, match="appeared during capture"):
        session._promote_recording()

    current_identity = out.stat()
    assert (current_identity.st_dev, current_identity.st_ino) == (
        original_identity.st_dev,
        original_identity.st_ino,
    )
    session.abort()
    assert out.is_dir()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_tab_close_discards_partial_output(tmp_path: Path) -> None:
    out = tmp_path / "recording"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()
    assert session._recording_dir is not None
    (session._recording_dir / "meta.json").write_text('{"id":"not-final"}\n')

    session._handle_page_close()

    with pytest.raises(BrowserAttachError, match="selected browser tab closed"):
        session.finish()
    assert not out.exists()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_tab_close_during_finalization_discards_metadata(
    tmp_path: Path,
) -> None:
    out = tmp_path / "recording"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()

    class ClosingPage:
        url = "https://app.example.test/"
        frames: list = []

        def __init__(self) -> None:
            self.main_frame = SimpleNamespace(url=self.url)

        def evaluate(self, _script):
            return {"width": 1280, "height": 800, "dpr": 1}

    session.page = ClosingPage()
    session._page_lifecycle_listeners_installed = True
    session._attached_geometry = (1280, 800, 1.0)
    session._initial_attached_viewport = (1280, 800)

    class ClosingRecorder:
        def finish(self):
            assert session._recording_dir is not None
            (session._recording_dir / "meta.json").write_text(
                json.dumps({"viewport": [1280, 800]})
            )
            return session._recording_dir

    session.recorder = ClosingRecorder()  # type: ignore[assignment]
    session._pw = SimpleNamespace(stop=lambda: session._handle_page_close())

    with pytest.raises(BrowserAttachError, match="selected browser tab closed"):
        session.finish()
    assert not out.exists()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_popup_during_finalization_discards_metadata(
    tmp_path: Path,
) -> None:
    out = tmp_path / "recording"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()

    class PopupPage:
        url = "https://app.example.test/"
        frames: list = []

        def __init__(self) -> None:
            self.main_frame = SimpleNamespace(url=self.url)

        def evaluate(self, _script):
            return {"width": 1280, "height": 800, "dpr": 1}

    session.page = PopupPage()
    session._page_lifecycle_listeners_installed = True
    session._attached_geometry = (1280, 800, 1.0)
    session._initial_attached_viewport = (1280, 800)

    class FinalizingRecorder:
        def finish(self):
            assert session._recording_dir is not None
            (session._recording_dir / "meta.json").write_text(
                json.dumps({"viewport": [1280, 800]})
            )
            return session._recording_dir

    session.recorder = FinalizingRecorder()  # type: ignore[assignment]
    session._pw = SimpleNamespace(
        stop=lambda: session._handle_popup(SimpleNamespace(url="about:blank"))
    )

    with pytest.raises(BrowserAttachError, match="popup or new tab"):
        session.finish()
    assert not out.exists()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


@pytest.mark.parametrize("late_event", ["context_page", "origin", "frame"])
def test_attached_late_lifecycle_event_discards_metadata(
    tmp_path: Path,
    late_event: str,
) -> None:
    out = tmp_path / f"recording-{late_event}"
    session = InteractiveRecorder(
        "https://app.example.test/",
        out,
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._prepare_recording_dir()

    class LifecyclePage:
        url = "https://app.example.test/"
        frames: list = []

        def __init__(self) -> None:
            self.main_frame = SimpleNamespace(url=self.url)

        def evaluate(self, _script):
            return {"width": 1280, "height": 800, "dpr": 1}

    session.page = LifecyclePage()
    session._page_lifecycle_listeners_installed = True
    session._attached_geometry = (1280, 800, 1.0)
    session._initial_attached_viewport = (1280, 800)

    class FinalizingRecorder:
        def finish(self):
            assert session._recording_dir is not None
            (session._recording_dir / "meta.json").write_text(
                json.dumps({"viewport": [1280, 800]})
            )
            return session._recording_dir

    session.recorder = FinalizingRecorder()  # type: ignore[assignment]

    def emit_late_event() -> None:
        if late_event == "context_page":
            session._handle_context_page(SimpleNamespace(url="about:blank"))
        elif late_event == "origin":
            session.page.main_frame.url = "https://other.example.test/"
            session._handle_frame_navigation(session.page.main_frame)
        else:
            session._handle_frame_tree_change(SimpleNamespace())

    session._pw = SimpleNamespace(stop=emit_late_event)

    with pytest.raises(BrowserAttachError):
        session.finish()
    assert not out.exists()
    assert not list(tmp_path.glob(".openadapt-recording-partial-*"))


def test_attached_recorder_refuses_a_new_context_page(tmp_path: Path) -> None:
    session = InteractiveRecorder(
        "https://app.example.test/",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    selected = SimpleNamespace()
    context = SimpleNamespace(pages=[selected])
    selected.context = context
    session.page = selected
    session._context_pages_at_start = (selected,)

    context.pages.append(SimpleNamespace(context=context))

    with pytest.raises(BrowserAttachError, match="popup or new tab"):
        session._assert_no_new_pages()
    assert session.done is True


def test_attached_recorder_refuses_iframe_events(tmp_path: Path) -> None:
    session = InteractiveRecorder(
        "https://app.example.test/",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": False,
            "kind": "click",
            "x": 10,
            "y": 20,
        }
    )
    assert session.done is True
    assert session._pyq == []
    assert session._listener_error is not None
    assert "iframe" in str(session._listener_error)

    session.done = False
    session._listener_error = None
    selected_frame = object()
    session.page = SimpleNamespace(main_frame=selected_frame)
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": True,
            "kind": "click",
            "x": 10,
            "y": 20,
        },
        source={"page": session.page, "frame": object()},
    )
    assert session.done is True
    assert session._listener_error is not None
    assert "iframe" in str(session._listener_error)


def test_attached_recorder_refuses_invalid_viewport_evidence(tmp_path: Path) -> None:
    session = InteractiveRecorder(
        "https://app.example.test/",
        tmp_path / "recording",
        cdp_endpoint="http://127.0.0.1:9222",
    )
    selected_frame = object()
    session.page = SimpleNamespace(main_frame=selected_frame)
    session._enqueue_browser_event(
        {
            "__oaflow_session": session._session_id,
            "__oaflow_top_level": True,
            "__oaflow_viewport": [0, 800],
            "__oaflow_dpr": 2,
            "kind": "click",
            "url": "https://app.example.test/work",
            "x": 10,
            "y": 20,
        },
        source={"page": session.page, "frame": selected_frame},
    )
    assert session.done is True
    assert session._pyq == []
    assert session._listener_error is not None
    assert "invalid viewport evidence" in str(session._listener_error)


@pytest.mark.parametrize(
    "batch",
    [
        [{"kind": "click"}, {"kind": "click"}],
        [
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
            {"kind": "click"},
        ],
        [
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
            {"kind": "key", "key": "Enter"},
        ],
        [{"kind": "scroll", "dy": 10}, {"kind": "click"}],
        [
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
            {"kind": "input", "field": "note", "_oaflow_input_session": "b"},
        ],
    ],
)
def test_browser_event_batch_refuses_multiple_logical_actions(
    batch: list[dict],
) -> None:
    with pytest.raises(BrowserAttachError, match="more than one logical"):
        InteractiveRecorder._validate_event_batch(batch)


@pytest.mark.parametrize(
    "batch",
    [
        [
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
            {"kind": "input", "field": "note", "_oaflow_input_session": "a"},
        ],
        [{"kind": "scroll", "dy": 10}, {"kind": "scroll", "dy": 20}],
    ],
)
def test_browser_event_batch_preserves_one_coalescible_action(
    batch: list[dict],
) -> None:
    InteractiveRecorder._validate_event_batch(batch)


def test_cli_threads_attach_contract_to_the_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_record(url, out_dir, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        out_dir.mkdir(parents=True)
        (out_dir / "meta.json").write_text(json.dumps({"source": "fake"}))
        return out_dir

    monkeypatch.setattr(
        "openadapt_flow.interactive_recorder.record_interactive", fake_record
    )
    selected = "https://app.example.test/work?record=42"
    rc = main(
        [
            "record",
            "--backend",
            "web",
            "--url",
            "https://app.example.test/",
            "--browser-cdp-endpoint",
            "http://127.0.0.1:9222",
            "--browser-page-url",
            selected,
            "--out",
            str(tmp_path / "recording"),
        ]
    )
    assert rc == 0
    assert captured["url"] == "https://app.example.test/"
    assert captured["cdp_endpoint"] == "http://127.0.0.1:9222"
    assert captured["browser_page_url"] == selected


def test_cli_refuses_attach_flags_on_the_wrong_surface(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="apply only to --backend web"):
        main(
            [
                "record",
                "--backend",
                "windows",
                "--browser-cdp-endpoint",
                "http://127.0.0.1:9222",
                "--out",
                str(tmp_path / "recording"),
            ]
        )


def test_cli_refuses_page_selector_without_endpoint(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="requires --browser-cdp-endpoint"):
        main(
            [
                "record",
                "--backend",
                "web",
                "--url",
                "https://app.example.test/",
                "--browser-page-url",
                "https://app.example.test/work",
                "--out",
                str(tmp_path / "recording"),
            ]
        )


def test_cli_refuses_headless_attachment(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--headless cannot be combined"):
        main(
            [
                "record",
                "--backend",
                "web",
                "--url",
                "https://app.example.test/",
                "--browser-cdp-endpoint",
                "http://127.0.0.1:9222",
                "--headless",
                "--out",
                str(tmp_path / "recording"),
            ]
        )


_ATTACH_HTML = b"""<!doctype html>
<html><head><title>Attach recorder test</title></head>
<body>
  <label for="note">Note</label><input id="note" name="note">
  <label for="password">Password</label>
  <input id="password" name="password" type="text">
  <label for="p&#228;ss">International secret</label>
  <input id="p&#228;ss" name="p&#228;ss" type="text">
  <label for="pre-focus-secret">Pre-focus secret</label>
  <input id="pre-focus-secret" name="pre-focus-secret" type="text">
  <label for="sticky-secret">Sticky secret</label>
  <input id="sticky-secret" name="sticky-secret" type="text"
         onkeydown="this.removeAttribute('name'); this.removeAttribute('id')">
  <label for="replacement-secret">Replacement secret</label>
  <input id="replacement-secret" name="replacement-secret" type="text"
         oninput="if (!this.dataset.replaced) {
           const replacement = this.cloneNode(true);
           replacement.removeAttribute('name');
           replacement.removeAttribute('id');
           replacement.dataset.replaced = 'yes';
           this.replaceWith(replacement);
           replacement.focus();
         }">
  <button id="save" onclick="document.body.dataset.saved='yes'">Save</button>
  <button id="open-popup" onclick="window.open('about:blank', '_blank')">
    Open popup
  </button>
  <iframe id="child" srcdoc="
    <input id='frame-password' type='password' value='FRAME-SECRET-NEVER-PERSIST'
           style='width:160px;height:30px;border:0'>
    <button id='inside'>Inside frame</button>
  "></iframe>
</body></html>"""


@pytest.fixture(scope="module")
def attach_app_url() -> str:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_ATTACH_HTML)))
            self.end_headers()
            self.wfile.write(_ATTACH_HTML)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _chromium_executable() -> Path | None:
    configured = os.environ.get("OPENADAPT_TEST_CHROMIUM_EXECUTABLE")
    candidates = [Path(configured)] if configured else []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            candidates.append(Path(playwright.chromium.executable_path))
    except Exception:
        pass
    for command in ("google-chrome", "google-chrome-stable", "chromium"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    candidates.append(
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


@pytest.mark.timeout(180)
def test_live_cdp_attach_records_compiles_and_leaves_browser_running_three_trials(
    attach_app_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Three real Chromium trials cover attach, secrets, frames, and detach."""

    executable = _chromium_executable()
    if executable is None:
        pytest.skip("no Chromium executable is installed")
    profile = tmp_path / "chrome-profile"
    profile.mkdir()
    process = subprocess.Popen(
        [
            str(executable),
            "--headless=new",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--window-size=1280,800",
            attach_app_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        while not port_file.is_file() and time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Chromium exited before its CDP endpoint was ready")
            time.sleep(0.05)
        assert port_file.is_file(), "Chromium CDP endpoint did not become ready"
        port = int(port_file.read_text().splitlines()[0])
        endpoint = f"http://127.0.0.1:{port}"

        for trial in range(3):
            secret = f"ATTACH-SECRET-{trial}-NEVER-PERSIST"
            secret_rect: dict[str, int] = {}

            def drive(page, pump, *, secret_value=secret, trial_number=trial):
                page.evaluate(
                    """() => {
                      document.querySelector('#note').value = '';
                      document.querySelector('#password').value = '';
                      delete document.body.dataset.saved;
                    }"""
                )
                page.click("#note")
                pump()
                page.keyboard.type(f"trial-{trial_number}")
                pump()
                pump()
                page.click("#password")
                box = page.locator("#password").bounding_box()
                assert box is not None
                secret_rect.update(
                    x=round(box["x"]),
                    y=round(box["y"]),
                    width=round(box["width"]),
                    height=round(box["height"]),
                )
                pump()
                page.keyboard.type(secret_value)
                pump()
                pump()
                page.click("#save")
                pump()
                pump()
                assert page.get_attribute("body", "data-saved") == "yes"

            recording = record_interactive(
                attach_app_url,
                tmp_path / f"recording-{trial}",
                secret_fields=("password",),
                param_fields=("note",),
                cdp_endpoint=endpoint,
                script=drive,
            )
            meta = json.loads((recording / "meta.json").read_text())
            events_text = (recording / "events.jsonl").read_text()
            assert meta["source"] == "openadapt-flow-playwright-cdp"
            assert meta["secret_params"] == ["password"]
            assert secret not in json.dumps(meta)
            assert secret not in events_text
            assert meta["viewport"][0] > 0 and meta["viewport"][1] > 0
            events = [json.loads(line) for line in events_text.splitlines()]
            secret_event = next(event for event in events if event.get("secret"))
            next_before = Image.open(
                recording / "frames" / f"{int(secret_event['i']) + 1:04d}_before.png"
            ).convert("RGB")
            crop = next_before.crop(
                (
                    secret_rect["x"],
                    secret_rect["y"],
                    secret_rect["x"] + secret_rect["width"],
                    secret_rect["y"] + secret_rect["height"],
                )
            )
            assert all(extrema == (0, 0) for extrema in crop.getextrema())

            bundle = tmp_path / f"bundle-{trial}"
            workflow = compile_recording(
                recording,
                bundle,
                name=f"attached-browser-{trial}",
            )
            assert workflow.steps
            assert workflow.secret_params == ["password"]

            # Finishing a recording detaches Playwright. It does not close the
            # operator's external browser or its selected tab.
            assert process.poll() is None
            with urlopen(f"{endpoint}/json/version", timeout=2) as response:
                assert response.status == 200

        unicode_secret = "INTERNATIONAL-SECRET-NEVER-PERSIST"
        unicode_secret_rect: dict[str, int] = {}

        def record_unicode_secret(page, pump):
            field = page.locator('[name="päss"]')
            field.fill("")
            box = field.bounding_box()
            assert box is not None
            unicode_secret_rect.update(
                x=round(box["x"]),
                y=round(box["y"]),
                width=round(box["width"]),
                height=round(box["height"]),
            )
            field.click()
            pump()
            page.keyboard.type(unicode_secret)
            pump()
            pump()
            page.click("#save")
            pump()
            pump()

        unicode_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-unicode-secret",
            secret_fields=("päss",),
            cdp_endpoint=endpoint,
            script=record_unicode_secret,
        )
        unicode_events_text = (unicode_recording / "events.jsonl").read_text()
        unicode_meta_text = (unicode_recording / "meta.json").read_text()
        assert unicode_secret not in unicode_events_text
        assert unicode_secret not in unicode_meta_text
        unicode_events = [json.loads(line) for line in unicode_events_text.splitlines()]
        unicode_event = next(event for event in unicode_events if event.get("secret"))
        unicode_next_before = Image.open(
            unicode_recording
            / "frames"
            / f"{int(unicode_event['i']) + 1:04d}_before.png"
        ).convert("RGB")
        unicode_crop = unicode_next_before.crop(
            (
                unicode_secret_rect["x"],
                unicode_secret_rect["y"],
                unicode_secret_rect["x"] + unicode_secret_rect["width"],
                unicode_secret_rect["y"] + unicode_secret_rect["height"],
            )
        )
        assert all(extrema == (0, 0) for extrema in unicode_crop.getextrema())
        assert process.poll() is None

        def assert_mutating_secret_stays_private(
            *,
            field_selector: str,
            declared_field: str,
            secret: str,
            output_name: str,
            remove_identity_before_focus: bool = False,
        ) -> None:
            secret_rect: dict[str, int] = {}

            def type_through_mutation(page, pump):
                field = page.query_selector(field_selector)
                assert field is not None
                if remove_identity_before_focus:
                    field.evaluate(
                        """element => {
                          element.removeAttribute('name');
                          element.removeAttribute('id');
                        }"""
                    )
                box = field.bounding_box()
                assert box is not None
                secret_rect.update(
                    x=round(box["x"]),
                    y=round(box["y"]),
                    width=round(box["width"]),
                    height=round(box["height"]),
                )
                field.click()
                pump()
                page.keyboard.type(secret)
                pump()
                pump()
                page.click("#save")
                pump()
                pump()

            recording = record_interactive(
                attach_app_url,
                tmp_path / output_name,
                secret_fields=(declared_field,),
                cdp_endpoint=endpoint,
                script=type_through_mutation,
            )
            for artifact in recording.rglob("*"):
                if artifact.is_file():
                    assert secret.encode() not in artifact.read_bytes()
            events = [
                json.loads(line)
                for line in (recording / "events.jsonl").read_text().splitlines()
            ]
            secret_events = [event for event in events if event.get("secret")]
            assert secret_events
            assert all(event.get("text") is None for event in secret_events)
            for frame_path in (recording / "frames").glob("*.png"):
                frame = Image.open(frame_path).convert("RGB")
                crop = frame.crop(
                    (
                        secret_rect["x"],
                        secret_rect["y"],
                        secret_rect["x"] + secret_rect["width"],
                        secret_rect["y"] + secret_rect["height"],
                    )
                )
                assert all(extrema == (0, 0) for extrema in crop.getextrema())

        pre_focus_secret = "PREINPUT-SECRET-NEVER-PERSIST"
        assert_mutating_secret_stays_private(
            field_selector="#pre-focus-secret",
            declared_field="pre-focus-secret",
            secret=pre_focus_secret,
            output_name="recording-pre-focus-secret",
            remove_identity_before_focus=True,
        )
        attribute_secret = "ATTRIBUTE-MUTATION-SECRET-NEVER-PERSIST"
        assert_mutating_secret_stays_private(
            field_selector="#sticky-secret",
            declared_field="sticky-secret",
            secret=attribute_secret,
            output_name="recording-sticky-secret",
        )
        replacement_secret = "REPLACEMENT-SECRET-NEVER-PERSIST"
        assert_mutating_secret_stays_private(
            field_selector="#replacement-secret",
            declared_field="replacement-secret",
            secret=replacement_secret,
            output_name="recording-replacement-secret",
        )
        captured_output = capsys.readouterr()
        for secret in (pre_focus_secret, attribute_secret, replacement_secret):
            assert secret not in captured_output.out
            assert secret not in captured_output.err
        assert process.poll() is None

        dynamic_secret = "DYNAMIC-SECRET-LITERAL-NEVER-PERSIST"
        dynamic_secret_rect: dict[str, int] = {}

        def add_remove_and_type_dynamic_secret(page, pump):
            rect = page.evaluate(
                """secret => {
                  const field = document.createElement('input');
                  field.id = 'dynamic-secret';
                  field.name = 'dynamic-secret';
                  field.style.cssText = 'width:220px;height:40px;border:0';
                  document.body.appendChild(field);
                  field.removeAttribute('name');
                  field.removeAttribute('id');
                  field.value = secret;
                  field.dispatchEvent(new Event('input', {bubbles: true}));
                  const box = field.getBoundingClientRect();
                  return {
                    x: Math.round(box.left), y: Math.round(box.top),
                    width: Math.round(box.width), height: Math.round(box.height),
                  };
                }""",
                dynamic_secret,
            )
            dynamic_secret_rect.update(rect)
            pump()
            pump()

        dynamic_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-dynamic-secret",
            secret_fields=("dynamic-secret",),
            cdp_endpoint=endpoint,
            script=add_remove_and_type_dynamic_secret,
        )
        for artifact in dynamic_recording.rglob("*"):
            if artifact.is_file():
                assert dynamic_secret.encode() not in artifact.read_bytes()
        dynamic_events = [
            json.loads(line)
            for line in (dynamic_recording / "events.jsonl").read_text().splitlines()
        ]
        assert len(dynamic_events) == 1
        assert dynamic_events[0].get("secret") is True
        dynamic_after = Image.open(
            dynamic_recording / "frames" / "0000_after.png"
        ).convert("RGB")
        dynamic_crop = dynamic_after.crop(
            (
                dynamic_secret_rect["x"],
                dynamic_secret_rect["y"],
                dynamic_secret_rect["x"] + dynamic_secret_rect["width"],
                dynamic_secret_rect["y"] + dynamic_secret_rect["height"],
            )
        )
        assert all(extrema == (0, 0) for extrema in dynamic_crop.getextrema())
        assert process.poll() is None

        moved_secret = "MOVED-FINAL-SECRET-NEVER-PERSIST"
        moved_secret_rect: dict[str, int] = {}

        def move_secret_and_finish_without_pump(page, _pump):
            rect = page.evaluate(
                """secret => {
                  const field = document.createElement('input');
                  field.id = 'moved-final-secret';
                  field.name = 'moved-final-secret';
                  field.dataset.oaMovedFinalSecret = 'yes';
                  field.style.cssText = [
                    'position:fixed', 'left:20px', 'top:180px',
                    'width:220px', 'height:40px', 'border:0', 'z-index:1000',
                  ].join(';');
                  document.body.appendChild(field);
                  field.removeAttribute('name');
                  field.removeAttribute('id');
                  field.value = secret;
                  field.dispatchEvent(new Event('input', {bubbles: true}));
                  field.style.left = '700px';
                  field.style.top = '300px';
                  const box = field.getBoundingClientRect();
                  return {
                    x: Math.round(box.left), y: Math.round(box.top),
                    width: Math.round(box.width), height: Math.round(box.height),
                  };
                }""",
                moved_secret,
            )
            moved_secret_rect.update(rect)

        moved_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-moved-final-secret",
            secret_fields=("moved-final-secret",),
            cdp_endpoint=endpoint,
            script=move_secret_and_finish_without_pump,
        )
        for artifact in moved_recording.rglob("*"):
            if artifact.is_file():
                assert moved_secret.encode() not in artifact.read_bytes()
        moved_events = [
            json.loads(line)
            for line in (moved_recording / "events.jsonl").read_text().splitlines()
        ]
        assert len(moved_events) == 1
        assert moved_events[0].get("secret") is True
        moved_after = Image.open(moved_recording / "frames" / "0000_after.png").convert(
            "RGB"
        )
        moved_crop = moved_after.crop(
            (
                moved_secret_rect["x"],
                moved_secret_rect["y"],
                moved_secret_rect["x"] + moved_secret_rect["width"],
                moved_secret_rect["y"] + moved_secret_rect["height"],
            )
        )
        assert all(extrema == (0, 0) for extrema in moved_crop.getextrema())
        from playwright.sync_api import sync_playwright

        with sync_playwright() as cleanup_playwright:
            cleanup_browser = cleanup_playwright.chromium.connect_over_cdp(endpoint)
            cleanup_page = select_attached_page(
                cleanup_browser,
                app_url=attach_app_url,
            )
            cleanup_page.evaluate(
                """() => document.querySelector(
                  '[data-oa-moved-final-secret="yes"]'
                ).remove()"""
            )
        assert process.poll() is None

        detached_marker_session = InteractiveRecorder(
            attach_app_url,
            tmp_path / "recording-detached-secret-marker",
            secret_fields=("detached-cleanup-secret",),
            cdp_endpoint=endpoint,
        )
        detached_marker_session.start()
        assert detached_marker_session.page is not None
        detached_marker_session.page.evaluate(
            """() => {
              const field = document.createElement('input');
              field.name = 'detached-cleanup-secret';
              document.body.appendChild(field);
              window.__detachedOaSecret = field;
            }"""
        )
        detached_marker_session.page.wait_for_timeout(0)
        assert detached_marker_session.page.evaluate(
            """() => Array.from(window.__detachedOaSecret.attributes)
              .some((attribute) => attribute.name.startsWith('data-oaflow-secret-'))"""
        )
        detached_marker_session.page.evaluate(
            "() => window.__detachedOaSecret.remove()"
        )
        detached_marker_session.finish()

        detached_marker_probe = InteractiveRecorder(
            attach_app_url,
            tmp_path / "recording-detached-secret-marker-probe",
            cdp_endpoint=endpoint,
        )
        detached_marker_probe.start()
        assert detached_marker_probe.page is not None
        assert not detached_marker_probe.page.evaluate(
            """() => Array.from(window.__detachedOaSecret.attributes)
              .some((attribute) => attribute.name.startsWith('data-oaflow-secret-'))"""
        )
        detached_marker_probe.page.evaluate("() => delete window.__detachedOaSecret")
        detached_marker_probe.abort()
        assert process.poll() is None

        child_secret_rect: dict[str, int] = {}

        def retain_top_level_action_with_child_secret(page, pump):
            child_secret = page.frame_locator("#child").locator("#frame-password")
            box = child_secret.bounding_box()
            assert box is not None
            child_secret_rect.update(
                x=round(box["x"]),
                y=round(box["y"]),
                width=round(box["width"]),
                height=round(box["height"]),
            )
            page.click("#note")
            pump()
            pump()

        child_secret_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-child-frame-secret",
            cdp_endpoint=endpoint,
            script=retain_top_level_action_with_child_secret,
        )
        child_before = Image.open(
            child_secret_recording / "frames" / "0000_before.png"
        ).convert("RGB")
        child_crop = child_before.crop(
            (
                child_secret_rect["x"],
                child_secret_rect["y"],
                child_secret_rect["x"] + child_secret_rect["width"],
                child_secret_rect["y"] + child_secret_rect["height"],
            )
        )
        assert all(extrema == (0, 0) for extrema in child_crop.getextrema())
        assert process.poll() is None

        frame_race_session = InteractiveRecorder(
            attach_app_url,
            tmp_path / "recording-frame-race-probe",
            cdp_endpoint=endpoint,
        )
        frame_race_session.start()
        try:
            race_page = frame_race_session.page
            race_backend = frame_race_session.backend
            assert race_page is not None and race_backend is not None
            original_screenshot = race_page.screenshot
            masked_races = 0
            for trial in range(30):
                race_page.evaluate(
                    """() => {
                      const previous = document.querySelector('#race-frame');
                      if (previous) previous.remove();
                    }"""
                )
                race_page.wait_for_timeout(0)
                state = {"attach": True}

                def attach_after_frame_snapshot(**kwargs):
                    if state["attach"]:
                        state["attach"] = False
                        race_page.evaluate(
                            """trial => {
                              const frame = document.createElement('iframe');
                              frame.id = 'race-frame';
                              frame.style.cssText = [
                                'position:fixed', 'left:20px', 'top:160px',
                                'width:220px', 'height:80px', 'border:0',
                              ].join(';');
                              frame.srcdoc = `<input id="race-password"
                                type="password" value="RACE-${trial}"
                                style="width:180px;height:40px;border:0">`;
                              document.body.appendChild(frame);
                            }""",
                            trial,
                        )
                    return original_screenshot(**kwargs)

                with monkeypatch.context() as patch_context:
                    patch_context.setattr(
                        race_page,
                        "screenshot",
                        attach_after_frame_snapshot,
                    )
                    png = race_backend.screenshot()
                password = race_page.frame_locator("#race-frame").locator(
                    "#race-password"
                )
                box = password.bounding_box()
                assert box is not None
                image = Image.open(BytesIO(png)).convert("RGB")
                crop = image.crop(
                    (
                        round(box["x"]),
                        round(box["y"]),
                        round(box["x"] + box["width"]),
                        round(box["y"] + box["height"]),
                    )
                )
                assert all(extrema == (0, 0) for extrema in crop.getextrema())
                masked_races += 1
            assert masked_races == 30

            churn_attempts = 0

            def attach_and_detach_during_every_capture(**kwargs):
                nonlocal churn_attempts
                churn_attempts += 1
                race_page.evaluate(
                    """attempt => {
                      const frame = document.createElement('iframe');
                      frame.id = `churn-${attempt}`;
                      frame.srcdoc = '<input type="password" value="churn">';
                      document.body.appendChild(frame);
                      frame.remove();
                    }""",
                    churn_attempts,
                )
                return original_screenshot(**kwargs)

            with monkeypatch.context() as patch_context:
                patch_context.setattr(
                    race_page,
                    "screenshot",
                    attach_and_detach_during_every_capture,
                )
                with pytest.raises(ScreenshotMaskStabilityError, match="frame tree"):
                    race_backend.screenshot()
                assert churn_attempts == 3
        finally:
            if frame_race_session.page is not None:
                frame_race_session.page.evaluate(
                    """() => {
                      const frame = document.querySelector('#race-frame');
                      if (frame) frame.remove();
                    }"""
                )
            frame_race_session.abort()
        assert not (tmp_path / "recording-frame-race-probe").exists()
        assert process.poll() is None

        interleaved_recording = tmp_path / "recording-interleaved-action-refusal"
        interleaved_session = InteractiveRecorder(
            attach_app_url,
            interleaved_recording,
            cdp_endpoint=endpoint,
        )
        interleaved_session.start()
        assert interleaved_session.page is not None
        assert interleaved_session.backend is not None
        interleaved_session.page.click("#note")
        original_backend_screenshot = interleaved_session.backend.screenshot
        interleaved_action_injected = False

        def screenshot_after_second_action() -> bytes:
            nonlocal interleaved_action_injected
            if not interleaved_action_injected:
                interleaved_action_injected = True
                interleaved_session.page.click("#save")
                interleaved_session.page.wait_for_timeout(0)
            return original_backend_screenshot()

        monkeypatch.setattr(
            interleaved_session.backend,
            "screenshot",
            screenshot_after_second_action,
        )
        try:
            with pytest.raises(BrowserAttachError, match="more than one logical"):
                interleaved_session.pump()
        finally:
            interleaved_session.abort()
        assert interleaved_action_injected
        assert not (interleaved_recording / "meta.json").exists()
        assert process.poll() is None

        def rapid_pointer_pointer(page, pump):
            page.click("#note")
            page.click("#save")
            pump()

        def rapid_input_submit(page, pump):
            page.evaluate("document.querySelector('#note').value = ''")
            page.click("#note")
            pump()
            page.keyboard.type("rapid-input-submit")
            page.click("#save")
            pump()

        def rapid_input_enter(page, pump):
            page.evaluate("document.querySelector('#note').value = ''")
            page.click("#note")
            pump()
            page.keyboard.type("rapid-input-enter")
            page.keyboard.press("Enter")
            pump()

        def rapid_scroll_click(page, pump):
            page.mouse.wheel(0, 40)
            page.click("#note")
            pump()

        for case_name, drive_rapid_actions in (
            ("pointer-pointer", rapid_pointer_pointer),
            ("input-submit", rapid_input_submit),
            ("input-enter", rapid_input_enter),
            ("scroll-click", rapid_scroll_click),
        ):
            rapid_recording = tmp_path / f"recording-rapid-{case_name}"
            with pytest.raises(BrowserAttachError, match="more than one logical"):
                record_interactive(
                    attach_app_url,
                    rapid_recording,
                    cdp_endpoint=endpoint,
                    script=drive_rapid_actions,
                )
            assert not (rapid_recording / "meta.json").exists()
            assert process.poll() is None

        def coalesce_one_field(page, pump):
            page.evaluate("document.querySelector('#note').value = ''")
            page.click("#note")
            pump()
            page.keyboard.type("same-field-input-coalesces")
            pump()
            pump()

        coalesced_recording = record_interactive(
            attach_app_url,
            tmp_path / "recording-coalesced-input",
            cdp_endpoint=endpoint,
            script=coalesce_one_field,
        )
        coalesced_events = [
            json.loads(line)
            for line in (coalesced_recording / "events.jsonl").read_text().splitlines()
        ]
        assert (
            len([event for event in coalesced_events if event["kind"] == "type"]) == 1
        )
        assert process.poll() is None

        popup_recording = tmp_path / "recording-popup-refusal"

        def open_popup(page, pump):
            with page.expect_popup() as popup_info:
                page.click("#open-popup")
            popup_info.value.wait_for_load_state()
            pump()

        with pytest.raises(BrowserAttachError, match="popup or new tab"):
            record_interactive(
                attach_app_url,
                popup_recording,
                cdp_endpoint=endpoint,
                script=open_popup,
            )
        assert not (popup_recording / "meta.json").exists()
        assert process.poll() is None
        with urlopen(f"{endpoint}/json/list", timeout=2) as response:
            popup_targets = json.load(response)
        assert any(target.get("url") == "about:blank" for target in popup_targets)

        popup_activity_recording = tmp_path / "recording-popup-activity-refusal"

        def act_inside_popup(page, pump):
            with page.expect_popup() as popup_info:
                page.click("#open-popup")
            popup = popup_info.value
            popup.set_content(
                "<input id='popup-note'><button id='popup-save'>Save</button>"
            )
            popup.fill("#popup-note", "activity-that-must-not-disappear")
            popup.click("#popup-save")
            pump()

        with pytest.raises(BrowserAttachError, match="popup or new tab"):
            record_interactive(
                attach_app_url,
                popup_activity_recording,
                cdp_endpoint=endpoint,
                script=act_inside_popup,
            )
        assert not (popup_activity_recording / "meta.json").exists()
        assert process.poll() is None
        with urlopen(f"{endpoint}/json/version", timeout=2) as response:
            assert response.status == 200

        short_page_recording = tmp_path / "recording-short-page-refusal"

        def act_in_short_lived_context_page(page, pump):
            temporary = page.context.new_page()
            temporary.set_content(
                "<input id='short-note'><button id='short-save'>Save</button>"
            )
            temporary.fill("#short-note", "activity-that-must-not-disappear")
            temporary.click("#short-save")
            temporary.close()
            assert len(page.context.pages) >= 1
            pump()

        with pytest.raises(BrowserAttachError, match="popup or new tab"):
            record_interactive(
                attach_app_url,
                short_page_recording,
                cdp_endpoint=endpoint,
                script=act_in_short_lived_context_page,
            )
        assert not (short_page_recording / "meta.json").exists()
        assert process.poll() is None
        with urlopen(f"{endpoint}/json/version", timeout=2) as response:
            assert response.status == 200

        prebaseline_recording = tmp_path / "recording-prebaseline-page-refusal"
        original_select_attached_page = interactive_recorder_module.select_attached_page
        prebaseline_page_acted = False

        def select_after_short_lived_page(browser, *, app_url, page_url=None):
            nonlocal prebaseline_page_acted
            selected = original_select_attached_page(
                browser,
                app_url=app_url,
                page_url=page_url,
            )
            temporary = selected.context.new_page()
            temporary.set_content(
                "<input id='pre-note'><button id='pre-save'>Save</button>"
            )
            temporary.fill("#pre-note", "prebaseline-activity-must-not-disappear")
            temporary.click("#pre-save")
            prebaseline_page_acted = True
            temporary.close()
            return selected

        with monkeypatch.context() as patch_context:
            patch_context.setattr(
                interactive_recorder_module,
                "select_attached_page",
                select_after_short_lived_page,
            )
            with pytest.raises(BrowserAttachError, match="popup or new tab"):
                record_interactive(
                    attach_app_url,
                    prebaseline_recording,
                    cdp_endpoint=endpoint,
                    script=lambda _page, _pump: None,
                )
        assert prebaseline_page_acted
        assert not (prebaseline_recording / "meta.json").exists()
        assert process.poll() is None

        baseline_getter_recording = tmp_path / "recording-baseline-getter-refusal"
        from playwright.sync_api import BrowserContext

        original_pages_getter = BrowserContext.pages.fget
        assert original_pages_getter is not None
        baseline_getter_page_acted = False

        def pages_with_short_lived_action(context):
            nonlocal baseline_getter_page_acted
            existing = original_pages_getter(context)
            if not baseline_getter_page_acted:
                baseline_getter_page_acted = True
                temporary = context.new_page()
                temporary.set_content(
                    "<input id='gap-note'><button id='gap-save'>Save</button>"
                )
                temporary.fill("#gap-note", "baseline-gap-action-must-not-disappear")
                temporary.click("#gap-save")
                temporary.close()
            return existing

        with monkeypatch.context() as patch_context:
            patch_context.setattr(
                BrowserContext,
                "pages",
                property(pages_with_short_lived_action),
            )
            with pytest.raises(BrowserAttachError, match="popup or new tab"):
                record_interactive(
                    attach_app_url,
                    baseline_getter_recording,
                    cdp_endpoint=endpoint,
                    script=lambda _page, _pump: None,
                )
        assert baseline_getter_page_acted
        assert not (baseline_getter_recording / "meta.json").exists()
        assert process.poll() is None

        late_page_recording = tmp_path / "recording-late-page-refusal"
        late_page_session = InteractiveRecorder(
            attach_app_url,
            late_page_recording,
            cdp_endpoint=endpoint,
        )
        late_page_session.start()
        assert late_page_session.page is not None
        assert late_page_session._pw is not None
        original_playwright_stop = late_page_session._pw.stop

        def stop_after_short_lived_page() -> None:
            temporary = late_page_session.page.context.new_page()
            temporary.set_content(
                "<input id='late-note'><button id='late-save'>Save</button>"
            )
            temporary.fill("#late-note", "late-activity-must-not-disappear")
            temporary.click("#late-save")
            temporary.close()
            original_playwright_stop()

        monkeypatch.setattr(late_page_session._pw, "stop", stop_after_short_lived_page)
        with pytest.raises(BrowserAttachError, match="popup or new tab"):
            late_page_session.finish()
        assert not (late_page_recording / "meta.json").exists()
        assert process.poll() is None
        with urlopen(f"{endpoint}/json/version", timeout=2) as response:
            assert response.status == 200

        cleanup_frame_recording = tmp_path / "recording-cleanup-frame-refusal"
        cleanup_frame_session = InteractiveRecorder(
            attach_app_url,
            cleanup_frame_recording,
            cdp_endpoint=endpoint,
        )
        cleanup_frame_session.start()
        assert cleanup_frame_session.page is not None
        original_cleanup = cleanup_frame_session._cleanup_page_listeners
        cleanup_race_injected = False

        def cleanup_after_frame_race() -> None:
            nonlocal cleanup_race_injected
            if not cleanup_race_injected:
                cleanup_race_injected = True
                cleanup_frame_session.page.evaluate(
                    """() => {
                      const frame = document.createElement('iframe');
                      frame.srcdoc = '<input type="password" value="late">';
                      document.body.appendChild(frame);
                      frame.remove();
                    }"""
                )
                cleanup_frame_session.page.wait_for_timeout(0)
            original_cleanup()

        monkeypatch.setattr(
            cleanup_frame_session,
            "_cleanup_page_listeners",
            cleanup_after_frame_race,
        )
        with pytest.raises(BrowserAttachError, match="changed frame state"):
            cleanup_frame_session.finish()
        assert cleanup_race_injected
        assert not (cleanup_frame_recording / "meta.json").exists()
        assert process.poll() is None

        iframe_recording = tmp_path / "recording-iframe-refusal"

        def click_inside_existing_iframe(page, pump):
            page.frame_locator("#child").locator("#inside").click()
            pump()

        with pytest.raises(BrowserAttachError, match="iframe"):
            record_interactive(
                attach_app_url,
                iframe_recording,
                cdp_endpoint=endpoint,
                script=click_inside_existing_iframe,
            )
        assert not (iframe_recording / "meta.json").exists()
        assert process.poll() is None

        origin_bounce_recording = tmp_path / "recording-origin-bounce-refusal"
        other_origin = attach_app_url.replace("127.0.0.1", "localhost")

        def leave_origin_and_return(page, pump):
            page.goto(other_origin)
            page.goto(attach_app_url)
            pump()

        with pytest.raises(BrowserAttachError, match="left the declared"):
            record_interactive(
                attach_app_url,
                origin_bounce_recording,
                cdp_endpoint=endpoint,
                script=leave_origin_and_return,
            )
        assert not origin_bounce_recording.exists()
        assert process.poll() is None

        overlap_recording = tmp_path / "recording-resize-overlap-refusal"

        def resize_during_action(page, pump):
            cdp = page.context.new_cdp_session(page)
            cdp.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 1000,
                    "height": 650,
                    "deviceScaleFactor": 2,
                    "mobile": False,
                },
            )
            page.click("#note")
            pump()

        with pytest.raises(BrowserAttachError, match="overlapped"):
            record_interactive(
                attach_app_url,
                overlap_recording,
                cdp_endpoint=endpoint,
                script=resize_during_action,
            )
        assert not (overlap_recording / "meta.json").exists()
        assert process.poll() is None

        resized_recording = tmp_path / "recording-resized"

        def resize_then_record(page, pump):
            page.evaluate("document.querySelector('#note').value = ''")
            page.click("#note")
            pump()
            page.keyboard.type("before-resize")
            pump()
            pump()
            cdp = page.context.new_cdp_session(page)
            cdp.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 900,
                    "height": 600,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                },
            )
            pump()
            page.click("#note")
            pump()
            page.keyboard.type("after-resize")
            pump()
            pump()

        recording = record_interactive(
            attach_app_url,
            resized_recording,
            cdp_endpoint=endpoint,
            script=resize_then_record,
        )
        meta = json.loads((recording / "meta.json").read_text())
        events = [
            json.loads(line)
            for line in (recording / "events.jsonl").read_text().splitlines()
        ]
        assert meta["viewport_mode"] == "per-event"
        assert meta["viewport_history"][-1]["viewport"] == [900, 600]
        assert len(meta["viewport_history"]) >= 2
        assert events
        assert {tuple(event["viewport_before"]) for event in events} == {
            (1000, 650),
            (900, 600),
        }
        assert all(
            event["viewport_before"] == event["viewport_after"] for event in events
        )
        resized_bundle = tmp_path / "bundle-resized"
        resized_workflow = compile_recording(
            recording,
            resized_bundle,
            name="attached-browser-resized",
        )
        assert resized_workflow.steps
        assert process.poll() is None
        with urlopen(f"{endpoint}/json/version", timeout=2) as response:
            assert response.status == 200
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

"""Safe attachment of the Playwright recorder to an existing Chromium tab."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

from openadapt_flow.__main__ import main
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.compiler import compile_recording
from openadapt_flow.interactive_recorder import (
    BrowserAttachError,
    InteractiveRecorder,
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
  <input id="password" name="password" type="password">
  <button id="save" onclick="document.body.dataset.saved='yes'">Save</button>
  <iframe id="child" srcdoc="<button id='inside'>Inside frame</button>"></iframe>
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

            def drive(page, pump, *, secret_value=secret, trial_number=trial):
                page.evaluate(
                    """() => {
                      document.querySelector('#note').value = '';
                      document.querySelector('#password').value = '';
                      delete document.body.dataset.saved;
                    }"""
                )
                page.click("#note")
                page.keyboard.type(f"trial-{trial_number}")
                pump()
                pump()
                page.click("#password")
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

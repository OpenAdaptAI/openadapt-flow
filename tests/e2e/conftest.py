"""Session fixtures for the record -> compile -> replay E2E suite.

One MockMed server, one recorded demonstration, and one compiled bundle are
shared across every scenario (session-scoped, via ``tmp_path_factory``); each
replay gets a FRESH Playwright page (fresh MockMed JS state) from a shared
chromium instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

import pytest

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.compiler import compile_recording
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.mockmed.server import serve
from openadapt_flow.runtime import Replayer

NOTE_TEXT = "E2E triage booking three months"
PARAMS = {"note": NOTE_TEXT}
VIEWPORT = {"width": 1280, "height": 800}


@pytest.fixture(scope="session")
def mockmed_url() -> Iterator[str]:
    """Serve the MockMed static app once for the whole E2E session."""
    url, stop = serve(port=0)
    yield url
    stop()


def drift_url(base_url: str, drift: Optional[str]) -> str:
    """MockMed URL with an optional ``?drift=...`` query."""
    if not drift:
        return base_url
    return f"{base_url.rstrip('/')}/?drift={drift}"


@pytest.fixture(scope="session")
def recording_dir(mockmed_url: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Record the canonical triage demo once against default MockMed."""
    from openadapt_flow.demo_driver import record_triage_demo

    out = tmp_path_factory.mktemp("recording")
    return record_triage_demo(mockmed_url, out, note_text=NOTE_TEXT)


@dataclass
class Bundle:
    """A compiled workflow bundle plus its in-memory Workflow."""

    dir: Path
    workflow: Workflow

    def fresh_workflow(self) -> Workflow:
        """Reload the workflow from disk (replays mutate it via healing)."""
        return Workflow.load(self.dir)


@pytest.fixture(scope="session")
def bundle(recording_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Bundle:
    """Compile the shared recording once."""
    out = tmp_path_factory.mktemp("bundle")
    workflow = compile_recording(recording_dir, out, name="triage-demo")
    return Bundle(dir=out, workflow=workflow)


@pytest.fixture(scope="module")
def _browser() -> Iterator[object]:
    """One headless chromium shared by all replays (pages are per-replay).

    Module-scoped (not session): the sync Playwright driver keeps its event
    loop suspended mid-``run_until_complete`` while alive, and only ONE sync
    Playwright can exist per thread. Session scope would keep it alive into
    later test modules (test_mockmed / test_recorder), which open their own
    sync Playwright and would fail with "Sync API inside the asyncio loop".
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


ReplayFn = Callable[..., tuple[RunReport, Path]]


@pytest.fixture(scope="module")
def replay(_browser, tmp_path_factory: pytest.TempPathFactory) -> ReplayFn:
    """Factory: replay a bundle dir against a URL with a fresh backend.

    Returns a callable ``replay(bundle_dir, url, *, params=PARAMS,
    run_dir=None, save_healed_to=None) -> (RunReport, run_dir)``. Each call
    opens a fresh page (fresh MockMed state), runs the Replayer, and closes
    the page.
    """

    def _replay(
        bundle_dir: Path,
        url: str,
        *,
        params: Optional[dict[str, str]] = None,
        run_dir: Optional[Path] = None,
        save_healed_to: Optional[Path] = None,
    ) -> tuple[RunReport, Path]:
        if run_dir is None:
            run_dir = tmp_path_factory.mktemp("run")
        page = _browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        try:
            page.goto(url)
            backend = PlaywrightBackend(page)
            workflow = Workflow.load(bundle_dir)
            report = Replayer(backend).run(
                workflow,
                params=dict(PARAMS if params is None else params),
                bundle_dir=Path(bundle_dir),
                run_dir=Path(run_dir),
                save_healed_to=save_healed_to,
            )
        finally:
            page.close()
        return report, Path(run_dir)

    return _replay

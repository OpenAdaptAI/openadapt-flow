"""Generic cross-frame structural resolution and one-shot delivery contracts."""

from __future__ import annotations

import pytest

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.ir import StructuralLocator


@pytest.fixture
def framed_backend():
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 800, "height": 500},
            device_scale_factor=1,
        )
        page.set_content(
            '<iframe id="shell" style="width:650px;height:360px"></iframe>'
        )
        outer = page.frames[-1]
        outer.set_content('<iframe id="app" style="width:500px;height:260px"></iframe>')
        frame = page.frames[-1]
        frame.set_content(
            """<!doctype html><html><body>
            <table><tr data-openadapt-identity="record-1">
              <td>Record 1</td><td><button id="target"
                onclick="window.clicks += 1">Submit</button></td>
            </tr></table><script>window.clicks = 0</script>
            </body></html>"""
        )
        yield PlaywrightBackend(page), page, frame
        browser.close()


def _target_locator(backend, frame):
    box = frame.locator("#target").bounding_box()
    assert box is not None
    x = int(round(box["x"] + box["width"] / 2))
    y = int(round(box["y"] + box["height"] / 2))
    locator = backend.structural_locator_at(x, y)
    assert locator is not None
    return locator, box


def _has_guard_attribute(frame) -> bool:
    return bool(
        frame.evaluate(
            """() => Array.from(document.querySelectorAll('*')).some(
                el => Array.from(el.attributes).some(
                    attr => attr.name.startsWith('data-openadapt-actuation-'))
            )"""
        )
    )


def test_nested_frame_locator_delivers_once_in_top_level_coordinates(
    framed_backend,
) -> None:
    backend, _page, frame = framed_backend
    locator, box = _target_locator(backend, frame)

    assert locator.frame_path == ["#shell", "#app"]
    handle = backend.locate_structural(locator)
    assert handle is not None
    assert handle.region == tuple(
        int(round(box[key])) for key in ("x", "y", "width", "height")
    )

    receipt = backend.act_structural(locator, handle)

    assert receipt.target_fingerprint == handle.target_fingerprint
    assert frame.evaluate("window.clicks") == 1
    with pytest.raises(StructuralResolutionRefused, match="stale|consumed"):
        backend.act_structural(locator, handle)


def test_frame_path_and_target_ambiguity_refuse_before_arming() -> None:
    sync = pytest.importorskip("playwright.sync_api")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 800, "height": 500})
        page.set_content("<iframe></iframe><iframe></iframe>")
        backend = PlaywrightBackend(page)
        locator = StructuralLocator(selector="#target", frame_path=["iframe"])

        with pytest.raises(
            StructuralResolutionRefused, match="frame path is ambiguous"
        ):
            backend.locate_structural(locator)
        assert all(not _has_guard_attribute(frame) for frame in page.frames[1:])
        browser.close()


def test_navigation_invalidates_child_guard_and_token_reuse(framed_backend) -> None:
    backend, _page, frame = framed_backend
    locator, _box = _target_locator(backend, frame)
    handle = backend.locate_structural(locator)
    assert handle is not None

    frame.set_content(
        '<button id="target" onclick="window.clicks += 1">Submit</button>'
        "<script>window.clicks = 0</script>"
    )
    with pytest.raises(StructuralResolutionRefused):
        backend.act_structural(locator, handle)
    assert frame.evaluate("window.clicks") == 0
    with pytest.raises(StructuralResolutionRefused, match="stale|consumed"):
        backend.act_structural(locator, handle)


def test_top_level_context_change_refuses_child_frame_delivery(framed_backend) -> None:
    backend, page, frame = framed_backend
    page.locator("head").evaluate(
        "el => el.insertAdjacentHTML('beforeend', "
        '\'<meta name="openadapt-session-identity" content="\' + '
        "'a'.repeat(64) + '\">')"
    )
    locator, _box = _target_locator(backend, frame)
    handle = backend.locate_structural(locator)
    assert handle is not None
    assert backend.session_identity() == "a" * 64

    page.locator('meta[name="openadapt-session-identity"]').evaluate(
        "el => el.content = 'b'.repeat(64)"
    )
    with pytest.raises(StructuralResolutionRefused, match="execution context"):
        backend.act_structural(locator, handle)
    assert frame.evaluate("window.clicks") == 0
    assert not _has_guard_attribute(frame)


def test_cancel_and_actuation_exception_always_clean_child_guard(
    framed_backend,
) -> None:
    backend, _page, frame = framed_backend
    locator, _box = _target_locator(backend, frame)
    cancelled = backend.locate_structural(locator)
    assert cancelled is not None and _has_guard_attribute(frame)
    backend.cancel_pending_structural_guards()
    assert not _has_guard_attribute(frame)

    handle = backend.locate_structural(locator)
    assert handle is not None
    frame.locator("body").evaluate(
        """body => body.insertAdjacentHTML('beforeend',
        '<div style="position:fixed;inset:0;z-index:99"></div>')"""
    )
    with pytest.raises(StructuralResolutionRefused, match="unactionable"):
        backend.act_structural(locator, handle)
    assert frame.evaluate("window.clicks") == 0
    assert not _has_guard_attribute(frame)

"""Shared helpers for the adversarial-validation E2E suites.

Used by ``test_perturbation.py`` (Track A: environment/data drift),
``test_chaos.py`` (Track B: mid-run fault injection), and
``test_primitives.py`` (Track C: interaction-primitive taxonomy). See
``docs/validation/VALIDATION.md`` for the experiment matrix these suites
automate and the observed outcomes they pin down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.runtime import Replayer


class ChaosBackend(PlaywrightBackend):
    """Backend wrapper that sabotages the app mid-run.

    Fires ``inject(page)`` exactly once, immediately after the Nth click or
    the Nth ``type_text`` (whichever trigger is configured) — i.e. between
    the triggering step's action and the next step's resolution, which is
    where real-world state changes land.
    """

    def __init__(
        self,
        page,
        *,
        inject: Callable[[object], None],
        after_click: Optional[int] = None,
        after_type: Optional[int] = None,
    ) -> None:
        super().__init__(page)
        self._inject = inject
        self._after_click = after_click
        self._after_type = after_type
        self._clicks = 0
        self._types = 0
        self.fired = False

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        super().click(x, y, double=double)
        self._clicks += 1
        if (
            not self.fired
            and self._after_click is not None
            and self._clicks == self._after_click
        ):
            self.fired = True
            self._inject(self.page)

    def type_text(self, text: str) -> None:
        super().type_text(text)
        self._types += 1
        if (
            not self.fired
            and self._after_type is not None
            and self._types == self._after_type
        ):
            self.fired = True
            self._inject(self.page)


def replay_on_page(
    browser,
    bundle_dir: Path,
    url: str,
    run_dir: Path,
    *,
    params: dict[str, str],
    viewport: tuple[int, int] = (1280, 800),
    device_scale_factor: int = 1,
    backend_factory: Optional[Callable[[object], PlaywrightBackend]] = None,
) -> tuple[RunReport, dict]:
    """Replay ``bundle_dir`` on a fresh page and observe final app state.

    Unlike the ``replay`` fixture in ``conftest.py``, this helper controls
    the page's viewport/device-scale-factor and can wrap the backend (fault
    injection), and it returns ground truth read from the live app AFTER the
    run — which is what lets a test distinguish a *safe halt* from a *wrong
    action that the report never noticed*.

    Returns:
        ``(report, state)`` where ``state`` has ``hash`` (location.hash),
        ``banner`` (text of ``#saved-banner`` or None), ``enc_item`` (text
        of the first ``.enc-item`` or None), and ``status`` (text of
        ``#status`` or None, for the widgets page).
    """
    page = browser.new_page(
        viewport={"width": viewport[0], "height": viewport[1]},
        device_scale_factor=device_scale_factor,
    )
    try:
        page.goto(url)
        backend = (
            backend_factory(page) if backend_factory else PlaywrightBackend(page)
        )
        workflow = Workflow.load(bundle_dir)
        report = Replayer(backend).run(
            workflow,
            params=params,
            bundle_dir=Path(bundle_dir),
            run_dir=Path(run_dir),
        )
        state = {
            "hash": page.evaluate("location.hash"),
            "banner": page.evaluate(
                "(document.getElementById('saved-banner') || {}).textContent"
                " || null"
            ),
            "enc_item": page.evaluate(
                "(document.querySelector('.enc-item') || {}).textContent"
                " || null"
            ),
            "status": page.evaluate(
                "(document.getElementById('status') || {}).textContent"
                " || null"
            ),
        }
    finally:
        page.close()
    return report, state


def failing_step(report: RunReport):
    """The first failed StepResult, or None."""
    for result in report.results:
        if not result.ok:
            return result
    return None


def describe(report: RunReport, state: dict) -> str:
    """Compact run description for assertion messages."""
    lines = [
        f"success={report.success} rungs={report.rung_counts} "
        f"heals={report.heal_count} state={state}"
    ]
    for r in report.results:
        rung = r.resolution.rung if r.resolution else "-"
        lines.append(
            f"  {r.step_id} ok={r.ok} rung={rung} pc={r.postconditions_ok} "
            f"err={r.error}"
        )
    return "\n".join(lines)

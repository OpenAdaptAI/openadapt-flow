"""Replayer: execute a compiled Workflow against a Backend.

Per step: settle, screenshot, resolve the anchor via the resolution ladder,
enforce the irreversible-step risk gate, act through the Backend, settle
again, and poll postconditions until they pass or time out (with one
re-settle retry). Postcondition failure is semantic drift: the run halts,
naming the step and embedding its before/after screenshots in the report.

Steps that succeed via any rung other than ``template`` are healed: the
anchor is refreshed from the live frame, the heal is recorded under
``run_dir/heals/<step_id>/``, and — when ``save_healed_to`` is set — a full
healed bundle is written.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.backend import Backend
from openadapt_flow.ir import (
    ActionKind,
    Region,
    Resolution,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime import heal as heal_mod
from openadapt_flow.runtime.resolver import is_below_ocr, pad_region, resolve

# REGION_STABLE template check: how far the expected content may shift from
# the recorded region (real apps re-layout by a few pixels between runs),
# and the minimum template-match score to accept it.
PC_TEMPLATE_SEARCH_PAD = 80
PC_TEMPLATE_THRESHOLD = 0.9

# Closed-loop scroll: a SCROLL step keeps scrolling by its recorded delta
# until the NEXT anchored step's anchor resolves on a settled frame, bounded
# by this multiple of the step's own recorded scroll distance. Consecutive
# SCROLL steps hand the loop to each other (each probes first and no-ops
# once the anchor is in view), so a run of N recorded scrolls has a combined
# budget of ~2.5x the total recorded distance.
SCROLL_BUDGET_FACTOR = 2.5


class Replayer:
    """Replays a Workflow against a Backend using injected vision.

    Args:
        backend: The Backend to act through (screenshot/click/type/press).
        vision: Namespace-like object exposing ``find_template``,
            ``find_text``, ``ocr``, ``phash_png``, ``phash_distance``, and
            ``wait_settled``. Defaults to the real ``openadapt_flow.vision``
            module, imported lazily so unit tests can inject a fake without
            the OCR stack ever loading.
        grounder: Optional Grounder used as the last resolution rung.
        poll_interval_s: Postcondition polling interval in seconds.
    """

    def __init__(
        self,
        backend: Backend,
        *,
        vision: Optional[Any] = None,
        grounder: Optional[Any] = None,
        poll_interval_s: float = 0.05,
    ) -> None:
        if vision is None:
            import openadapt_flow.vision as vision  # lazy: heavy OCR deps

        self.backend = backend
        self.vision = vision
        self.grounder = grounder
        self.poll_interval_s = poll_interval_s

    # -- public API ----------------------------------------------------------

    def run(
        self,
        workflow: Workflow,
        *,
        params: Optional[dict[str, str]] = None,
        bundle_dir: Path,
        run_dir: Path,
        save_healed_to: Optional[Path] = None,
    ) -> RunReport:
        """Execute the workflow and write a run directory.

        Args:
            workflow: The compiled workflow. Heals are applied to this
                in-memory object as the run progresses.
            params: Values for parameterized TYPE steps (``step.param``).
                Parameters not supplied here fall back to the recorded
                example/default values in ``workflow.params``.
            bundle_dir: The workflow bundle directory (source of template
                crops).
            run_dir: Output directory for report.json, per-step screenshots
                (``steps/``), and heal artifacts (``heals/``).
            save_healed_to: When set, write a full healed bundle (updated
                workflow.json + new and unchanged template crops) here.

        Returns:
            The RunReport (also saved as ``run_dir/report.json``). The run
            aborts at the first failed step; ``success`` is True only if
            every step completed.
        """
        bundle_dir = Path(bundle_dir)
        run_dir = Path(run_dir)
        (run_dir / "steps").mkdir(parents=True, exist_ok=True)
        # Caller-supplied params override the recorded defaults; a bundle
        # with recorded example values replays without any explicit params.
        params = {**workflow.params, **(params or {})}

        report = RunReport(
            workflow_name=workflow.name,
            started_at=datetime.now(timezone.utc).isoformat(),
            params=params,
        )
        new_crops: dict[str, bytes] = {}
        t_run = time.monotonic()

        for step_index, step in enumerate(workflow.steps):
            result = self._run_step(
                step,
                workflow=workflow,
                step_index=step_index,
                params=params,
                bundle_dir=bundle_dir,
                run_dir=run_dir,
                new_crops=new_crops,
            )
            report.results.append(result)
            if result.ok and result.resolution is not None:
                rung = result.resolution.rung
                report.rung_counts[rung] = report.rung_counts.get(rung, 0) + 1
                if rung == "grounder":
                    report.model_calls += 1
            if result.heal is not None:
                report.heal_count += 1
            if not result.ok:
                break

        report.success = len(report.results) == len(workflow.steps) and all(
            result.ok for result in report.results
        )
        report.total_ms = (time.monotonic() - t_run) * 1000.0

        if save_healed_to is not None:
            heal_mod.write_healed_bundle(
                workflow, bundle_dir, Path(save_healed_to), new_crops
            )

        report.save(run_dir)
        return report

    # -- per-step execution ---------------------------------------------------

    def _run_step(
        self,
        step: Step,
        *,
        workflow: Workflow,
        step_index: int,
        params: dict[str, str],
        bundle_dir: Path,
        run_dir: Path,
        new_crops: dict[str, bytes],
    ) -> StepResult:
        """Execute a single step; never raises (failures land in the result)."""
        t0 = time.monotonic()
        result = StepResult(step_id=step.id, intent=step.intent, ok=False)

        # Settle before the pre-action screenshot.
        before_png = self.vision.wait_settled(self.backend)
        result.before_png = self._save_step_png(run_dir, step.id, "before", before_png)
        last_frame = before_png

        try:
            resolution, matched_region, error = self._resolve_step(
                step, before_png, bundle_dir
            )
            # Retry ladder failures with fresh settled frames until
            # ``step.timeout_s``: a remote app can present a settled-looking
            # but still-loading frame (wait_settled times out), and the
            # target only appears moments later. Structural errors (missing
            # anchor) and the risk gate (resolution is not None) never retry.
            deadline = t0 + step.timeout_s
            while (
                error is not None
                and resolution is None
                and step.anchor is not None
                and time.monotonic() < deadline
            ):
                time.sleep(self.poll_interval_s)
                before_png = self.vision.wait_settled(self.backend)
                result.before_png = self._save_step_png(
                    run_dir, step.id, "before", before_png
                )
                last_frame = before_png
                resolution, matched_region, error = self._resolve_step(
                    step, before_png, bundle_dir
                )
            result.resolution = resolution
            if error is None:
                error = self._act(
                    step,
                    resolution,
                    params,
                    workflow=workflow,
                    step_index=step_index,
                    bundle_dir=bundle_dir,
                    before_png=before_png,
                )

            if error is None:
                after_png = self.vision.wait_settled(self.backend)
                last_frame = after_png
                postconditions_ok, last_frame, failed = (
                    self._check_postconditions(step, after_png, bundle_dir)
                )
                result.postconditions_ok = postconditions_ok
                if not postconditions_ok:
                    detail = "; ".join(failed) or "unknown postcondition"
                    error = (
                        f"Postconditions failed for step '{step.id}' "
                        f"({step.intent}): expected screen state not reached "
                        f"(semantic drift) — failed: {detail} — run aborted"
                    )

            result.ok = error is None
            result.error = error

            if (
                result.ok
                and resolution is not None
                and matched_region is not None
                and resolution.rung != "template"
                and step.anchor is not None
            ):
                # Heal from the PRE-action frame: that is the frame the
                # anchor was resolved against (the action may have navigated
                # to a different screen, where a crop at the old location
                # would be garbage).
                result.heal = self._heal_step(
                    step, resolution, matched_region, before_png, workflow,
                    run_dir, new_crops,
                )
        except Exception as exc:  # defensive: report, don't crash the run
            result.ok = False
            result.error = f"Step '{step.id}' raised {type(exc).__name__}: {exc}"

        result.after_png = self._save_step_png(run_dir, step.id, "after", last_frame)
        result.elapsed_ms = (time.monotonic() - t0) * 1000.0
        return result

    def _resolve_step(
        self,
        step: Step,
        screen_png: bytes,
        bundle_dir: Path,
    ) -> tuple[Optional[Resolution], Optional[Region], Optional[str]]:
        """Resolve the step's anchor, applying the irreversible risk gate.

        Returns:
            (resolution, matched_region, error). ``error`` is set when the
            step needs an anchor it doesn't have, the ladder fails, or the
            risk gate blocks acting.
        """
        needs_anchor = step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
        if step.anchor is None:
            if needs_anchor:
                return None, None, (
                    f"Step '{step.id}' ({step.intent}) is a {step.action.value} "
                    "step but has no anchor"
                )
            return None, None, None

        template_png: Optional[bytes] = None
        template_path = Path(bundle_dir) / step.anchor.template
        if template_path.is_file():
            template_png = template_path.read_bytes()

        resolved = resolve(
            step.anchor,
            screen_png,
            self.vision,
            self.grounder,
            step.intent,
            template_png=template_png,
            viewport=self.backend.viewport,
        )
        if resolved is None:
            return None, None, (
                f"Could not resolve target for step '{step.id}' "
                f"({step.intent}): all resolution rungs failed"
            )
        resolution, matched_region = resolved

        if step.risk == "irreversible" and is_below_ocr(resolution.rung):
            return resolution, matched_region, (
                f"Step '{step.id}' ({step.intent}) is irreversible but only "
                f"resolved via the '{resolution.rung}' rung — needs human "
                "confirmation; refusing to act (v0 policy)"
            )
        return resolution, matched_region, None

    def _act(
        self,
        step: Step,
        resolution: Optional[Resolution],
        params: dict[str, str],
        *,
        workflow: Workflow,
        step_index: int,
        bundle_dir: Path,
        before_png: bytes,
    ) -> Optional[str]:
        """Perform the step's action through the backend.

        Returns:
            An error string (no action performed / partial) or None.
        """
        if step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            assert resolution is not None  # guaranteed by _resolve_step
            x, y = resolution.point
            self.backend.click(x, y, double=step.action is ActionKind.DOUBLE_CLICK)
            return None

        if step.action is ActionKind.TYPE:
            if step.param is not None:
                if step.param not in params:
                    return (
                        f"Step '{step.id}' ({step.intent}) requires parameter "
                        f"'{step.param}' but it was not provided"
                    )
                text = params[step.param]
            elif step.text is not None:
                text = step.text
            else:
                return (
                    f"Step '{step.id}' ({step.intent}) is a TYPE step with "
                    "neither text nor param"
                )
            if resolution is not None:
                # Anchored TYPE: click to focus the field first.
                x, y = resolution.point
                self.backend.click(x, y)
            self.backend.type_text(text)
            return None

        if step.action is ActionKind.KEY:
            if not step.key:
                return f"Step '{step.id}' ({step.intent}) is a KEY step with no key"
            self.backend.press(step.key)
            return None

        if step.action is ActionKind.WAIT:
            # WAIT means wait_settled only; the post-action settle handles it.
            return None

        if step.action is ActionKind.SCROLL:
            return self._act_scroll(
                step,
                workflow=workflow,
                step_index=step_index,
                bundle_dir=bundle_dir,
                before_png=before_png,
            )

        return f"Step '{step.id}' has unsupported action {step.action!r}"

    # -- closed-loop scroll ------------------------------------------------------

    def _act_scroll(
        self,
        step: Step,
        *,
        workflow: Workflow,
        step_index: int,
        bundle_dir: Path,
        before_png: bytes,
    ) -> Optional[str]:
        """Execute a SCROLL step as a closed loop on the next anchor.

        A recorded scroll's purpose is to bring the next target into view,
        so the step scrolls by its recorded delta until the NEXT anchored
        step's anchor resolves on a settled frame — not a fixed number of
        times. The step probes BEFORE scrolling (a preceding SCROLL step may
        already have brought the target into view, making this one a no-op)
        and stops as soon as a probe resolves.

        The loop is bounded: this step may scroll at most
        ``SCROLL_BUDGET_FACTOR`` times its own recorded distance. On budget
        exhaustion the step fails loudly — unless the immediately following
        step is another SCROLL step, which inherits the loop (so a recorded
        run of N scrolls shares a combined ~2.5x budget).

        Falls back to the fixed recorded delta (open-loop, one gesture) when
        no later step has an anchor or the recorded delta is zero. Probes
        never call the grounder: closed-loop scrolling must stay model-free.

        Returns:
            An error string on budget exhaustion (see above) or None.
        """
        dx = step.scroll_dx or 0
        dy = step.scroll_dy or 0
        next_step = self._next_anchored_step(workflow, step_index)
        if next_step is None or (dx == 0 and dy == 0):
            self.backend.scroll(dx, dy)
            return None

        if self._probe_anchor(next_step, before_png, bundle_dir):
            return None  # target already in view; nothing to scroll

        increment = math.hypot(dx, dy)
        budget = SCROLL_BUDGET_FACTOR * increment
        scrolled = 0.0
        while scrolled + increment <= budget:
            self.backend.scroll(dx, dy)
            scrolled += increment
            frame = self.vision.wait_settled(self.backend)
            if self._probe_anchor(next_step, frame, bundle_dir):
                return None

        following = (
            workflow.steps[step_index + 1]
            if step_index + 1 < len(workflow.steps)
            else None
        )
        if following is not None and following.action is ActionKind.SCROLL:
            # The next SCROLL step continues the loop with its own budget.
            return None
        return (
            f"Step '{step.id}' ({step.intent}): closed-loop scroll exhausted "
            f"its budget ({scrolled:.0f}px of {budget:.0f}px allowed, "
            f"{SCROLL_BUDGET_FACTOR}x the recorded distance) without the "
            f"anchor of step '{next_step.id}' ({next_step.intent}) resolving "
            "— target never came into view; run aborted"
        )

    @staticmethod
    def _next_anchored_step(workflow: Workflow, step_index: int) -> Optional[Step]:
        """The first step after ``step_index`` that carries an anchor."""
        for candidate in workflow.steps[step_index + 1:]:
            if candidate.anchor is not None:
                return candidate
        return None

    def _probe_anchor(
        self, step: Step, frame_png: bytes, bundle_dir: Path
    ) -> bool:
        """Single ladder pass for ``step``'s anchor against ``frame_png``.

        Used by the closed-loop scroll to test whether the scroll target is
        in view. No timeout retries and no grounder (a probe per scroll
        gesture must stay fast and model-free).
        """
        assert step.anchor is not None  # guaranteed by _next_anchored_step
        template_png: Optional[bytes] = None
        template_path = Path(bundle_dir) / step.anchor.template
        if template_path.is_file():
            template_png = template_path.read_bytes()
        return resolve(
            step.anchor,
            frame_png,
            self.vision,
            None,  # never ground during a scroll probe
            step.intent,
            template_png=template_png,
            viewport=self.backend.viewport,
        ) is not None

    # -- postconditions --------------------------------------------------------

    def _check_postconditions(
        self, step: Step, frame_png: bytes, bundle_dir: Path
    ) -> tuple[bool, bytes, list[str]]:
        """Poll postconditions until each passes or times out.

        Each postcondition is polled (fresh screenshots) up to its own
        ``timeout_s``. If any fails, the screen is re-settled once and all
        postconditions are re-checked a single time.

        Returns:
            (ok, last_frame, failed) — the frame the final verdict was based
            on, plus human-readable descriptions of the postconditions that
            failed the final check (empty when ok).
        """
        ok, frame_png = self._poll_postconditions(step, frame_png, bundle_dir)
        if ok:
            return True, frame_png, []
        # One re-settle retry.
        frame_png = self.vision.wait_settled(self.backend)
        failed = [
            self._describe_postcondition(pc)
            for pc in step.expect
            if not self._postcondition_passes(pc, frame_png, bundle_dir)
        ]
        return not failed, frame_png, failed

    @staticmethod
    def _describe_postcondition(pc: Any) -> str:
        """Human-readable one-liner for a postcondition (for error messages)."""
        kind = pc.kind.value if hasattr(pc.kind, "value") else pc.kind
        if kind in ("text_present", "text_absent"):
            return f"{kind} {pc.text!r}"
        return f"{kind} region={tuple(pc.region) if pc.region else None}"

    def _poll_postconditions(
        self, step: Step, frame_png: bytes, bundle_dir: Path
    ) -> tuple[bool, bytes]:
        """First pass: poll each postcondition until pass or timeout."""
        for pc in step.expect:
            deadline = time.monotonic() + pc.timeout_s
            while True:
                if self._postcondition_passes(pc, frame_png, bundle_dir):
                    break
                if time.monotonic() >= deadline:
                    return False, frame_png
                time.sleep(self.poll_interval_s)
                frame_png = self.backend.screenshot()
        return True, frame_png

    def _postcondition_passes(
        self, pc: Any, frame_png: bytes, bundle_dir: Path
    ) -> bool:
        """Evaluate a single postcondition against a frame."""
        kind = pc.kind.value if hasattr(pc.kind, "value") else pc.kind
        if kind == "text_present":
            return (
                pc.text is not None
                and self.vision.find_text(frame_png, pc.text) is not None
            )
        if kind == "text_absent":
            return (
                pc.text is None
                or self.vision.find_text(frame_png, pc.text) is None
            )
        if kind == "region_stable":
            if pc.region is None or pc.phash is None:
                return True
            region = tuple(pc.region)
            # Template check first: real apps re-layout by a few pixels
            # between runs (auto-scrolling panes, variable banner heights),
            # which the exact-position phash cannot tolerate — accept the
            # expected content anywhere near the recorded region.
            template_png = self._postcondition_template(pc, bundle_dir)
            if template_png is not None:
                search = pad_region(
                    region, PC_TEMPLATE_SEARCH_PAD, self.backend.viewport
                )
                match = self.vision.find_template(
                    frame_png,
                    template_png,
                    search_region=search,
                    threshold=PC_TEMPLATE_THRESHOLD,
                )
                if match is not None:
                    return True
            live = self.vision.phash_png(frame_png, region=region)
            distance = self.vision.phash_distance(live, pc.phash)
            return distance <= pc.phash_tolerance
        return False

    @staticmethod
    def _postcondition_template(pc: Any, bundle_dir: Path) -> Optional[bytes]:
        """Bytes of a REGION_STABLE postcondition's template crop, if any."""
        rel = getattr(pc, "template", None)
        if not rel:
            return None
        path = Path(bundle_dir) / rel
        return path.read_bytes() if path.is_file() else None

    # -- healing ---------------------------------------------------------------

    def _heal_step(
        self,
        step: Step,
        resolution: Resolution,
        matched_region: Region,
        frame_png: bytes,
        workflow: Workflow,
        run_dir: Path,
        new_crops: dict[str, bytes],
    ):
        """Build, apply, and persist a heal for a non-template success."""
        event, crop_png = heal_mod.build_heal_event(
            step, resolution, matched_region, frame_png, self.vision
        )
        heal_mod.apply_heal(workflow, event)
        heal_mod.persist_heal(event, crop_png, frame_png, run_dir)
        new_crops[step.id] = crop_png
        return event

    # -- io ----------------------------------------------------------------------

    @staticmethod
    def _save_step_png(
        run_dir: Path, step_id: str, suffix: str, png: bytes
    ) -> str:
        """Save a per-step screenshot; return its run-dir-relative path."""
        rel = f"steps/{step_id}_{suffix}.png"
        (Path(run_dir) / rel).write_bytes(png)
        return rel

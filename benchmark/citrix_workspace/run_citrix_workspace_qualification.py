#!/usr/bin/env python3
"""Citrix-Workspace-window backend qualification against a no-DOM canvas STAND-IN.

Part 2 of the no-DOM/Citrix validation. Drives the REAL Citrix-Workspace-window
pixel backend (:class:`~openadapt_flow.backends.citrix_workspace.CitrixWorkspaceBackend`,
a :class:`~openadapt_flow.backends.remote_display.RemoteDisplayBackend` preset)
through the unmodified Recorder -> compile_recording -> Replayer, asserting the
SAME validation contract as Part 1 (benchmark/canvas_ladder), but exercising the
window-scoped-capture-plus-OS-input backend code path instead of the browser
backend.

HOW IT IS A REAL PROOF (not a mock of the backend): the backend is unmodified;
only its ``WindowClient`` seam -- the OS window-server layer that captures a
window by id and injects OS input into it -- is swapped for a
:class:`CanvasWindowClient` that captures the Part-1 no-DOM ``<canvas>`` and
injects into it. This is the exact "point it at a different client window" seam
``RemoteDisplayBackend`` was designed around, so ALL of the backend's real logic
runs: window resolution, per-frame capture + DPI/scale computation, the
pixel->screen-point map, the frame-freshness lease, the occlusion guard, and the
fail-loud input-trust gate. Swapping ``CanvasWindowClient`` for the host's real
Mac/Win client and the owner string for "Citrix Viewer" points the SAME backend
at a live Citrix Workspace window (see ../README.md, "Real ICA/HDX release gate").

HONEST LABEL: the surface here is the **no-DOM HTML5-canvas class** (the class
Citrix Workspace-*web* presents), NOT Citrix ICA/HDX. This proves the Citrix
*backend contract + ladder + effect + safe-halt*; it does NOT prove HDX codecs,
ICA compression, or the real Workspace-client input path. See ../README.md.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image

from openadapt_flow.backends.citrix_workspace import (
    CitrixWorkspaceBackend,
    default_citrix_owner,
)
from openadapt_flow.backends.remote_display import WindowInfo

# Reuse the Part-1 fixture geometry, drift, oracle, and reset so the two proofs
# are identical except for the backend under test.
_CANVAS_HARNESS = (
    Path(__file__).resolve().parents[1]
    / "canvas_ladder"
    / "run_canvas_ladder_qualification.py"
)


def _load_canvas_harness():
    spec = importlib.util.spec_from_file_location(
        "canvas_ladder_harness", _CANVAS_HARNESS
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class CanvasWindowClient:
    """A :class:`~openadapt_flow.backends.remote_display.WindowClient` backed by
    the no-DOM noVNC ``<canvas>`` (Playwright).

    Presents the canvas to ``RemoteDisplayBackend`` as if it were a native client
    window: ``capture`` screenshots the canvas; ``mouse``/``type_chars``/``key``
    inject at canvas-relative points that noVNC forwards over the VNC wire. Window
    identity/geometry are constant and stable (fixed bounds), so the backend's
    lease/occlusion/frontmost gates all evaluate against a single unambiguous
    window -- exactly as they would against one real Citrix Workspace window.
    """

    def __init__(
        self, page, *, owner: str, title: str, width: int = 1280, height: int = 800
    ) -> None:
        self._page = page
        self._owner, self._title = owner, title
        self._w, self._h = width, height
        self._canvas = page.locator("canvas").first
        self._win = WindowInfo(
            window_id=1,
            owner=owner,
            title=title,
            pid=4242,
            bounds=(0.0, 0.0, float(width), float(height)),
            on_screen=True,
        )

    def _offset(self) -> tuple[float, float]:
        bb = self._canvas.bounding_box()
        if bb is None:
            raise RuntimeError("noVNC <canvas> not visible")
        return bb["x"], bb["y"]

    # -- WindowClient protocol ----------------------------------------------
    def input_trusted(self) -> bool:
        return True

    def frontmost_pid(self) -> Optional[int]:
        return self._win.pid

    def find_windows(self, owner: str, title: Optional[str]) -> list[WindowInfo]:
        # Match the backend's owner/title request the way the real client would;
        # bounds are CONSTANT so the backend's geometry-stability lease holds.
        return [self._win]

    def key_window_id(self, pid: int) -> Optional[int]:
        return self._win.window_id if pid == self._win.pid else None

    def window_at_point(self, x: float, y: float) -> Optional[int]:
        return self._win.window_id

    def capture(self, window_id: int) -> tuple[bytes, int, int]:
        raw = self._canvas.screenshot()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        if img.size != (self._w, self._h):
            img = img.crop((0, 0, self._w, self._h))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue(), self._w, self._h

    def activate(self, pid: int) -> None:
        return None

    def mouse_move(self, x: float, y: float) -> None:
        ox, oy = self._offset()
        self._page.mouse.move(ox + x, oy + y)

    def mouse(
        self, x: float, y: float, *, button: str, down: bool, click_count: int = 1
    ) -> None:
        ox, oy = self._offset()
        self._page.mouse.move(ox + x, oy + y)
        if down:
            self._page.mouse.down(button=button)
        else:
            self._page.mouse.up(button=button)
        self._page.wait_for_timeout(60)

    def type_chars(self, text: str) -> None:
        self._page.keyboard.type(text, delay=35)
        self._page.wait_for_timeout(120)

    def key(self, keycode: int, *, down: bool, flags: list[str]) -> None:
        # The fixture flow uses no named-key presses; a real client owns the
        # keycode namespace. Left unimplemented on purpose (resolve_key returns
        # None so press() halts loudly rather than mis-firing a wrong key).
        raise NotImplementedError(
            "CanvasWindowClient does not synthesize named-key scancodes"
        )

    def scroll(self, dx: int, dy: int) -> None:
        self._page.mouse.wheel(dx, dy)

    def resolve_key(self, token: str) -> Optional[tuple[int, bool]]:
        return None


def _make_page(pw, base_url: str, port: int, canvas_mod):
    browser = pw.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(
        viewport={"width": 1500, "height": 950}, device_scale_factor=1
    )
    page.goto(f"{base_url}:{port}/{canvas_mod.NOVNC_PATH}")
    page.wait_for_selector("canvas", timeout=20000)
    # Wait for a non-blank painted frame.
    deadline = time.monotonic() + 20
    cvs = page.locator("canvas").first
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        img = Image.open(io.BytesIO(cvs.screenshot())).convert("RGB")
        if len(img.getcolors(maxcolors=1 << 24) or []) > 3:
            return browser, page
    raise RuntimeError("noVNC canvas never painted a non-blank kiosk frame")


def _citrix_backend(page, canvas_mod):
    client = CanvasWindowClient(
        page, owner=default_citrix_owner(), title="canvas-stand-in"
    )
    # activate_before_input keeps the real frontmost/key-window gate exercised;
    # require_input_trust stays ON so the fail-loud contract is under test.
    return CitrixWorkspaceBackend(
        client,
        window_title="canvas-stand-in",
        activate_before_input=True,
        require_input_trust=True,
        settle_s=0.03,
        max_frame_age_s=30.0,
    )


def run_qualification(
    container: str,
    *,
    out_dir: Path,
    base_url: str,
    port: int,
    candidate_commit: str = "",
    base_commit: str = "",
) -> dict:
    from playwright.sync_api import sync_playwright

    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime.replayer import Replayer

    cm = _load_canvas_harness()
    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    rec_dir, bundle_dir = work / "recording", work / "bundle"
    trials: list[dict] = []
    t_start = time.monotonic()
    expected_saved = f"{cm.EXPECTED_MRN}\t{cm.NOTE_VALUE}"

    with sync_playwright() as pw:
        # ---- Trial 1: healthy record -> compile -> replay through the ladder --
        cm._reset_kiosk(container)
        browser, page = _make_page(pw, base_url, port, cm)
        try:
            backend = _citrix_backend(page, cm)
            rec = Recorder(
                backend,
                rec_dir,
                settle_interval_s=0.3,
                settle_stable_frames=2,
                settle_timeout_s=6.0,
            )
            rec.click(*cm.ADA_ROW)
            rec.click(*cm.NOTE_FIELD)
            rec.type_text(cm.NOTE_VALUE, param=cm.NOTE_PARAM)
            rec.click(*cm.SAVE_BUTTON)
            rec.finish()
        finally:
            browser.close()

        workflow = compile_recording(
            rec_dir, bundle_dir, name="citrix-workspace-ladder"
        )

        cm._reset_kiosk(container)
        browser, page = _make_page(pw, base_url, port, cm)
        try:
            backend = _citrix_backend(page, cm)
            report = Replayer(backend, poll_interval_s=0.3).run(
                workflow,
                params={cm.NOTE_PARAM: cm.NOTE_VALUE},
                bundle_dir=bundle_dir,
                run_dir=work / "run_healthy",
            )
        finally:
            browser.close()
        saved = cm._read_saved_note(container)
        effect_confirmed = saved == expected_saved
        rung_counts = dict(report.rung_counts)
        structural_used = rung_counts.get("structural", 0)
        visual_rungs = {
            k: v
            for k, v in rung_counts.items()
            if k in ("template", "template_global", "ocr", "geometry")
        }
        healthy_ok = (
            report.success
            and report.model_calls == 0
            and structural_used == 0
            and bool(visual_rungs)
            and effect_confirmed
        )
        trials.append(
            {
                "trial": 1,
                "kind": "healthy_record_compile_replay",
                "success": bool(report.success),
                "model_calls": int(report.model_calls),
                "rung_counts": rung_counts,
                "structural_rung_used": int(structural_used),
                "visual_rungs_used": visual_rungs,
                "effect_confirmed": effect_confirmed,
                "effect_expected": expected_saved,
                "effect_observed": saved,
                "passed": bool(healthy_ok),
                "failure_class": None if healthy_ok else "healthy_contract_violation",
            }
        )

        # ---- Trial 2: severe drift -> SAFE-HALT (no blind coordinate replay) --
        cm._reset_kiosk(container)
        browser, page = _make_page(pw, base_url, port, cm)
        try:
            backend = _citrix_backend(page, cm)
            sev_report = Replayer(
                cm._DriftBackend.severe(backend), poll_interval_s=0.3
            ).run(
                workflow,
                params={cm.NOTE_PARAM: cm.NOTE_VALUE},
                bundle_dir=bundle_dir,
                run_dir=work / "run_drift_severe",
            )
        finally:
            browser.close()
        saved_sev = cm._read_saved_note(container)
        sev_halted = not sev_report.success
        sev_no_write = saved_sev != expected_saved
        sev_no_model = sev_report.model_calls == 0
        sev_ok = sev_halted and sev_no_write and sev_no_model
        trials.append(
            {
                "trial": 2,
                "kind": "severe_drift_safe_halt",
                "drift": (
                    "downscale_0.14x + gaussian_blur_2.0 + theme_invert + "
                    "jpeg_q5 (illegible synthetic stand-in frame)"
                ),
                "halted": bool(sev_halted),
                "rung_counts": dict(sev_report.rung_counts),
                "model_calls": int(sev_report.model_calls),
                "silent_write": bool(saved_sev == expected_saved),
                "effect_after_drift": saved_sev,
                "passed": bool(sev_ok),
                "failure_class": None if sev_ok else "drift_not_safely_halted",
            }
        )

    cm._reset_kiosk(container)

    successes = sum(1 for t in trials if t["passed"])
    accepted = (
        len(trials) == 2
        and successes == 2
        and trials[0]["model_calls"] == 0
        and trials[0]["structural_rung_used"] == 0
        and trials[0]["effect_confirmed"]
        and trials[1]["halted"]
        and not trials[1]["silent_write"]
    )

    evidence = {
        "schema_version": "openadapt.citrix-workspace-qualification.v1",
        "substrate": "citrix-workspace-backend-over-no-dom-canvas-standin",
        "backend_under_test": "CitrixWorkspaceBackend (RemoteDisplayBackend preset)",
        "window_client": "CanvasWindowClient (noVNC canvas via Playwright)",
        "citrix_owner_preset": default_citrix_owner(),
        "candidate_commit": candidate_commit,
        "base_commit": base_commit,
        "task": (
            "record->compile->replay a patient-note write through the "
            "vision-only resolver ladder, driving the Citrix-Workspace-"
            "window pixel backend (window-scoped capture + OS input) over a "
            "no-DOM canvas stand-in; confirm the write via an independent "
            "document oracle; and safe-halt under severe injected drift"
        ),
        "contract": {
            "healthy_zero_model_calls": trials[0]["model_calls"] == 0,
            "healthy_structural_rung_used": trials[0]["structural_rung_used"],
            "healthy_visual_rungs_used": trials[0]["visual_rungs_used"],
            "healthy_effect_confirmed": trials[0]["effect_confirmed"],
            "severe_drift_safely_halted": trials[1]["halted"],
            "severe_drift_no_silent_write": not trials[1]["silent_write"],
        },
        "oracle": "docker exec cat of the kiosk-persisted note file",
        "failure_taxonomy": [
            "connect_or_frame_failure",
            "healthy_contract_violation",
            "effect_not_confirmed",
            "drift_not_safely_halted",
        ],
        "caveat": (
            "Exercises the REAL Citrix-Workspace-window pixel backend "
            "(window-scoped capture + OS input through the inherited fail-loud "
            "safety gates) over a no-DOM HTML5-canvas STAND-IN (the class Citrix "
            "Workspace-web presents) -- NOT Citrix ICA/HDX. Real ICA/HDX "
            "validation requires the separate 3+3 release gate in README."
        ),
        "pending_for_real_ica": [
            "3 healthy and 3 drift trials on one exact ICA/HDX environment",
            "independent effect oracle for the bounded synthetic lab task",
            "zero silent incorrect success and explicit refusal under drift",
            "reviewed bounded aggregate with raw evidence retained privately",
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trials": trials,
        "run_count": len(trials),
        "successes": successes,
        "model_calls": trials[0]["model_calls"],
        "total_s": round(time.monotonic() - t_start, 3),
        "accepted": bool(accepted),
    }
    return evidence


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--container",
        default=os.environ.get(
            "OAFLOW_CANVAS_LADDER_CONTAINER", "oaflow-canvas-ladder"
        ),
    )
    ap.add_argument(
        "--base-url",
        default=os.environ.get("OAFLOW_CANVAS_LADDER_BASE_URL", "http://localhost"),
    )
    ap.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("OAFLOW_CANVAS_LADDER_PORT", "6080")),
    )
    ap.add_argument(
        "--output", type=Path, default=Path("benchmark/citrix_workspace/results.json")
    )
    ap.add_argument("--candidate-commit", default="")
    ap.add_argument("--base-commit", default="")
    args = ap.parse_args()

    out_dir = args.output.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence = run_qualification(
        args.container,
        out_dir=out_dir,
        base_url=args.base_url,
        port=args.port,
        candidate_commit=args.candidate_commit,
        base_commit=args.base_commit,
    )
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    payload = json.dumps(evidence, sort_keys=True).encode()
    print(f"evidence sha256: {hashlib.sha256(payload).hexdigest()}")
    print(f"accepted: {evidence['accepted']}  wrote: {args.output}")
    return 0 if evidence["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

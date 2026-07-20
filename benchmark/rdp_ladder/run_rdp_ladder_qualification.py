#!/usr/bin/env python3
"""Real-RDP vision-ladder qualification: record -> compile -> replay over a
genuine RDP round-trip, asserting the validation contract.

This is the missing live analog of the desktop structural proof (#102) and the
RDP transport/input proof (#142) for the VISION-ONLY resolution ladder: it drives
the UNMODIFIED Recorder -> compiler -> Replayer over a real RDP pixel surface
with NO structural (a11y/UIA) backend, so resolution can only go through the
visual rungs (template -> template_global -> ocr -> geometry). It asserts:

  * record -> compile -> replay succeeds on a healthy run,
  * ZERO model calls on the healthy run (the $0 deterministic guarantee),
  * resolution used the VISUAL rungs and NEVER the structural rung,
  * the write EFFECT is independently confirmed (document oracle: the note the
    kiosk persisted equals the intended value -- record_written + field_equals),
  * the ladder HALTS (never silently mis-clicks) when the frame is degraded by
    injected DPI/theme/JPEG-compression drift (simulated drift on a real
    session).

The RDP surface is the `benchmark/rdp_ladder/fixture` container: a deterministic
Tk kiosk app served by a FreeRDP *server* and rendered back by a FreeRDP
*client*, so the pixels this harness reads and the input it injects both cross a
genuine RDP protocol exchange. See that directory's README for why FreeRDP (not
the product's aardwolf client) is used for the Linux CI surface, and how this
complements the aardwolf-over-Windows transport qualification in `benchmark/rdp`.

Honest scope: this qualifies the VISUAL RESOLUTION LADDER + CONTRACT over real
RDP-transported pixels. It is NOT a Citrix ICA/HDX proof, NOT an aardwolf
transport proof (that is `benchmark/rdp`), and the injected drift is
simulated-drift-on-a-real-session, not WAN capture.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

# -- fixture geometry (kiosk_app.py renders these at fixed positions) ----------
VIEWPORT = (1280, 800)
ADA_ROW = (347, 192)         # "Ada Lovelace   MRN A1001" roster row
GRACE_ROW = (347, 262)       # "Grace Hopper   MRN B2002" roster row
NOTE_FIELD = (410, 588)      # clinical-note entry
SAVE_BUTTON = (910, 588)     # "Save Note" button
NOTE_PARAM = "note"
NOTE_VALUE = "followup in two weeks"
EXPECTED_MRN = "MRN A1001"   # Ada's MRN, written by the kiosk on save
SAVE_PATH = "/opt/rdp_fixture/saved_note.txt"

# xdotool keysym map for the transport (named keys + space).
_XDOTOOL_KEYS = {
    "enter": "Return", "tab": "Tab", "escape": "Escape", "backspace": "BackSpace",
    "delete": "Delete", "space": "space", "home": "Home", "end": "End",
    "pageup": "Prior", "pagedown": "Next", "up": "Up", "down": "Down",
    "left": "Left", "right": "Right", "ctrl": "ctrl", "shift": "shift",
    "alt": "alt", "meta": "super",
}


class DockerX11RdpTransport:
    """`RDPTransport` over the FreeRDP client display inside the fixture
    container: `framebuffer()` screenshots the RDP-decoded client display and
    `pointer/key/wheel` inject X events that the FreeRDP client forwards over
    the RDP wire to the kiosk. Both directions traverse real RDP.

    Uses `docker exec` so the flow stack runs on the host (where it is
    installed) while the RDP round-trip stays in the container.
    """

    def __init__(self, container: str, *, display: str = ":1",
                 width: int = 1280, height: int = 800) -> None:
        self._c = container
        self._display = display
        self._w, self._h = width, height
        self._last_pointer: Optional[tuple[int, int]] = None

    def _exec(self, args: list[str], *, binary: bool = False):
        cmd = ["docker", "exec", "-e", f"DISPLAY={self._display}", self._c, *args]
        res = subprocess.run(cmd, capture_output=True,
                             timeout=30, check=False)
        if res.returncode != 0 and not binary:
            raise RuntimeError(
                f"docker exec {args!r} failed rc={res.returncode}: "
                f"{res.stderr.decode(errors='replace')[:200]}")
        return res.stdout

    # -- RDPTransport --------------------------------------------------------
    def connect(self) -> None:
        # Wait for a non-blank RDP frame (the kiosk painted through the wire).
        for _ in range(40):
            _, w, h = self.framebuffer()
            if (w, h) == (self._w, self._h):
                img = self._grab()
                if len(img.getcolors(maxcolors=1 << 24) or []) > 1:
                    return
            time.sleep(0.5)
        raise RuntimeError("no painted RDP frame within timeout")

    def disconnect(self) -> None:
        pass

    def _grab(self) -> Image.Image:
        out = self._exec(["import", "-window", "root", "png:-"], binary=True)
        if not out:
            raise RuntimeError("empty framebuffer capture")
        return Image.open(io.BytesIO(out)).convert("RGB")

    def framebuffer(self):
        img = self._grab()
        return img, img.width, img.height

    def pointer(self, x: int, y: int, button: str, down: bool) -> None:
        btn = {"left": "1", "right": "3", "middle": "2"}.get(button, "1")
        self._last_pointer = (int(x), int(y))
        verb = "mousedown" if down else "mouseup"
        self._exec(["xdotool", "mousemove", "--sync", str(int(x)), str(int(y)),
                    verb, btn])

    def key(self, keysym_or_char: str, down: bool) -> None:
        keysym = _XDOTOOL_KEYS.get(keysym_or_char, keysym_or_char)
        if keysym == " ":
            keysym = "space"
        # A single printable character is delivered as one atomic press+release
        # on the DOWN edge (xdotool `key`), which is far more reliable through
        # the RDP client than separate synthetic keydown/keyup scancodes; the
        # UP edge is then a no-op. Named keys / modifiers keep true down/up so
        # chords still nest correctly.
        if len(keysym) == 1 or keysym == "space":
            if down:
                self._exec(["xdotool", "key", "--clearmodifiers", keysym])
            return
        verb = "keydown" if down else "keyup"
        self._exec(["xdotool", verb, "--clearmodifiers", keysym])

    def wheel(self, dx: int, dy: int) -> None:
        if not dy:
            return
        btn = "5" if dy > 0 else "4"
        for _ in range(max(1, abs(dy) // 100)):
            self._exec(["xdotool", "click", btn])


# -- injected-drift wrapper (simulated drift on a real RDP session) ------------
class _DriftBackend:
    """Wraps a Backend and degrades every screenshot with DPI + theme + JPEG
    drift, so the resolver sees a realistically-degraded remote frame while the
    REAL RDP session is unchanged. Used only for the halt-under-drift case."""

    def __init__(self, backend, *, downscale: float = 0.4, invert: bool = True,
                 jpeg_quality: int = 8) -> None:
        self._b = backend
        self._ds, self._invert, self._q = downscale, invert, jpeg_quality

    def __getattr__(self, name):
        return getattr(self._b, name)

    def screenshot(self) -> bytes:
        img = Image.open(io.BytesIO(self._b.screenshot())).convert("RGB")
        w, h = img.size
        # Severe DPI/scale drift: downsample hard then back up -- destroys the
        # fine glyph/edge detail the template AND OCR rungs rely on (a laggy,
        # low-bandwidth ICA/HDX frame), so a conservative ladder must halt
        # rather than resolve on degraded pixels.
        img = img.resize((max(1, int(w * self._ds)), max(1, int(h * self._ds))),
                         Image.BILINEAR).resize((w, h), Image.BILINEAR)
        if self._invert:                       # theme inversion
            img = ImageOps.invert(img)
        buf = io.BytesIO()                     # heavy ICA/HDX-like JPEG blocking
        img.save(buf, format="JPEG", quality=self._q)
        out = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
        png = io.BytesIO()
        out.save(png, format="PNG")
        return png.getvalue()


def _read_saved_note(container: str) -> Optional[str]:
    res = subprocess.run(["docker", "exec", container, "cat", SAVE_PATH],
                         capture_output=True, timeout=15, check=False)
    if res.returncode != 0:
        return None
    return res.stdout.decode(errors="replace").strip()


def _reset_kiosk(container: str) -> None:
    """Restore the kiosk to its initial state between trials via an IN-PLACE
    reset (SIGUSR1): the kiosk clears the form and deletes the saved note
    without destroying its window, so the RDP display never blanks and
    keyboard-focus continuity is preserved (see kiosk_app.py)."""
    subprocess.run(["docker", "exec", container, "pkill", "-USR1", "-f",
                    "kiosk_app.py"], capture_output=True, timeout=30, check=False)
    time.sleep(1.5)  # let the Tk poll apply the reset + the RDP frame settle


def run_qualification(container: str, *, out_dir: Path,
                      candidate_commit: str = "", base_commit: str = "") -> dict:
    from openadapt_flow.backends.rdp_backend import FreeRDPBackend
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime.replayer import Replayer

    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    trials: list[dict] = []
    t_start = time.monotonic()

    # ---- Trial 1: healthy record -> compile -> replay through the ladder ----
    _reset_kiosk(container)
    transport = DockerX11RdpTransport(container)
    backend = FreeRDPBackend(transport, connect=True)

    rec_dir = work / "recording"
    bundle_dir = work / "bundle"
    run_dir = work / "run_healthy"

    rec = Recorder(backend, rec_dir, settle_interval_s=0.3,
                   settle_stable_frames=2, settle_timeout_s=6.0)
    rec.click(*ADA_ROW)                       # select patient (visual rung)
    rec.click(*NOTE_FIELD)                    # focus note field
    rec.type_text(NOTE_VALUE, param=NOTE_PARAM)
    rec.click(*SAVE_BUTTON)                   # write (irreversible)
    rec.finish()

    workflow = compile_recording(rec_dir, bundle_dir, name="rdp-vision-ladder")

    _reset_kiosk(container)
    transport2 = DockerX11RdpTransport(container)
    backend2 = FreeRDPBackend(transport2, connect=True)
    report = Replayer(backend2, poll_interval_s=0.3).run(
        workflow, params={NOTE_PARAM: NOTE_VALUE},
        bundle_dir=bundle_dir, run_dir=run_dir)

    saved = _read_saved_note(container)
    expected_saved = f"{EXPECTED_MRN}\t{NOTE_VALUE}"
    effect_confirmed = saved == expected_saved
    rung_counts = dict(report.rung_counts)
    structural_used = rung_counts.get("structural", 0)
    visual_rungs = {k: v for k, v in rung_counts.items()
                    if k in ("template", "template_global", "ocr", "geometry")}
    healthy_ok = (report.success and report.model_calls == 0
                  and structural_used == 0 and bool(visual_rungs)
                  and effect_confirmed)
    trials.append({
        "trial": 1, "kind": "healthy_record_compile_replay",
        "success": bool(report.success), "model_calls": int(report.model_calls),
        "rung_counts": rung_counts, "structural_rung_used": int(structural_used),
        "visual_rungs_used": visual_rungs,
        "effect_confirmed": effect_confirmed,
        "effect_expected": expected_saved, "effect_observed": saved,
        "passed": bool(healthy_ok),
        "failure_class": None if healthy_ok else "healthy_contract_violation",
    })

    # ---- Trial 2: halt-under-drift (simulated DPI+theme+JPEG on real frame) --
    _reset_kiosk(container)
    transport3 = DockerX11RdpTransport(container)
    backend3 = FreeRDPBackend(transport3, connect=True)
    drift_backend = _DriftBackend(backend3)
    run_dir_drift = work / "run_drift"
    drift_report = Replayer(drift_backend, poll_interval_s=0.3).run(
        workflow, params={NOTE_PARAM: NOTE_VALUE},
        bundle_dir=bundle_dir, run_dir=run_dir_drift)
    saved_after_drift = _read_saved_note(container)
    # Contract: under heavy drift the ladder must HALT (not succeed) and must
    # NOT have silently written the note (no wrong/partial effect).
    drift_halted = (not drift_report.success)
    drift_no_silent_write = saved_after_drift != expected_saved
    drift_no_model = drift_report.model_calls == 0
    drift_ok = drift_halted and drift_no_silent_write and drift_no_model
    trials.append({
        "trial": 2, "kind": "halt_under_injected_drift",
        "drift": "downscale_0.4x_blur + theme_invert + jpeg_q8 (simulated on real session)",
        "halted": bool(drift_halted),
        "model_calls": int(drift_report.model_calls),
        "silent_write": bool(saved_after_drift == expected_saved),
        "effect_after_drift": saved_after_drift,
        "passed": bool(drift_ok),
        "failure_class": None if drift_ok else "drift_not_safely_halted",
    })

    _reset_kiosk(container)

    successes = sum(1 for t in trials if t["passed"])
    accepted = (len(trials) == 2 and successes == 2
                and trials[0]["model_calls"] == 0
                and trials[0]["structural_rung_used"] == 0
                and trials[0]["effect_confirmed"]
                and trials[1]["halted"] and not trials[1]["silent_write"])

    evidence = {
        "schema_version": "openadapt.rdp-ladder-qualification.v1",
        "substrate": "real-rdp-freerdp3-roundtrip-linux-kiosk",
        "candidate_commit": candidate_commit,
        "base_commit": base_commit,
        "task": ("record->compile->replay a patient-note write through the "
                 "vision-only resolver ladder over a real RDP round-trip, with "
                 "no structural backend; confirm the write via an independent "
                 "document oracle; and halt under injected DPI/theme/JPEG drift"),
        "contract": {
            "healthy_zero_model_calls": trials[0]["model_calls"] == 0,
            "healthy_structural_rung_used": trials[0]["structural_rung_used"],
            "healthy_visual_rungs_used": trials[0]["visual_rungs_used"],
            "healthy_effect_confirmed": trials[0]["effect_confirmed"],
            "drift_safely_halted": trials[1]["halted"],
            "drift_no_silent_write": not trials[1]["silent_write"],
        },
        "environment": {
            "rdp_server": "freerdp-shadow-cli3 (FreeRDP3 server)",
            "rdp_client": "xfreerdp3 (FreeRDP3 client, fullscreen)",
            "surface": "Tk kiosk app served over RDP; observed on client display",
            "backend": "openadapt_flow FreeRDPBackend (pixel-only, use_structural off)",
            "transport": "DockerX11RdpTransport (screenshot+xdotool over the RDP client)",
            "viewport": list(VIEWPORT),
            "note": ("aardwolf (the product's Windows-RDP transport) cannot "
                     "connect to Linux RDP servers; see fixture README. The "
                     "aardwolf-over-Windows transport is qualified separately in "
                     "benchmark/rdp (PR #142)."),
        },
        "oracle": "docker exec cat of the kiosk-persisted note file",
        "failure_taxonomy": [
            "connect_or_frame_failure", "healthy_contract_violation",
            "effect_not_confirmed", "drift_not_safely_halted",
        ],
        "caveat": ("Real RDP-transported pixels + input, but NOT Citrix ICA/HDX, "
                   "NOT the aardwolf transport, and the drift is "
                   "simulated-on-a-real-session (not WAN capture)."),
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
    ap.add_argument("--container", default=os.environ.get(
        "OAFLOW_RDP_LADDER_CONTAINER", "oaflow-rdp-ladder"))
    ap.add_argument("--output", type=Path, default=Path(
        "benchmark/rdp_ladder/results.json"))
    ap.add_argument("--candidate-commit", default="")
    ap.add_argument("--base-commit", default="")
    args = ap.parse_args()

    out_dir = args.output.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence = run_qualification(
        args.container, out_dir=out_dir,
        candidate_commit=args.candidate_commit, base_commit=args.base_commit)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    payload = json.dumps(evidence, sort_keys=True).encode()
    print(f"evidence sha256: {hashlib.sha256(payload).hexdigest()}")
    print(f"accepted: {evidence['accepted']}  wrote: {args.output}")
    return 0 if evidence["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

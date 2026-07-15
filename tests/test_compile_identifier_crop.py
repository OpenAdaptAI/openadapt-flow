"""Compile-time identifier-crop capture for the PIXEL-ONLY (macOS/Citrix) path.

The pixel-compare identity tier (``runtime.identity.verify_pixel_identity``) is
production-reachable only when the compiler persists a recorded identifier crop
on a pixel-only substrate. These tests pin that capture and the guarantee it is
built to preserve:

* a pixel-only recording (an armed OCR identity band, NO structured identity —
  UIA/DOM does not cross the ICA/RDP boundary) compiles WITH an
  ``anchor.identifier_crop`` + ``identifier_region`` and the crop file on disk;
* a recording that DID capture structured identity (browser DOM / Windows UIA)
  gets NO crop — the structured tier owns identity and no identity pixels are
  written at rest;
* the captured crop drives a WRONG-identifier HALT: ``verify_pixel_identity``
  MISMATCHES a one-glyph-different live crop, ABSTAINS (not VERIFY) on the same
  value, and the VERIFY path stays hard-gated off — so arming the tier can only
  add a safe halt, never a pixel false-accept (zero-false-accept preserved).
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from openadapt_flow.compiler import compile_recording
from openadapt_flow.ir import ActionKind
from openadapt_flow.runtime import identity as identity_mod

VIEWPORT = (1280, 800)
# A patient identity row: name + DOB (far from the recording date => kept as
# identity, not chronology) + MRN, drawn to the LEFT of the click target so it
# lands in the click row's identity band but OUTSIDE the target's own template
# crop.
ROW_Y = 430
IDENTITY_TEXT = "MARIA GARCIA  DOB 1974-08-21  MRN AC50061"
# One-glyph-different MRN (AC50061 -> AC58061): a different patient whose only
# difference is a single digit — the wrong-patient the pixel tier must catch.
IDENTITY_TEXT_WRONG = "MARIA GARCIA  DOB 1974-08-21  MRN AC58061"


def _blank() -> np.ndarray:
    return np.full((VIEWPORT[1], VIEWPORT[0], 3), 245, dtype=np.uint8)


def _draw_text(img: np.ndarray, x: int, y: int, text: str) -> None:
    cv2.putText(
        img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2, cv2.LINE_AA
    )


def _draw_button(img: np.ndarray, x: int, y: int, w: int, h: int, label: str) -> None:
    cv2.rectangle(img, (x, y), (x + w, y + h), (205, 205, 205), -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (70, 70, 70), 2)
    cv2.putText(
        img,
        label,
        (x + 12, y + h // 2 + 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )


def _before_frame(identity_text: str = IDENTITY_TEXT) -> np.ndarray:
    img = _blank()
    _draw_text(img, 80, ROW_Y + 8, identity_text)
    # click target ("Open") on the same row, to the right of the identity text
    _draw_button(img, 980, ROW_Y - 22, 120, 44, "Open")
    return img


def _write_frame(recording: Path, i: int, suffix: str, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    (recording / "frames" / f"{i:04d}_{suffix}.png").write_bytes(buf.tobytes())


def _build_recording(tmp_path: Path, *, with_structured: bool) -> tuple[Path, Path]:
    recording = tmp_path / "recording"
    bundle = tmp_path / "bundle"
    (recording / "frames").mkdir(parents=True)

    before = _before_frame()
    after = before.copy()
    _draw_text(after, 420, 620, "Chart Opened")  # a localized postcondition change

    click = {"i": 0, "kind": "click", "x": 1040, "y": ROW_Y, "t": 1.0}
    if with_structured:
        click["structured_identity"] = IDENTITY_TEXT
    _write_frame(recording, 0, "before", before)
    _write_frame(recording, 0, "after", after)
    (recording / "events.jsonl").write_text(json.dumps(click) + "\n")
    (recording / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-idcrop-001",
                "created_at": "2026-07-06T00:00:00+00:00",
                "viewport": list(VIEWPORT),
                "app_url": "http://localhost:0/",
                "params": {},
            }
        )
    )
    return recording, bundle


def _click_step(workflow):
    return next(s for s in workflow.steps if s.action is ActionKind.CLICK)


def test_pixel_only_recording_captures_identifier_crop(tmp_path: Path) -> None:
    recording, bundle = _build_recording(tmp_path, with_structured=False)
    workflow = compile_recording(recording, bundle, name="idcrop-pixel")

    anchor = _click_step(workflow).anchor
    assert anchor is not None
    # Identity is armed on the pixel substrate (OCR band read the row) ...
    assert anchor.context_text is not None or anchor.identity_template is not None
    # ... so the identifier crop was captured.
    assert anchor.identifier_crop is not None
    assert anchor.identifier_region is not None
    crop_path = bundle / anchor.identifier_crop
    assert crop_path.is_file()
    # A real crop (decodable, non-empty) inside the frame.
    rx, ry, rw, rh = anchor.identifier_region
    assert rw > 0 and rh > 0
    assert 0 <= rx and 0 <= ry
    assert rx + rw <= VIEWPORT[0] and ry + rh <= VIEWPORT[1]
    img = cv2.imdecode(
        np.frombuffer(crop_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR
    )
    assert img is not None and img.size > 0


def test_structured_recording_writes_no_identifier_crop(tmp_path: Path) -> None:
    recording, bundle = _build_recording(tmp_path, with_structured=True)
    workflow = compile_recording(recording, bundle, name="idcrop-structured")

    anchor = _click_step(workflow).anchor
    assert anchor is not None
    # Structured identity present => the structured tier owns identity; no
    # identity pixels are persisted at rest.
    assert anchor.identifier_crop is None
    assert anchor.identifier_region is None
    assert not (bundle / "identifiers").exists()


def test_captured_crop_halts_wrong_identifier_and_never_verifies(
    tmp_path: Path,
) -> None:
    """The zero-false-accept guarantee, end to end on the compiled crop.

    A wrong-MRN live crop (re-cut at the SAME recorded region, exactly as the
    replayer does) MISMATCHES; the same value ABSTAINS rather than VERIFIES; and
    the pixel VERIFY path is hard-gated off. So capturing the crop can only add
    a safe HALT on a different identifier — it can never turn a wrong patient
    into a verified one.
    """
    recording, bundle = _build_recording(tmp_path, with_structured=False)
    workflow = compile_recording(recording, bundle, name="idcrop-guarantee")
    anchor = _click_step(workflow).anchor
    assert anchor is not None and anchor.identifier_crop is not None
    region = anchor.identifier_region
    assert region is not None

    recorded_png = (bundle / anchor.identifier_crop).read_bytes()

    # Live frame for a DIFFERENT patient (one MRN glyph changed), re-cut at the
    # SAME region — the wrong-patient case the tier must halt.
    wrong_frame = _before_frame(IDENTITY_TEXT_WRONG)
    ok, wrong_buf = cv2.imencode(".png", wrong_frame)
    assert ok
    wrong_live_png = identity_mod.crop_region(wrong_buf.tobytes(), region)

    verdict = identity_mod.verify_pixel_identity(recorded_png, wrong_live_png)
    assert verdict is not None and verdict.status == "mismatch"

    # Same value re-cut from the identical recorded frame: no localized spike,
    # so the tier ABSTAINS (None) and the ladder falls through — it does NOT
    # VERIFY on the pixel tier (VERIFY is gated off).
    same_frame = _before_frame(IDENTITY_TEXT)
    ok, same_buf = cv2.imencode(".png", same_frame)
    assert ok
    same_live_png = identity_mod.crop_region(same_buf.tobytes(), region)
    same_verdict = identity_mod.verify_pixel_identity(recorded_png, same_live_png)
    assert same_verdict is None or same_verdict.status != "verified"

    # The gate that makes the above unconditional: the pixel tier can never
    # VERIFY, so it can never false-accept regardless of the crop captured.
    assert identity_mod.PIXEL_VERIFY_ENABLED is False

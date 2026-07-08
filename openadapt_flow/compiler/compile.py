"""Compile a recorded demonstration into a workflow bundle.

Input is a recording directory (``meta.json`` + ``events.jsonl`` +
``frames/{i:04d}_before.png`` / ``_after.png``); output is a bundle directory
(``workflow.json`` + ``templates/*.png`` + a generated, human-reviewable
``workflow.py`` rendering).
"""

from __future__ import annotations

import difflib
import json
import math
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Landmark,
    Point,
    Postcondition,
    PostconditionKind,
    Region,
    Step,
    Workflow,
)
from openadapt_flow.vision.hashing import phash_png
from openadapt_flow.vision.ocr import OcrLine, normalize_text, ocr

# Template crop size around a click target (clamped to the frame).
TEMPLATE_W = 160
TEMPLATE_H = 64

# Crops with almost no pixel variance (e.g. the middle of an empty textarea)
# cannot be template-matched; the crop is grown through this ladder until its
# grayscale standard deviation clears MIN_TEMPLATE_STD, so every template
# contains at least some structure (a border, a label, ...).
CROP_GROWTH_LADDER: tuple[tuple[int, int], ...] = (
    (TEMPLATE_W, TEMPLATE_H),
    (240, 96),
    (320, 128),
    (480, 192),
    (640, 256),
)
MIN_TEMPLATE_STD = 5.0

# Minimum OCR confidence for a line to be used as ocr_text / landmark text.
MIN_OCR_CONFIDENCE = 0.5

# Before/after diff parameters (see DESIGN.md "Compiler").
DIFF_THRESHOLD = 25
# Tolerance for REGION_STABLE postconditions. The structural (edge-based)
# phash distance is ~0 for identical screens, <= 12 for cosmetic drift
# (theme palettes, relocated buttons elsewhere in the region) and >= 30 for
# semantic drift (e.g. a blocking modal), measured on MockMed; 16 splits
# those populations with margin.
REGION_STABLE_TOLERANCE = 16

# The changed region is padded by this many pixels (clamped to the frame)
# before hashing: tiny text-only regions (e.g. a freshly typed value inside
# an input) are dominated by glyph antialiasing, which shifts with palette
# drift; padding pulls stable structure (field borders, surrounding
# whitespace) into the hashed region.
REGION_STABLE_PAD = 24

# Minimum characters for a TEXT_PRESENT candidate (filters OCR noise).
MIN_TEXT_PRESENT_LEN = 3

# A TEXT_PRESENT candidate is dropped when at least this fraction of an
# excluded (parameterized) value's characters fuzzily appears inside it.
# Matching is whitespace-insensitive because OCR frequently drops or moves
# spaces (e.g. "Encounter saved — my note" -> "Encountersaved—mynote").
EXCLUDE_CONTAINMENT_RATIO = 0.8

# A TEXT_PRESENT candidate counts as already visible in the before frame
# when its squashed text matches a before line at or above this ratio. OCR
# reads the same static text slightly differently across frames (whitespace
# jitter, the odd glyph), so exact comparison would misclassify permanently
# visible chrome (e.g. an app header) as "new" — a postcondition that can
# never discriminate anything.
SEEN_BEFORE_RATIO = 0.9

# A TEXT_PRESENT candidate is dropped when it whole-line matches a click
# target's label (any anchor's ocr_text) at or above this ratio. Target
# labels are mutable evidence by design — rename drift changes them and the
# resolution ladder heals through it — so asserting one as a postcondition
# invariant would turn cosmetic label drift into a false semantic-drift
# abort.
LABEL_MATCH_RATIO = 0.85


def _load_events(recording_dir: Path) -> list[dict]:
    """Parse events.jsonl into a list of event dicts."""
    events = []
    with (recording_dir / "events.jsonl").open() as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _read_png(path: Path) -> Optional[bytes]:
    """Read PNG bytes, or None if the file does not exist."""
    return path.read_bytes() if path.exists() else None


def _clamped_crop_region(
    click: Point, frame_w: int, frame_h: int, size: tuple[int, int] = (TEMPLATE_W, TEMPLATE_H)
) -> Region:
    """Compute a ``size`` crop centered on ``click``, clamped so it stays
    inside the frame (shrunk only if the frame itself is smaller)."""
    w = min(size[0], frame_w)
    h = min(size[1], frame_h)
    x = min(max(0, click[0] - w // 2), frame_w - w)
    y = min(max(0, click[1] - h // 2), frame_h - h)
    return (x, y, w, h)


def _discriminative_crop_region(
    frame: np.ndarray, click: Point
) -> Region:
    """Crop region centered on ``click`` with enough structure to match.

    Starts at TEMPLATE_W x TEMPLATE_H and grows through CROP_GROWTH_LADDER
    until the crop's grayscale standard deviation reaches MIN_TEMPLATE_STD
    (a fully flat crop — e.g. the middle of an empty textarea — matches
    everywhere and nowhere). Falls back to the largest ladder size.
    """
    frame_h, frame_w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    region = _clamped_crop_region(click, frame_w, frame_h)
    for size in CROP_GROWTH_LADDER:
        region = _clamped_crop_region(click, frame_w, frame_h, size)
        x, y, w, h = region
        if float(gray[y : y + h, x : x + w].std()) >= MIN_TEMPLATE_STD:
            break
    return region


def _crop_png(png: bytes, region: Region) -> bytes:
    """Crop ``region`` out of a PNG and re-encode as PNG bytes."""
    arr = np.frombuffer(png, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode PNG bytes")
    x, y, w, h = region
    crop = img[y : y + h, x : x + w]
    ok, buf = cv2.imencode(".png", crop)
    if not ok:
        raise ValueError("could not encode crop as PNG")
    return buf.tobytes()


def _regions_intersect(a: Region, b: Region) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _best_crop_text(
    lines: list[OcrLine], click_y: Optional[int] = None
) -> Optional[str]:
    """Best label text for a target crop.

    OCR often splits one button label into several fragments (e.g. ``Save``
    + ``Encounter``); all confident fragments whose boxes vertically overlap
    the click row are joined left-to-right. Falls back to the single most
    confident line when no fragment overlaps ``click_y`` (or it is not
    given). Returns None if nothing confident was recognized.
    """
    confident = [
        l
        for l in lines
        if l.confidence >= MIN_OCR_CONFIDENCE and l.text.strip()
    ]
    if not confident:
        return None
    if click_y is not None:
        row = [
            l
            for l in confident
            if l.region[1] <= click_y <= l.region[1] + l.region[3]
        ]
        if row:
            row.sort(key=lambda l: l.region[0])
            return " ".join(l.text.strip() for l in row)
    best = max(confident, key=lambda l: l.confidence)
    return best.text.strip()


def _landmarks_for(
    frame_lines: list[OcrLine], crop_region: Region, click: Point
) -> list[Landmark]:
    """Derive up to 2 landmarks from OCR lines outside the template crop.

    ``relation`` encodes where the *landmark* sits relative to the target
    (dominant axis of the landmark-center -> click-point vector): a click
    to the landmark's right yields ``left_of``. ``distance_px`` is the
    Euclidean distance between the two, and ``dx_px``/``dy_px`` carry the
    exact landmark-center -> click-point offsets so the geometry rung can
    reconstruct the target precisely (see ``ir.Landmark``).
    """
    candidates = []
    for line in frame_lines:
        if line.confidence < MIN_OCR_CONFIDENCE or not line.text.strip():
            continue
        if _regions_intersect(line.region, crop_region):
            continue
        lx, ly, lw, lh = line.region
        cx, cy = lx + lw // 2, ly + lh // 2
        dx, dy = click[0] - cx, click[1] - cy
        dist = math.hypot(dx, dy)
        if abs(dx) >= abs(dy):
            # Target is to the landmark's right -> the landmark is left_of it.
            relation = "left_of" if dx >= 0 else "right_of"
        else:
            relation = "above" if dy >= 0 else "below"
        candidates.append((dist, relation, line.text.strip(), dx, dy))
    candidates.sort(key=lambda c: c[0])
    return [
        Landmark(
            relation=rel,
            ocr_text=text,
            distance_px=int(round(dist)),
            dx_px=int(dx),
            dy_px=int(dy),
        )
        for dist, rel, text, dx, dy in candidates[:2]
    ]


def _largest_changed_region(
    before_png: bytes, after_png: bytes
) -> Optional[Region]:
    """Bounding rect of the largest changed area between two frames.

    ``cv2.absdiff`` -> grayscale -> threshold ``DIFF_THRESHOLD`` -> dilate ->
    largest external contour's bounding rect. Returns None if nothing
    changed.
    """
    before = cv2.imdecode(
        np.frombuffer(before_png, dtype=np.uint8), cv2.IMREAD_COLOR
    )
    after = cv2.imdecode(
        np.frombuffer(after_png, dtype=np.uint8), cv2.IMREAD_COLOR
    )
    if before is None or after is None:
        return None
    if before.shape != after.shape:
        return (0, 0, after.shape[1], after.shape[0])
    diff = cv2.absdiff(before, after)
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
    kernel = np.ones((9, 9), dtype=np.uint8)
    mask = cv2.dilate(mask, kernel, iterations=2)
    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(largest)
    if w <= 0 or h <= 0:
        return None
    return (int(x), int(y), int(w), int(h))


def _squash(text: str) -> str:
    """Lowercase and remove ALL whitespace (OCR-tolerant comparison key)."""
    return "".join(normalize_text(text).split())


def _contains_excluded(text: str, excluded: tuple[str, ...]) -> bool:
    """True if ``text`` fuzzily contains (or is contained by) an excluded value.

    OCR mangles whitespace and the odd character, so exact substring checks
    miss e.g. the typed note inside a "Encounter saved — <note>" banner.
    Both sides are squashed (lowercased, whitespace removed) and an excluded
    value counts as present when at least ``EXCLUDE_CONTAINMENT_RATIO`` of
    its characters match ``text`` via difflib matching blocks.

    Args:
        text: A TEXT_PRESENT candidate (raw OCR line text).
        excluded: Excluded (parameterized) values, raw.

    Returns:
        True when the candidate must not be asserted.
    """
    hay = _squash(text)
    if not hay:
        return False
    for raw in excluded:
        ex = _squash(raw)
        if not ex:
            continue
        if ex in hay or hay in ex:
            return True
        matcher = difflib.SequenceMatcher(None, ex, hay)
        matched = sum(block.size for block in matcher.get_matching_blocks())
        if matched / len(ex) >= EXCLUDE_CONTAINMENT_RATIO:
            return True
    return False


def _matches_label(text: str, labels: tuple[str, ...]) -> bool:
    """True if ``text`` whole-line fuzzy-matches a click-target label."""
    sq = _squash(text)
    if not sq:
        return False
    for label in labels:
        lsq = _squash(label)
        if not lsq:
            continue
        if difflib.SequenceMatcher(None, sq, lsq).ratio() >= LABEL_MATCH_RATIO:
            return True
    return False


def _new_text_postcondition(
    before_lines: list[OcrLine],
    after_lines: list[OcrLine],
    *,
    exclude_texts: tuple[str, ...] = (),
    avoid_labels: tuple[str, ...] = (),
) -> Optional[Postcondition]:
    """TEXT_PRESENT for the longest OCR text in after but not before.

    Args:
        before_lines: OCR lines from the before frame.
        after_lines: OCR lines from the after frame.
        exclude_texts: Texts that must NOT be asserted (parameterized typed
            values, which vary per run — including any screen text that
            embeds them, e.g. a save-confirmation banner).
        avoid_labels: Click-target label texts (anchor ``ocr_text`` values)
            that must not be asserted: labels are mutable evidence the
            resolution ladder heals through (rename drift), not invariants.

    Returns:
        A TEXT_PRESENT postcondition, or None if there is no suitable new
        text.
    """
    before_norm = {normalize_text(l.text) for l in before_lines}
    before_squashed = [
        _squash(l.text) for l in before_lines if l.text.strip()
    ]

    def seen_before(text: str, norm: str) -> bool:
        if norm in before_norm:
            return True
        sq = _squash(text)
        for prior in before_squashed:
            if sq == prior:
                return True
            ratio = difflib.SequenceMatcher(None, sq, prior).ratio()
            if ratio >= SEEN_BEFORE_RATIO:
                return True
        return False

    candidates = []
    for line in after_lines:
        if line.confidence < MIN_OCR_CONFIDENCE:
            continue
        text = line.text.strip()
        norm = normalize_text(text)
        if len(norm) < MIN_TEXT_PRESENT_LEN or seen_before(text, norm):
            continue
        if _contains_excluded(text, exclude_texts):
            continue
        if _matches_label(text, avoid_labels):
            continue
        candidates.append(text)
    if not candidates:
        return None
    longest = max(candidates, key=len)
    return Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=longest)


def _postconditions(
    before_png: Optional[bytes],
    after_png: Optional[bytes],
    *,
    exclude_texts: tuple[str, ...] = (),
    avoid_labels: tuple[str, ...] = (),
    bundle: Optional[Path] = None,
    step_id: Optional[str] = None,
) -> list[Postcondition]:
    """Derive postconditions from a step's before/after frames.

    When ``bundle`` and ``step_id`` are given, the REGION_STABLE
    postcondition also carries a template crop of the expected region
    content (``templates/<step_id>_expect.png``): real apps re-layout by a
    few pixels between runs (auto-scrolling panes, banner heights), which a
    fixed-position phash cannot tolerate — the replayer first looks for the
    expected content NEAR the recorded region and only then falls back to
    the exact-position hash.
    """
    if before_png is None or after_png is None:
        return []
    expect: list[Postcondition] = []
    changed = _largest_changed_region(before_png, after_png)
    if changed is not None:
        after = cv2.imdecode(
            np.frombuffer(after_png, dtype=np.uint8), cv2.IMREAD_COLOR
        )
        frame_h, frame_w = after.shape[:2]
        x, y, w, h = changed
        x0 = max(0, x - REGION_STABLE_PAD)
        y0 = max(0, y - REGION_STABLE_PAD)
        x1 = min(frame_w, x + w + REGION_STABLE_PAD)
        y1 = min(frame_h, y + h + REGION_STABLE_PAD)
        padded: Region = (x0, y0, x1 - x0, y1 - y0)
        template_rel: Optional[str] = None
        if bundle is not None and step_id is not None:
            template_rel = f"templates/{step_id}_expect.png"
            (bundle / template_rel).write_bytes(_crop_png(after_png, padded))
        expect.append(
            Postcondition(
                kind=PostconditionKind.REGION_STABLE,
                region=padded,
                phash=phash_png(after_png, region=padded),
                phash_tolerance=REGION_STABLE_TOLERANCE,
                template=template_rel,
            )
        )
    text_pc = _new_text_postcondition(
        ocr(before_png),
        ocr(after_png),
        exclude_texts=exclude_texts,
        avoid_labels=avoid_labels,
    )
    if text_pc is not None:
        expect.append(text_pc)
    return expect


def _text_preview(text: str, limit: int = 24) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compile_recording(
    recording_dir: Path | str, out_bundle_dir: Path | str, *, name: str
) -> Workflow:
    """Compile a recording directory into a workflow bundle.

    For each click (or double_click) event: crop a template (160x64, clamped
    to the frame, centered on the click), OCR the crop for ``ocr_text``,
    derive up to two landmarks from nearby OCR lines outside the crop, and
    derive postconditions (REGION_STABLE on the largest changed region plus
    TEXT_PRESENT for the most distinctive new text). Type/key events carry
    their text/param/key through. Parameterized typed values and click
    target labels are never asserted in any step's postconditions (the
    former vary per run; the latter are mutable evidence the resolution
    ladder heals through under rename drift). The bundle gets
    ``workflow.json``, ``templates/*.png`` and a generated readable
    ``workflow.py``.

    Args:
        recording_dir: Recording directory (meta.json, events.jsonl, frames/).
        out_bundle_dir: Output bundle directory (created if missing).
        name: Workflow name.

    Returns:
        The compiled :class:`Workflow` (also saved to the bundle).

    Raises:
        ValueError: On an unknown event kind.
        FileNotFoundError: If a click event's before frame is missing.
    """
    from openadapt_flow.compiler.codegen import render_workflow_py

    recording = Path(recording_dir)
    bundle = Path(out_bundle_dir)
    (bundle / "templates").mkdir(parents=True, exist_ok=True)

    meta = json.loads((recording / "meta.json").read_text())
    events = _load_events(recording)
    params: dict[str, str] = dict(meta.get("params") or {})
    viewport = meta.get("viewport")

    # Parameterized values vary per run, so they must never be asserted in
    # ANY step's postconditions — not just the TYPE step's own: the typed
    # value can surface later (e.g. in a save-confirmation banner after the
    # subsequent click), and baking the demo-time value in would make the
    # bundle replayable only with that exact value.
    param_values: list[str] = [v for v in params.values() if v]
    for event in events:
        if event.get("kind") == "type" and event.get("param"):
            text = event.get("text")
            if text:
                param_values.append(text)
    exclude_texts: tuple[str, ...] = tuple(dict.fromkeys(param_values))

    # Pass 1 builds the steps (anchors, actions); postconditions are derived
    # in pass 2, once every click target's label is known — target labels
    # are mutable evidence (rename drift) and must not be asserted.
    pending: list[tuple[Step, Optional[bytes], Optional[bytes]]] = []
    for event in events:
        i = int(event["i"])
        kind = event["kind"]
        step_id = f"step_{i:03d}"
        before_png = _read_png(recording / "frames" / f"{i:04d}_before.png")
        after_png = _read_png(recording / "frames" / f"{i:04d}_after.png")

        if kind in ("click", "double_click"):
            if before_png is None:
                raise FileNotFoundError(
                    f"missing before frame for {kind} event {i} in {recording}"
                )
            click: Point = (int(event["x"]), int(event["y"]))
            frame = cv2.imdecode(
                np.frombuffer(before_png, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            crop_region = _discriminative_crop_region(frame, click)
            template_bytes = _crop_png(before_png, crop_region)
            template_rel = f"templates/{step_id}.png"
            (bundle / template_rel).write_bytes(template_bytes)

            ocr_text = _best_crop_text(
                ocr(before_png, region=crop_region), click_y=click[1]
            )
            frame_lines = ocr(before_png)
            landmarks = _landmarks_for(frame_lines, crop_region, click)
            anchor = Anchor(
                template=template_rel,
                region=crop_region,
                click_point=click,
                ocr_text=ocr_text,
                landmarks=landmarks,
            )
            verb = "double-click" if kind == "double_click" else "click"
            intent = (
                f"{verb} '{ocr_text}'"
                if ocr_text
                else f"{verb} at ({click[0]}, {click[1]})"
            )
            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=intent,
                        action=(
                            ActionKind.DOUBLE_CLICK
                            if kind == "double_click"
                            else ActionKind.CLICK
                        ),
                        anchor=anchor,
                    ),
                    before_png,
                    after_png,
                )
            )
        elif kind == "type":
            param = event.get("param")
            text = event.get("text")
            if param:
                intent = f"type <{param}>"
            else:
                intent = f"type '{_text_preview(text or '')}'"
            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=intent,
                        action=ActionKind.TYPE,
                        text=text,
                        param=param,
                    ),
                    before_png,
                    after_png,
                )
            )
        elif kind == "key":
            key = event["key"]
            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=f"press {key}",
                        action=ActionKind.KEY,
                        key=key,
                    ),
                    before_png,
                    after_png,
                )
            )
        elif kind == "scroll":
            dx, dy = int(event.get("dx", 0)), int(event.get("dy", 0))
            # SCROLL steps get NO postconditions (note the (None, None)
            # frames): scrolling shifts the whole viewport, so a frame diff
            # spans nearly the full screen and would assert mutable page
            # content as an invariant. The scroll's purpose — bringing the
            # next target into view — is verified by the next anchored
            # step's resolution ladder, which fails if the scroll did not
            # land.
            pending.append(
                (
                    Step(
                        id=step_id,
                        intent=f"scroll by ({dx}, {dy})",
                        action=ActionKind.SCROLL,
                        scroll_dx=dx,
                        scroll_dy=dy,
                    ),
                    None,
                    None,
                )
            )
        else:
            raise ValueError(f"unknown event kind {kind!r} (event {i})")

    # Pass 2: derive postconditions, never asserting parameterized values or
    # any click target's label.
    anchor_labels: tuple[str, ...] = tuple(
        step.anchor.ocr_text
        for step, _, _ in pending
        if step.anchor is not None and step.anchor.ocr_text
    )
    steps: list[Step] = []
    for step, step_before, step_after in pending:
        step.expect = _postconditions(
            step_before,
            step_after,
            exclude_texts=exclude_texts,
            avoid_labels=anchor_labels,
            bundle=bundle,
            step_id=step.id,
        )
        steps.append(step)

    workflow = Workflow(
        name=name,
        recording_id=meta.get("id"),
        viewport=tuple(viewport) if viewport else None,
        params=params,
        steps=steps,
    )
    workflow.save(bundle)
    (bundle / "workflow.py").write_text(render_workflow_py(workflow))
    return workflow

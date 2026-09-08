"""Repair input and identity checks using encrypted bundles and real vision.

These are boundary tests with constructed local pixels. They do not stand in
for retained runtime repair evidence or a release qualification campaign.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from openadapt_flow import vision
from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.repair.campaign import (
    _patch_for_anchor,
    run_fault_campaign,
)
from openadapt_flow.repair.cli import _campaign_inputs
from openadapt_flow.repair.registration import build_candidate
from openadapt_flow.runtime import resolver
from openadapt_flow.runtime.healing.perturbation import (
    DriftCase,
    DriftKind,
    anchor_band_verdict,
    band_sampler,
    perturb,
    replay_patch,
)
from openadapt_flow.runtime.identity_template import build_identity_template

VIEWPORT = (1000, 260)
REGION = (720, 90, 220, 70)
POINT = (830, 125)
IDENTITY = "Marta Rivera Neurology"


def pixels(identity: str = IDENTITY) -> bytes:
    image = Image.new("RGB", VIEWPORT, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(28)
    draw.text((35, 108), identity, font=font, fill="black")
    draw.rounded_rectangle((720, 90, 940, 160), radius=12, fill=(224, 235, 249))
    draw.text((782, 108), "Save", font=font, fill=(17, 17, 17))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def png_crop(frame: bytes) -> bytes:
    image = Image.open(io.BytesIO(frame))
    stream = io.BytesIO()
    x, y, width, height = REGION
    image.crop((x, y, x + width, y + height)).save(stream, format="PNG")
    return stream.getvalue()


def hashed_anchor() -> Anchor:
    return Anchor(
        template="templates/save.png",
        region=REGION,
        click_point=POINT,
        ocr_text="Save",
        context_text=None,
        identity_template=build_identity_template(IDENTITY),
        identifier_region=(30, 100, 460, 60),
    )


def encrypted_candidate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", "synthetic-test-key")
    frame = pixels()
    source_anchor = hashed_anchor()
    for name, label in (("prior", "Save note"), ("proposed", "Save")):
        bundle = tmp_path / name
        (bundle / "templates").mkdir(parents=True)
        (bundle / "templates/save.png").write_bytes(png_crop(frame))
        anchor = source_anchor.model_copy(update={"ocr_text": label})
        workflow = Workflow(
            name="encrypted-repair-input",
            viewport=VIEWPORT,
            steps=[
                Step(
                    id="save",
                    intent="Save the note",
                    action=ActionKind.CLICK,
                    anchor=anchor,
                )
            ],
        )
        workflow.save(bundle, encrypt=True)
    run = tmp_path / "run"
    (run / "heals/save").mkdir(parents=True)
    (run / "heals/save/screen.png").write_bytes(frame)
    return build_candidate(
        tmp_path / "prior",
        tmp_path / "proposed",
        source="heal",
        evidence_run_dir=run,
    )


def test_campaign_inputs_use_decrypted_crop_without_disk_derivative(
    tmp_path, monkeypatch
):
    candidate = encrypted_candidate(tmp_path, monkeypatch)
    before = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    inputs = _campaign_inputs(candidate)
    assert len(inputs) == 1
    _, anchor, frame, template = inputs[0]
    assert template == png_crop(frame)
    assert anchor.context_text is None
    assert anchor.identity_template is not None
    # The encrypted crop remains encrypted; reading campaign input adds no files.
    assert not (tmp_path / "proposed/templates/save.png").exists()
    assert (tmp_path / "proposed/templates/save.png.enc").exists()
    assert before == {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize(
    "observed,expected",
    [(IDENTITY, "verified"), ("David Chen Oncology", "mismatch"), ("", "unreadable")],
)
def test_hashed_band_uses_canonical_identity_verdict(observed, expected):
    anchor = hashed_anchor()
    assert anchor_band_verdict(anchor, observed) == expected


def test_plaintext_cannot_replace_hashed_identity():
    anchor = hashed_anchor().model_copy(update={"context_text": "David Chen Oncology"})
    assert anchor_band_verdict(anchor, "David Chen Oncology") != "verified"


def test_structured_only_identity_is_unavailable_to_pixel_campaign():
    anchor = hashed_anchor().model_copy(
        update={
            "identity_template": build_identity_template(
                None, structured_identity="record=200"
            ),
            "identifier_region": None,
        }
    )
    assert anchor_band_verdict(anchor, "record=200") == "unreadable"


def real_resolver(anchor: Anchor, template: bytes):
    def locate(frame: bytes):
        result = resolver.resolve(anchor, frame, vision, template_png=template)
        return result[0].point if result else None

    return locate


def test_real_ocr_checks_hashed_identity_in_translated_and_scaled_identifier_region():
    anchor = hashed_anchor()
    frame = pixels()
    sample = band_sampler(VIEWPORT, vision, anchor=anchor)
    for kind in (DriftKind.SHIFT, DriftKind.SCALE, DriftKind.RETHEME):
        case = perturb(frame, POINT, kind)
        observed = sample(case.frame_png, case.expected_point) or ""
        assert anchor_band_verdict(anchor, observed) == "verified", (kind, observed)
    assert (
        anchor_band_verdict(anchor, sample(pixels("David Chen Oncology"), POINT) or "")
        != "verified"
    )


def test_real_ocr_excludes_target_label_from_unscoped_identity_band():
    anchor = hashed_anchor().model_copy(update={"identifier_region": None})
    observed = band_sampler(VIEWPORT, vision, anchor=anchor)(pixels(), POINT)
    assert observed is not None and "Save" not in observed
    assert anchor_band_verdict(anchor, observed) == "verified"


def test_replay_requires_hash_material_and_rejects_wrong_live_identity():
    anchor = hashed_anchor()
    frame = pixels()
    patch = _patch_for_anchor("save", anchor)
    sample = band_sampler(VIEWPORT, vision, anchor=anchor)
    locate = real_resolver(anchor, png_crop(frame))
    baseline = DriftCase("baseline", DriftKind.SHIFT, frame, POINT)
    missing = replay_patch(patch, [baseline], resolve=locate, sample_band=sample)
    assert not missing.promotable
    assert not missing.results[0].identity_ok
    correct = replay_patch(
        patch, [baseline], resolve=locate, sample_band=sample, identity_anchor=anchor
    )
    assert correct.promotable
    wrong = DriftCase("wrong", DriftKind.SHIFT, pixels("David Chen Oncology"), POINT)
    report = replay_patch(
        patch, [wrong], resolve=locate, sample_band=sample, identity_anchor=anchor
    )
    assert not report.promotable
    assert report.results[0].located and not report.results[0].identity_ok


def test_real_fault_battery_preserves_all_cases_and_checks_hashed_band():
    anchor = hashed_anchor()
    frame = pixels()
    result = run_fault_campaign(
        "save",
        anchor,
        frame,
        resolve=real_resolver(anchor, png_crop(frame)),
        sample_band=band_sampler(VIEWPORT, vision, anchor=anchor),
    )
    cases = {case.kind: case for case in result.cases}
    assert set(cases) == {
        "ambiguity",
        "wrong_entity",
        "stale_target",
        "unexpected_dialog",
        "verifier_failure",
    }
    assert cases["wrong_entity"].passed
    assert cases["verifier_failure"].passed
    assert all("unarmed" not in case.detail for case in result.cases)

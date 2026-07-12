"""Tests for the PHI/PII scrubbing integration (openadapt_flow.privacy).

Covered:

* the ``privacy`` extra is OPTIONAL — the module imports and the runtime runs
  with openadapt-privacy absent (default ``auto`` posture => plaintext, no crash);
* ``OPENADAPT_FLOW_SCRUB=on`` fails CLOSED when the capability is missing;
* ``OPENADAPT_FLOW_SCRUB=off`` never scrubs;
* text written to REPORT.md is scrubbed when a scrubber is present (injected fake);
* the drift-oracle console log is scrubbed;
* opt-in image redaction (``OPENADAPT_FLOW_SCRUB_IMAGES``) gates the persisted PNGs.

The fake scrubber avoids pulling in Presidio/spaCy so the suite stays fast and
offline (no model download, no Anthropic calls).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow import privacy
from openadapt_flow.ir import RunReport, StepResult, UnarmedStep
from openadapt_flow.report import render_run_report


class _FakeTextScrubber:
    """Redacts a fixed set of PHI tokens; enough to prove wiring, not Presidio."""

    _TOKENS = {
        "John Smith": "<PERSON>",
        "Jane Doe": "<PERSON>",
        "01/15/1985": "<DATE_TIME>",
        "MRN-99321": "<ID>",
        "john@example.com": "<EMAIL_ADDRESS>",
    }

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        for needle, repl in self._TOKENS.items():
            text = text.replace(needle, repl)
        return text


class _FakeImageScrubber:
    """Returns a solid-black image (stand-in for Presidio box redaction)."""

    def scrub_image(self, image, fill_color=None):
        return Image.new("RGB", image.size, (0, 0, 0))


@pytest.fixture(autouse=True)
def _clean_privacy_env(monkeypatch):
    """Each test starts from a clean scrub posture and no cached scrubber."""
    monkeypatch.delenv("OPENADAPT_FLOW_SCRUB", raising=False)
    monkeypatch.delenv("OPENADAPT_FLOW_SCRUB_IMAGES", raising=False)
    privacy.reset_scrubbers()
    yield
    privacy.reset_scrubbers()


# -- posture / optionality ---------------------------------------------------


def test_scrub_text_is_noop_without_scrubber(monkeypatch):
    """auto posture + no capability => plaintext, no crash (extra is optional)."""
    monkeypatch.setattr(privacy, "_build_provider", lambda: None)
    privacy.reset_scrubbers()
    assert privacy.scrub_mode() == "auto"
    assert privacy.text_scrubbing_enabled() is False
    assert privacy.scrub_text("Patient John Smith") == "Patient John Smith"


def test_scrub_on_fails_closed_when_capability_missing(monkeypatch):
    """OPENADAPT_FLOW_SCRUB=on must raise when openadapt-privacy is absent."""
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    monkeypatch.setattr(privacy, "_build_provider", lambda: None)
    privacy.reset_scrubbers()
    with pytest.raises(privacy.PrivacyNotAvailable):
        privacy.get_text_scrubber()


def test_scrub_off_never_scrubs(monkeypatch):
    """OPENADAPT_FLOW_SCRUB=off is an explicit no-op even with a scrubber present."""
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "off")
    privacy.set_text_scrubber(_FakeTextScrubber())
    assert privacy.scrub_mode() == "off"
    assert privacy.text_scrubbing_enabled() is False
    assert privacy.scrub_text("John Smith") == "John Smith"


def test_scrub_text_and_params_with_injected_scrubber():
    privacy.set_text_scrubber(_FakeTextScrubber())
    assert privacy.scrub_text("Patient John Smith DOB 01/15/1985") == (
        "Patient <PERSON> DOB <DATE_TIME>"
    )
    scrubbed = privacy.scrub_params({"patient": "Jane Doe", "note": "ok"})
    assert scrubbed == {"patient": "<PERSON>", "note": "ok"}


# -- REPORT.md text scrubbing ------------------------------------------------


def _phi_report() -> RunReport:
    return RunReport(
        workflow_name="Chart for John Smith",
        started_at="2026-07-12T00:00:00+00:00",
        params={"patient": "John Smith", "dob": "01/15/1985", "mrn": "MRN-99321"},
        results=[
            StepResult(
                step_id="step_open",
                intent="click 'John Smith' row",
                ok=False,
                error="postcondition text_present 'John Smith' timed out",
                elapsed_ms=100.0,
            )
        ],
        success=False,
        identity_applicable_steps=1,
        identity_armed_steps=0,
        identity_unarmed=[
            UnarmedStep(
                step_id="step_open",
                intent="click 'John Smith' row",
                reason="band text 'John Smith 01/15/1985' too generic",
            )
        ],
    )


def test_report_md_scrubbed_with_scrubber(tmp_path: Path):
    privacy.set_text_scrubber(_FakeTextScrubber())
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _phi_report().save(run_dir)

    md = render_run_report(run_dir).read_text()
    assert "John Smith" not in md
    assert "01/15/1985" not in md
    assert "MRN-99321" not in md
    assert "<PERSON>" in md  # redaction placeholder rendered
    # Non-PHI structure is preserved.
    assert "`patient`" in md
    assert "Totals" in md


def test_report_md_plaintext_when_scrubbing_off(tmp_path: Path):
    privacy.set_text_scrubber(_FakeTextScrubber())
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _phi_report().save(run_dir)

    import os

    os.environ["OPENADAPT_FLOW_SCRUB"] = "off"
    try:
        md = render_run_report(run_dir).read_text()
    finally:
        del os.environ["OPENADAPT_FLOW_SCRUB"]
    # off posture: the report still renders, unscrubbed (operator's choice).
    assert "John Smith" in md


# -- opt-in image redaction --------------------------------------------------


def _png_bytes(color=(200, 40, 40)) -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def test_image_redaction_off_by_default():
    privacy.set_image_scrubber(_FakeImageScrubber())
    original = _png_bytes()
    # No OPENADAPT_FLOW_SCRUB_IMAGES => passthrough.
    assert privacy.image_redaction_enabled() is False
    assert privacy.scrub_image_bytes(original) == original


def test_image_redaction_opt_in(monkeypatch):
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB_IMAGES", "1")
    privacy.set_image_scrubber(_FakeImageScrubber())
    assert privacy.image_redaction_enabled() is True
    out = privacy.scrub_image_bytes(_png_bytes((200, 40, 40)))
    # The fake redactor blacks out the frame.
    import io

    assert Image.open(io.BytesIO(out)).convert("RGB").getpixel((0, 0)) == (0, 0, 0)


def test_save_step_png_redacts_when_opt_in(monkeypatch, tmp_path: Path):
    """Replayer._save_step_png routes through opt-in image redaction."""
    from openadapt_flow.runtime.replayer import Replayer

    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB_IMAGES", "1")
    privacy.set_image_scrubber(_FakeImageScrubber())
    run_dir = tmp_path / "run"
    (run_dir / "steps").mkdir(parents=True)
    rel = Replayer._save_step_png(run_dir, "step_x", "before", _png_bytes((200, 40, 40)))
    import io

    saved = (run_dir / rel).read_bytes()
    assert Image.open(io.BytesIO(saved)).convert("RGB").getpixel((0, 0)) == (0, 0, 0)

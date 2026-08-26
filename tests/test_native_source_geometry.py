"""Sealed Capture v2 to Flow native source-geometry contract."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from openadapt_capture.db import create_db, crud
from openadapt_capture.db.models import ActionEvent, Recording, Screenshot, WindowEvent
from openadapt_capture.events import window_geometry_epoch_sha256
from openadapt_capture.terminal import seal_capture
from PIL import Image, ImageDraw

from openadapt_flow.adapters.capture import convert_capture
from openadapt_flow.compiler.compile import compile_recording

T0 = 100_000.0
FRAME_SIZE = (320, 200)
FRAME_ORDINAL = 1
ACTION_ORDINAL = 3
AFTER_FRAME_ORDINAL = 4


def _png(color: tuple[int, int, int], label: str) -> bytes:
    image = Image.new("RGB", FRAME_SIZE, color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 70, 220, 130), outline=(0, 0, 0), width=3)
    draw.text((120, 92), label, fill=(0, 0, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _geometry_state() -> dict:
    payload = {
        "schema_version": "openadapt.capture.window-scoped/v2",
        "window_capture": True,
        "window_id": "42",
        "owner": "FixtureApp",
        "pid": 4242,
        "process_start_time": 99_000.0,
        "coordinate_source": "test-screen-points",
        "geometry_generation": 1,
        "display_topology_sha256": "a" * 64,
        "bounds": [10.0, 20.0, 320.0, 200.0],
        "scale": 1.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "viewport": list(FRAME_SIZE),
        "source_viewport": list(FRAME_SIZE),
        "content_rect": [0, 0, *FRAME_SIZE],
        "fit_scale": 1.0,
        "on_screen": True,
    }
    payload["geometry_epoch_sha256"] = window_geometry_epoch_sha256(payload)
    return payload


def _capture_config(state: dict) -> dict:
    return {
        "capture_window": {
            **state,
            "target": {"owner": "FixtureApp", "title": None},
            "title": "Fixture Window",
            "initial_bounds": state["bounds"],
            "coordinate_space": "window_pixels",
        }
    }


def _make_capture(tmp_path: Path, *, sealed: bool = True) -> Path:
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    before = _png((240, 240, 240), "Run")
    after = _png((210, 240, 210), "Done")
    state = _geometry_state()

    engine, session_factory = create_db(str(capture_dir / "recording.db"))
    session = session_factory()
    try:
        recording = Recording(
            timestamp=T0,
            monitor_width=FRAME_SIZE[0],
            monitor_height=FRAME_SIZE[1],
            platform="test",
            task_description="native geometry fixture",
            double_click_interval_seconds=0.5,
            double_click_distance_pixels=5.0,
            config=_capture_config(state),
        )
        session.add(recording)
        session.flush()
        for timestamp, ordinal, png in (
            (T0 + 1.0, FRAME_ORDINAL, before),
            (T0 + 1.3, AFTER_FRAME_ORDINAL, after),
        ):
            session.add(
                Screenshot(
                    recording_id=recording.id,
                    recording_timestamp=T0,
                    timestamp=timestamp,
                    source_ordinal=ordinal,
                    png_data=png,
                    png_sha256=hashlib.sha256(png).hexdigest(),
                )
            )
            session.add(
                WindowEvent(
                    recording_id=recording.id,
                    timestamp=timestamp,
                    source_ordinal=ordinal,
                    title="Fixture Window",
                    left=10,
                    top=20,
                    width=320,
                    height=200,
                    window_id="42",
                    state=state,
                )
            )
        for timestamp, ordinal, pressed in (
            (T0 + 1.1, 2, True),
            (T0 + 1.2, ACTION_ORDINAL, False),
        ):
            session.add(
                ActionEvent(
                    recording_id=recording.id,
                    name="click",
                    timestamp=timestamp,
                    source_ordinal=ordinal,
                    mouse_x=160.0,
                    mouse_y=100.0,
                    mouse_button_name="left",
                    mouse_pressed=pressed,
                    screenshot_timestamp=T0 + 1.0,
                    screenshot_source_ordinal=FRAME_ORDINAL,
                    window_event_timestamp=T0 + 1.0,
                    window_event_source_ordinal=FRAME_ORDINAL,
                    window_geometry_generation=1,
                )
            )
        session.commit()
        crud.post_process_events(session, recording)
    finally:
        session.close()
        engine.dispose()

    if sealed:
        seal_capture(
            capture_dir,
            session_id="fixture-session",
            process_started_at=T0 - 1,
            capture_started_at=T0,
            capture_ended_at=T0 + 2,
            event_counts={
                "action": 2,
                "screen": 2,
                "window": 2,
                "browser": 0,
                "video": 0,
            },
            last_source_ordinal=AFTER_FRAME_ORDINAL,
        )
    return capture_dir


def _stamp_surface(recording_dir: Path, surface: str) -> None:
    path = recording_dir / "meta.json"
    meta = json.loads(path.read_text())
    meta["surface"] = surface
    path.write_text(json.dumps(meta, indent=2))


def test_sealed_native_capture_compiles_exact_source_geometry(tmp_path: Path) -> None:
    capture_dir = _make_capture(tmp_path)
    recording_dir = tmp_path / "recording"
    convert_capture(capture_dir, recording_dir, source_surface="windows")

    [event] = [
        json.loads(line)
        for line in (recording_dir / "events.jsonl").read_text().splitlines()
    ]
    geometry = event["source_geometry"]
    before_png = (recording_dir / "frames" / "0000_before.png").read_bytes()
    with Image.open(recording_dir / "frames" / "0000_after.png") as after_image:
        assert after_image.getpixel((0, 0)) == (210, 240, 210)
    assert geometry["source_action_ordinal"] == ACTION_ORDINAL
    assert geometry["source_frame_ordinal"] == FRAME_ORDINAL
    assert geometry["frame_sha256"] == hashlib.sha256(before_png).hexdigest()
    assert geometry["display_topology_sha256"] == "a" * 64

    _stamp_surface(recording_dir, "windows")
    workflow = compile_recording(
        recording_dir,
        tmp_path / "bundle",
        name="native geometry",
    )
    assert workflow.steps[0].source_geometry is not None
    assert (
        workflow.steps[0].source_geometry.binding_sha256 == geometry["binding_sha256"]
    )


def test_v2_capture_refuses_before_unsealed_database_can_be_opened(
    tmp_path: Path,
) -> None:
    capture_dir = _make_capture(tmp_path, sealed=False)
    database = capture_dir / "recording.db"
    before = (
        database.stat().st_mtime_ns,
        hashlib.sha256(database.read_bytes()).hexdigest(),
    )

    with pytest.raises(ValueError, match="no immutable terminal"):
        convert_capture(capture_dir, tmp_path / "recording", source_surface="windows")

    after = (
        database.stat().st_mtime_ns,
        hashlib.sha256(database.read_bytes()).hexdigest(),
    )
    assert after == before


def test_compiler_refuses_native_geometry_on_remote_surface(tmp_path: Path) -> None:
    capture_dir = _make_capture(tmp_path)
    recording_dir = tmp_path / "recording"
    convert_capture(capture_dir, recording_dir, source_surface="windows")
    _stamp_surface(recording_dir, "rdp")

    with pytest.raises(ValueError, match="only for an in-session native surface"):
        compile_recording(recording_dir, tmp_path / "bundle", name="wrong surface")


def test_compiler_refuses_tampered_exact_before_frame(tmp_path: Path) -> None:
    capture_dir = _make_capture(tmp_path)
    recording_dir = tmp_path / "recording"
    convert_capture(capture_dir, recording_dir, source_surface="windows")
    _stamp_surface(recording_dir, "windows")
    (recording_dir / "frames" / "0000_before.png").write_bytes(
        _png((255, 200, 200), "Changed")
    )

    with pytest.raises(ValueError, match="different before frame"):
        compile_recording(recording_dir, tmp_path / "bundle", name="tampered")


def test_legacy_step_json_omits_absent_source_geometry() -> None:
    from openadapt_flow.ir import ActionKind, Step

    dumped = Step(
        id="step_000", intent="press Enter", action=ActionKind.KEY, key="Enter"
    )
    assert "source_geometry" not in dumped.model_dump(mode="json")

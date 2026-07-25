"""Compose audited runtime state over raw app-only video without frame staging.

The source video remains unchanged. This tool copies it beside a PHI-safe
timeline, then asks a separately provisioned FFmpeg executable to encode a
presentation derivative directly from the source stream. It never modifies the
target application or injects presentation elements into its DOM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ass_time(milliseconds: float) -> str:
    total_cs = max(0, int(round(milliseconds / 10)))
    hours, remainder = divmod(total_cs, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _ass_text(value: str) -> str:
    return value.replace("{", r"\{").replace("}", r"\}")


def _event_end(events: list[dict[str, Any]], index: int, duration_ms: float) -> float:
    if index + 1 < len(events):
        following = events[index + 1].get("at_ms")
        if isinstance(following, (int, float)):
            return float(following)
    return duration_ms


def _validated_timeline(
    payload: Any, *, video: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], float]:
    if not isinstance(payload, dict):
        raise ValueError("overlay timeline must be an object")
    if payload.get("schema_version") != "openadapt.control-overlay-timeline/v1":
        raise ValueError("unsupported control-overlay timeline schema")
    if payload.get("data_classification") not in {"synthetic", "sanitized_public"}:
        raise ValueError("overlay export requires synthetic or sanitized-public data")
    pack_id = payload.get("evidence_pack_id")
    safe = "abcdefghijklmnopqrstuvwxyz0123456789-."
    if (
        not isinstance(pack_id, str)
        or not pack_id
        or any(c not in safe for c in pack_id)
    ):
        raise ValueError("overlay timeline has an unsafe evidence_pack_id")
    if payload.get("media_sha256") != _sha256(video):
        raise ValueError("overlay timeline is not bound to the exact source video")
    duration_ms = payload.get("duration_ms")
    if (
        not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or duration_ms <= 0
    ):
        raise ValueError("overlay timeline has no positive integer duration_ms")
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("overlay timeline has no retained runtime frames")
    previous = -1
    for sequence, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(f"overlay event {sequence} is not an object")
        at_ms = event.get("at_ms")
        if not isinstance(at_ms, int) or isinstance(at_ms, bool):
            raise ValueError(f"overlay event {sequence} lacks integer at_ms")
        if (sequence == 0 and at_ms != 0) or at_ms <= previous or at_ms > duration_ms:
            raise ValueError("overlay event offsets are not canonical and monotonic")
        frame = event.get("frame")
        if not isinstance(frame, dict):
            raise ValueError(f"overlay event {sequence} lacks a frame")
        required = (
            "schema_version",
            "state_id",
            "phase",
            "workflow_label",
            "mode",
            "status",
        )
        if any(not isinstance(frame.get(key), str) for key in required):
            raise ValueError(f"overlay frame {sequence} lacks canonical labels")
        if frame.get("schema_version") != "openadapt.control-overlay-frame/v1":
            raise ValueError(f"overlay frame {sequence} has an unsupported schema")
        if (
            frame.get("event_sequence") != sequence
            or frame.get("presentation") is not True
        ):
            raise ValueError(f"overlay frame {sequence} has inconsistent metadata")
        step = frame.get("step")
        if not isinstance(step, dict) or set(step) != {"current", "total"}:
            raise ValueError(f"overlay frame {sequence} has invalid step metadata")
        previous = at_ms
    return payload, events, float(duration_ms)


def _ass_document(timeline: dict[str, Any]) -> str:
    events = timeline["events"]
    duration_ms = float(timeline["duration_ms"])
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1280",
        "PlayResY: 800",
        "WrapStyle: 2",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Status,Helvetica,20,&H00FFFFFF,&H00FFFFFF,&H00000000,"
        "&HC0181B27,-1,0,0,0,100,100,0,0,3,0,0,7,24,24,22,1",
        "Style: Verified,Helvetica,22,&H00FFFFFF,&H00FFFFFF,&H00000000,"
        "&HC05F8B2F,-1,0,0,0,100,100,0,0,3,0,0,7,24,24,22,1",
        "Style: Halted,Helvetica,22,&H00FFFFFF,&H00FFFFFF,&H00000000,"
        "&HC03B3BF6,-1,0,0,0,100,100,0,0,3,0,0,7,24,24,22,1",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    ]
    for index, event in enumerate(events):
        start = float(event["at_ms"])
        end = max(start + 10, _event_end(events, index, duration_ms))
        frame = event["frame"]
        step = frame["step"]
        step_text = ""
        if step["current"] is not None and step["total"] is not None:
            step_text = rf"\NStep {step['current']} of {step['total']}"
        text = (
            f"OpenAdapt · {frame['workflow_label']}" rf"\N{frame['status']}{step_text}"
        )
        phase = frame["phase"]
        if phase == "verified":
            style = "Verified"
        elif phase in {"halted", "failed"}:
            style = "Halted"
        else:
            style = "Status"
        lines.append(
            "Dialogue: 0,"
            f"{_ass_time(start)},{_ass_time(end)},{style},,0,0,0,,"
            f"{_ass_text(text)}"
        )
    return "\n".join(lines) + "\n"


def _run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "FFmpeg command failed")


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        (
            "/System/Library/Fonts/SFNSRounded.ttf"
            if bold
            else "/System/Library/Fonts/SFNS.ttf"
        ),
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _overlay_cards(
    timeline: dict[str, Any], directory: Path
) -> list[tuple[Path, float, float]]:
    events = timeline["events"]
    duration_ms = float(timeline["duration_ms"])
    cards = []
    for index, event in enumerate(events):
        start_ms = float(event["at_ms"])
        end_ms = max(start_ms + 10, _event_end(events, index, duration_ms))
        frame = event["frame"]
        step = frame["step"]
        if step["current"] is not None and step["total"] is not None:
            detail = f"{frame['status']} · {step['current']}/{step['total']}"
        else:
            detail = frame["status"]
        terminal = frame["phase"] in {
            "verified",
            "completed_unverified",
            "halted",
            "failed",
            "rolled_back",
        }
        fill = (
            (46, 139, 95, 242)
            if frame["phase"] == "verified"
            else (154, 52, 52, 242)
            if terminal
            else (24, 27, 39, 232)
        )
        outline = (
            (89, 211, 153, 255) if frame["phase"] == "verified" else (96, 165, 250, 255)
        )
        image = Image.new("RGBA", (420, 60), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, 419, 59), radius=10, fill=fill)
        draw.rounded_rectangle((0, 0, 419, 59), radius=10, outline=outline, width=2)
        draw.text(
            (14, 8),
            f"OpenAdapt · {frame['workflow_label']} · {frame['profile']}",
            font=_font(14, bold=True),
            fill=(238, 242, 250, 255),
        )
        draw.text(
            (14, 33),
            detail,
            font=_font(13),
            fill=(255, 255, 255, 255) if terminal else (165, 177, 198, 255),
        )
        path = directory / f"step-{index:03d}.png"
        image.save(path)
        cards.append((path, start_ms / 1000, end_ms / 1000))
    return cards


def compose(
    video: Path,
    timeline_path: Path,
    out_dir: Path,
    ffmpeg: str,
    *,
    application: str,
    application_version: str,
) -> None:
    if out_dir.exists():
        raise FileExistsError(f"output directory already exists: {out_dir}")
    out_dir.mkdir(parents=True)
    timeline, events, _duration_ms = _validated_timeline(
        json.loads(timeline_path.read_text()), video=video
    )
    raw = out_dir / f"raw-app{video.suffix.lower()}"
    timeline_copy = out_dir / "runtime-timeline.json"
    ass = out_dir / "runtime-overlay.ass"
    shutil.copy2(video, raw)
    shutil.copy2(timeline_path, timeline_copy)
    raw.chmod(0o644)
    timeline_copy.chmod(0o644)
    ass.write_text(_ass_document(timeline))

    mp4 = out_dir / "presentation.mp4"
    webm = out_dir / "presentation.webm"
    poster = out_dir / "poster.jpg"
    with tempfile.TemporaryDirectory(prefix="openadapt-overlay-") as temp:
        cards = _overlay_cards(timeline, Path(temp))
        command = [ffmpeg, "-y", "-i", str(video)]
        for path, _start, _end in cards:
            command.extend(["-i", str(path)])
        filters = ["[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=0x60a5fa@0.85:t=4[v0]"]
        for index, (_path, start, end) in enumerate(cards, start=1):
            filters.append(
                f"[v{index - 1}][{index}:v]overlay=16:14:"
                f"enable='between(t,{start:.3f},{end:.3f})'[v{index}]"
            )
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                f"[v{len(cards)}]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "21",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(mp4),
            ]
        )
        _run(command)
    _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(mp4),
            "-an",
            "-c:v",
            "libvpx-vp9",
            "-crf",
            "31",
            "-b:v",
            "0",
            str(webm),
        ]
    )
    first_step_ms = next(
        (
            event["at_ms"]
            for event in events
            if event["frame"]["step"]["current"] is not None
        ),
        0,
    )
    poster_seconds = max(0.0, float(first_step_ms) / 1000 + 0.2)
    _run(
        [
            ffmpeg,
            "-y",
            "-ss",
            f"{poster_seconds:.3f}",
            "-i",
            str(mp4),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(poster),
        ]
    )
    terminal_phases = {
        "verified",
        "completed_unverified",
        "halted",
        "failed",
        "rolled_back",
    }
    terminal_event = next(
        (
            event
            for event in reversed(events)
            if event["frame"]["phase"] in terminal_phases
        ),
        None,
    )
    terminal_poster: Path | None = None
    if terminal_event is not None:
        terminal_poster = out_dir / "terminal.jpg"
        terminal_seconds = min(
            max(0.0, float(timeline["duration_ms"]) / 1000 - 0.05),
            float(terminal_event["at_ms"]) / 1000 + 0.2,
        )
        _run(
            [
                ffmpeg,
                "-y",
                "-ss",
                f"{terminal_seconds:.3f}",
                "-i",
                str(mp4),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(terminal_poster),
            ]
        )
    version = subprocess.run(
        [ffmpeg, "-version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    files = [raw, timeline_copy, ass, mp4, webm, poster]
    if terminal_poster is not None:
        files.append(terminal_poster)
    manifest = {
        "schema_version": 2,
        "application": application,
        "application_version": application_version,
        "data_classification": timeline["data_classification"],
        "evidence_pack_id": timeline["evidence_pack_id"],
        "source_video_sha256": _sha256(video),
        "source_timeline_sha256": _sha256(timeline_path),
        "composition": (
            "single-pass video decode/encode with audited timeline-derived overlays"
        ),
        "target_application_modified": False,
        "ffmpeg": version,
        "files": {
            item.name: {"sha256": _sha256(item), "bytes": item.stat().st_size}
            for item in files
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--application", required=True)
    parser.add_argument("--application-version", required=True)
    args = parser.parse_args()
    compose(
        args.video.resolve(),
        args.timeline.resolve(),
        args.out.resolve(),
        args.ffmpeg,
        application=args.application,
        application_version=args.application_version,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

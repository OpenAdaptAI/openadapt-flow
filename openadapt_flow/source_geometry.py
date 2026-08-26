"""Closed per-action native source-geometry evidence."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NATIVE_SOURCE_GEOMETRY_SCHEMA_VERSION = "openadapt.flow.native-source-geometry/v1"
_BINDING_DOMAIN = b"openadapt.flow.native-source-geometry.v1\0"


def native_source_geometry_sha256(payload: dict) -> str:
    """Hash the exact, closed native source binding field list."""
    fields = (
        "schema_version",
        "source_surface",
        "source_capture_session_sha256",
        "source_capture_terminal_sha256",
        "source_artifact_manifest_sha256",
        "source_action_ordinal",
        "source_frame_ordinal",
        "frame_sha256",
        "window_id",
        "owner",
        "pid",
        "process_start_time",
        "coordinate_source",
        "geometry_generation",
        "geometry_epoch_sha256",
        "display_topology_sha256",
        "bounds",
        "scale_x",
        "scale_y",
        "viewport",
        "source_viewport",
        "content_rect",
        "fit_scale",
    )
    closed = {field: payload.get(field) for field in fields}
    raw = json.dumps(
        closed,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(_BINDING_DOMAIN + raw).hexdigest()


class NativeSourceGeometry(BaseModel):
    """One native action bound to one verified source frame and geometry epoch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.flow.native-source-geometry/v1"]
    source_surface: Literal["windows", "macos", "linux"]
    source_capture_session_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_capture_terminal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifact_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_action_ordinal: int = Field(ge=1)
    source_frame_ordinal: int = Field(ge=1)
    frame_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    window_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    pid: int = Field(gt=0)
    process_start_time: float = Field(gt=0)
    coordinate_source: str = Field(min_length=1)
    geometry_generation: int = Field(ge=1)
    geometry_epoch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    display_topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bounds: tuple[float, float, float, float]
    scale_x: float = Field(gt=0)
    scale_y: float = Field(gt=0)
    viewport: tuple[int, int]
    source_viewport: tuple[int, int]
    content_rect: tuple[int, int, int, int]
    fit_scale: float = Field(gt=0)
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _closed_binding(self) -> "NativeSourceGeometry":
        if self.source_action_ordinal <= self.source_frame_ordinal:
            raise ValueError("a native action must follow its bound source frame")
        numeric = (
            self.process_start_time,
            *self.bounds,
            self.scale_x,
            self.scale_y,
            self.fit_scale,
        )
        if not all(math.isfinite(float(value)) for value in numeric):
            raise ValueError("native source geometry must be finite")
        if self.bounds[2] <= 0 or self.bounds[3] <= 0:
            raise ValueError("native source bounds must be positive")
        if any(value <= 0 for value in (*self.viewport, *self.source_viewport)):
            raise ValueError("native source viewports must be positive")
        left, top, width, height = self.content_rect
        if (
            left < 0
            or top < 0
            or width <= 0
            or height <= 0
            or left + width > self.viewport[0]
            or top + height > self.viewport[1]
        ):
            raise ValueError("native source content rectangle is outside its viewport")
        payload = self.model_dump(mode="json", exclude={"binding_sha256"})
        if self.binding_sha256 != native_source_geometry_sha256(payload):
            raise ValueError("native source geometry binding digest is invalid")
        return self

"""Backend protocols: the runtime boundary for observing and acting on a GUI.

Every backend provides fresh frames and bounded input. Browser and native
backends may additionally expose structural candidates and native operations;
opaque RDP/Citrix surfaces retain the visual/OCR/relational ladder. The
governed runtime owns uniqueness, identity, authorization, effect verification,
and refusal regardless of which observation or actuation rung is available.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.ir import (
        ActionDeliveryReceipt,
        Point,
        StructuralHandle,
        StructuralLocator,
    )


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _png_viewport(png: bytes) -> tuple[int, int]:
    """Return the exact PNG dimensions without decoding mutable image state."""

    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE):
        raise ValueError("frame observation requires valid PNG bytes")
    width, height = struct.unpack(">II", png[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("frame observation viewport must be positive")
    return int(width), int(height)


def frame_observation_identity(value: object) -> str:
    """Return one canonical privacy-safe identity digest for frame metadata."""

    return _sha256_json(value)


def _identity_text(value: str, *, name: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    if not normalized:
        raise ValueError(f"{name} must be non-empty")
    return normalized


def _domain_separated_identity_digest(domain: bytes, payload: object) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain + canonical).hexdigest()


def window_identity_sha256(
    *,
    window_id: str,
    pid: int,
    process_start_time: Optional[str],
    owner: str,
) -> str:
    """Canonical exact-window digest shared by every frame adapter."""

    if not isinstance(window_id, str) or not window_id.strip():
        raise ValueError("window_id must be a non-empty string")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
        raise ValueError("pid must be a non-negative integer")
    if process_start_time is not None and (
        not isinstance(process_start_time, str) or not process_start_time.strip()
    ):
        raise ValueError("process_start_time must be a non-empty string or null")
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner must be a non-empty string")
    payload = {
        "schema": "openadapt.window-identity.v1",
        "window_id": window_id.strip(),
        "pid": pid,
        "process_start_time": (
            process_start_time.strip() if process_start_time is not None else None
        ),
        # Match the producer contract exactly. Do not normalize or collapse
        # owner text: those transforms can alias distinct qualified values.
        "owner": owner.casefold(),
    }
    return _domain_separated_identity_digest(
        b"openadapt.window-identity.v1\x00",
        payload,
    )


def session_identity_sha256(
    *,
    authority: str,
    session_id: str,
    session_start_time: Optional[str],
    principal_identity_sha256: Optional[str],
) -> str:
    """Canonical exact-session digest shared by every frame adapter."""

    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    if session_start_time is not None and (
        not isinstance(session_start_time, str) or not session_start_time.strip()
    ):
        raise ValueError("session_start_time must be a non-empty string or null")
    if principal_identity_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", principal_identity_sha256
    ):
        raise ValueError("principal_identity_sha256 must be lower-hex SHA-256")
    payload = {
        "schema": "openadapt.session-identity.v1",
        "authority": _identity_text(authority, name="authority"),
        "session_id": session_id.strip(),
        "session_start_time": (
            session_start_time.strip() if session_start_time is not None else None
        ),
        "principal_identity_sha256": principal_identity_sha256,
    }
    return _domain_separated_identity_digest(
        b"openadapt.session-identity.v1\x00",
        payload,
    )


@dataclass(frozen=True, slots=True)
class DisplayGeometry:
    """One stable display in global physical or logical screen coordinates."""

    display_id: str
    bounds: tuple[float, float, float, float]
    scale: tuple[float, float]

    def __post_init__(self) -> None:
        if not isinstance(self.display_id, str) or not self.display_id:
            raise ValueError("display_id must be non-empty")
        if len(self.bounds) != 4 or not all(
            math.isfinite(value) for value in self.bounds
        ):
            raise ValueError("display bounds must contain four finite values")
        if self.bounds[2] <= 0 or self.bounds[3] <= 0:
            raise ValueError("display width and height must be positive")
        if len(self.scale) != 2 or not all(
            math.isfinite(value) and value > 0 for value in self.scale
        ):
            raise ValueError("display scale must contain two positive values")


def display_topology_sha256(
    displays: tuple[DisplayGeometry, ...],
    *,
    coordinate_space: str,
) -> str:
    """Digest one canonical, order-independent complete display topology."""

    if not displays:
        raise ValueError("display topology requires at least one display")
    if not isinstance(coordinate_space, str) or not coordinate_space:
        raise ValueError("display coordinate_space must be non-empty")
    ids = [display.display_id for display in displays]
    if len(ids) != len(set(ids)):
        raise ValueError("display topology contains duplicate display identities")
    return frame_observation_identity(
        {
            "schema": "openadapt.display-topology.v1",
            "coordinate_space": coordinate_space,
            "displays": [
                {
                    "display_id": display.display_id,
                    "bounds": list(display.bounds),
                    "scale": list(display.scale),
                }
                for display in sorted(displays, key=lambda item: item.display_id)
            ],
        }
    )


def select_display_for_bounds(
    displays: tuple[DisplayGeometry, ...],
    bounds: tuple[float, float, float, float],
) -> DisplayGeometry:
    """Select the display with the largest exact overlap with target bounds.

    Equal overlap is ambiguous and refuses. A zero-overlap target also refuses.
    Negative global origins are valid.
    """

    if not displays:
        raise ValueError("display selection requires at least one display")
    x, y, width, height = bounds
    if width <= 0 or height <= 0 or not all(math.isfinite(v) for v in bounds):
        raise ValueError("target display-selection bounds must be finite and positive")

    def overlap(display: DisplayGeometry) -> float:
        dx, dy, dw, dh = display.bounds
        return max(0.0, min(x + width, dx + dw) - max(x, dx)) * max(
            0.0, min(y + height, dy + dh) - max(y, dy)
        )

    ranked = sorted(
        ((overlap(display), display.display_id, display) for display in displays),
        key=lambda item: (-item[0], item[1]),
    )
    if ranked[0][0] <= 0:
        raise ValueError("target bounds do not intersect a known display")
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        raise ValueError("target bounds overlap multiple displays equally")
    return ranked[0][2]


def frame_geometry_epoch(
    *,
    viewport: tuple[int, int],
    viewport_width: int,
    viewport_height: int,
    origin: tuple[float, float],
    scale: Optional[tuple[float, float]],
    device_pixel_ratio: Optional[float],
    display_id: str,
    display_bounds: tuple[float, float, float, float],
    display_scale: tuple[float, float],
    topology_sha256: str,
    window_identity_sha256: str,
    session_identity_sha256: str,
    page_identity_sha256: Optional[str] = None,
    top_level_frame_identity_sha256: Optional[str] = None,
) -> str:
    """Digest every fact that can change a frame-to-input coordinate mapping."""

    return _sha256_json(
        {
            "schema": "openadapt.frame-geometry.v1",
            "viewport": list(viewport),
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "origin": list(origin),
            "scale": list(scale) if scale is not None else None,
            "device_pixel_ratio": device_pixel_ratio,
            "display_id": display_id,
            "display_bounds": list(display_bounds),
            "display_scale": list(display_scale),
            "topology_sha256": topology_sha256,
            "window_identity_sha256": window_identity_sha256,
            "session_identity_sha256": session_identity_sha256,
            "page_identity_sha256": page_identity_sha256,
            "top_level_frame_identity_sha256": top_level_frame_identity_sha256,
        }
    )


def frame_coordinate_mapping_epoch(
    *,
    viewport: tuple[int, int],
    viewport_width: int,
    viewport_height: int,
    origin: tuple[float, float],
    scale: Optional[tuple[float, float]],
    device_pixel_ratio: Optional[float],
    display_id: str,
    display_bounds: tuple[float, float, float, float],
    display_scale: tuple[float, float],
    topology_sha256: str,
    window_identity_sha256: str,
    session_identity_sha256: str,
) -> str:
    """Digest the surface facts that map frame pixels to live input space.

    Page and document identities are intentionally absent. A successful action
    can navigate or replace the top-level document without changing the window,
    viewport, display, or pixel mapping. Actuation still binds the stricter
    ``geometry_epoch``. Read-only frame-region checks use this mapping epoch so
    an expected navigation does not look like a resize.
    """

    return _sha256_json(
        {
            "schema": "openadapt.frame-coordinate-mapping.v1",
            "viewport": list(viewport),
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "origin": list(origin),
            "scale": list(scale) if scale is not None else None,
            "device_pixel_ratio": device_pixel_ratio,
            "display_id": display_id,
            "display_bounds": list(display_bounds),
            "display_scale": list(display_scale),
            "topology_sha256": topology_sha256,
            "window_identity_sha256": window_identity_sha256,
            "session_identity_sha256": session_identity_sha256,
        }
    )


@dataclass(frozen=True, slots=True)
class FrameObservation:
    """One immutable frame and the exact geometry/context that produced it.

    A resolver must use ``viewport`` from this object. An input lease must bind
    this object, not a PNG followed by a later backend property read. The three
    identity digests contain no window title, account name, or captured text.
    """

    png: bytes
    viewport: tuple[int, int]
    viewport_width: int
    viewport_height: int
    origin: tuple[float, float]
    scale: Optional[tuple[float, float]]
    device_pixel_ratio: Optional[float]
    display_id: str
    display_bounds: tuple[float, float, float, float]
    display_scale: tuple[float, float]
    topology_sha256: str
    window_identity_sha256: str
    session_identity_sha256: str
    page_identity_sha256: Optional[str]
    top_level_frame_identity_sha256: Optional[str]
    geometry_epoch: str

    def __post_init__(self) -> None:
        png = bytes(self.png)
        object.__setattr__(self, "png", png)
        if _png_viewport(png) != self.viewport:
            raise ValueError(
                "frame observation viewport does not match its exact PNG bytes"
            )
        if self.viewport_width <= 0 or self.viewport_height <= 0:
            raise ValueError("frame observation top-level viewport must be positive")
        if len(self.origin) != 2 or not all(math.isfinite(v) for v in self.origin):
            raise ValueError("frame observation origin must contain two finite values")
        if self.scale is not None and (
            len(self.scale) != 2
            or not all(math.isfinite(v) and v > 0 for v in self.scale)
        ):
            raise ValueError("frame observation scale must be positive when present")
        if self.device_pixel_ratio is not None and (
            not math.isfinite(self.device_pixel_ratio) or self.device_pixel_ratio <= 0
        ):
            raise ValueError(
                "frame observation device_pixel_ratio must be positive when present"
            )
        if not isinstance(self.display_id, str) or not self.display_id:
            raise ValueError("frame observation display_id must be non-empty")
        if len(self.display_bounds) != 4 or not all(
            math.isfinite(value) for value in self.display_bounds
        ):
            raise ValueError(
                "frame observation display_bounds must contain four finite values"
            )
        if self.display_bounds[2] <= 0 or self.display_bounds[3] <= 0:
            raise ValueError("frame observation display bounds must be positive")
        if len(self.display_scale) != 2 or not all(
            math.isfinite(value) and value > 0 for value in self.display_scale
        ):
            raise ValueError("frame observation display_scale must be positive")
        for name in (
            "topology_sha256",
            "window_identity_sha256",
            "session_identity_sha256",
            "geometry_epoch",
        ):
            value = getattr(self, name)
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(f"frame observation {name} must be lower-hex SHA-256")
        if (self.page_identity_sha256 is None) != (
            self.top_level_frame_identity_sha256 is None
        ):
            raise ValueError(
                "browser page and top-level frame identities must be supplied together"
            )
        for name in ("page_identity_sha256", "top_level_frame_identity_sha256"):
            value = getattr(self, name)
            if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"frame observation {name} must be lower-hex SHA-256")
        expected_epoch = frame_geometry_epoch(
            viewport=self.viewport,
            viewport_width=self.viewport_width,
            viewport_height=self.viewport_height,
            origin=self.origin,
            scale=self.scale,
            device_pixel_ratio=self.device_pixel_ratio,
            display_id=self.display_id,
            display_bounds=self.display_bounds,
            display_scale=self.display_scale,
            topology_sha256=self.topology_sha256,
            window_identity_sha256=self.window_identity_sha256,
            session_identity_sha256=self.session_identity_sha256,
            page_identity_sha256=self.page_identity_sha256,
            top_level_frame_identity_sha256=self.top_level_frame_identity_sha256,
        )
        if self.geometry_epoch != expected_epoch:
            raise ValueError(
                "frame observation geometry_epoch does not bind its geometry"
            )

    @property
    def frame_sha256(self) -> str:
        """Digest of the exact encoded PNG bytes used by this observation."""

        return hashlib.sha256(self.png).hexdigest()

    @property
    def coordinate_mapping_epoch(self) -> str:
        """Digest the coordinate mapping without document-lifecycle identity."""

        return frame_coordinate_mapping_epoch(
            viewport=self.viewport,
            viewport_width=self.viewport_width,
            viewport_height=self.viewport_height,
            origin=self.origin,
            scale=self.scale,
            device_pixel_ratio=self.device_pixel_ratio,
            display_id=self.display_id,
            display_bounds=self.display_bounds,
            display_scale=self.display_scale,
            topology_sha256=self.topology_sha256,
            window_identity_sha256=self.window_identity_sha256,
            session_identity_sha256=self.session_identity_sha256,
        )

    @classmethod
    def create(
        cls,
        png: bytes,
        *,
        viewport_width: Optional[int] = None,
        viewport_height: Optional[int] = None,
        origin: tuple[float, float],
        scale: Optional[tuple[float, float]],
        device_pixel_ratio: Optional[float],
        display_id: str,
        display_bounds: tuple[float, float, float, float],
        display_scale: tuple[float, float],
        topology_sha256: str,
        window_identity_sha256: str,
        session_identity_sha256: str,
        page_identity_sha256: Optional[str] = None,
        top_level_frame_identity_sha256: Optional[str] = None,
    ) -> "FrameObservation":
        viewport = _png_viewport(png)
        normalized_viewport_width = (
            viewport[0] if viewport_width is None else int(viewport_width)
        )
        normalized_viewport_height = (
            viewport[1] if viewport_height is None else int(viewport_height)
        )
        normalized_origin = (float(origin[0]), float(origin[1]))
        normalized_scale = (
            (float(scale[0]), float(scale[1])) if scale is not None else None
        )
        normalized_dpr = (
            float(device_pixel_ratio) if device_pixel_ratio is not None else None
        )
        normalized_display_bounds = (
            float(display_bounds[0]),
            float(display_bounds[1]),
            float(display_bounds[2]),
            float(display_bounds[3]),
        )
        normalized_display_scale = (
            float(display_scale[0]),
            float(display_scale[1]),
        )
        return cls(
            png=png,
            viewport=viewport,
            viewport_width=normalized_viewport_width,
            viewport_height=normalized_viewport_height,
            origin=normalized_origin,
            scale=normalized_scale,
            device_pixel_ratio=normalized_dpr,
            display_id=display_id,
            display_bounds=normalized_display_bounds,
            display_scale=normalized_display_scale,
            topology_sha256=topology_sha256,
            window_identity_sha256=window_identity_sha256,
            session_identity_sha256=session_identity_sha256,
            page_identity_sha256=page_identity_sha256,
            top_level_frame_identity_sha256=top_level_frame_identity_sha256,
            geometry_epoch=frame_geometry_epoch(
                viewport=viewport,
                viewport_width=normalized_viewport_width,
                viewport_height=normalized_viewport_height,
                origin=normalized_origin,
                scale=normalized_scale,
                device_pixel_ratio=normalized_dpr,
                display_id=display_id,
                display_bounds=normalized_display_bounds,
                display_scale=normalized_display_scale,
                topology_sha256=topology_sha256,
                window_identity_sha256=window_identity_sha256,
                session_identity_sha256=session_identity_sha256,
                page_identity_sha256=page_identity_sha256,
                top_level_frame_identity_sha256=top_level_frame_identity_sha256,
            ),
        )


FrameRegion = tuple[int, int, int, int]
NormalizedRegion = tuple[float, float, float, float]


def _validate_frame_region(
    region: FrameRegion,
    *,
    viewport: tuple[int, int],
    name: str,
) -> FrameRegion:
    x, y, width, height = (int(value) for value in region)
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ValueError(f"{name} must be a positive in-frame region")
    if x + width > viewport[0] or y + height > viewport[1]:
        raise ValueError(f"{name} exceeds the source frame viewport")
    return x, y, width, height


def _validate_identity_sha256(value: str, *, name: str) -> str:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{name} must be lower-hex SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class TargetRelativeRegionBinding:
    """One evidence region relative to an exact resolved target or anchor."""

    name: str
    source_region: FrameRegion
    anchor_identity_sha256: str
    source_anchor_region: FrameRegion
    normalized_offset: NormalizedRegion


@dataclass(frozen=True, slots=True)
class FrameTargetBinding:
    """Exact-frame target geometry; viewport normalization is display-only.

    Runtime identity, effect, and REGION_STABLE checks may reuse a dependent
    region after a geometry epoch change only by resolving its named anchor
    identity again and applying ``normalized_offset`` to that fresh anchor.
    ``presentation_normalized_region`` is never runtime evidence.
    """

    frame_sha256: str
    geometry_epoch: str
    source_viewport: tuple[int, int]
    source_target_region: FrameRegion
    target_identity_sha256: str
    target_relative_regions: tuple[TargetRelativeRegionBinding, ...]
    presentation_normalized_region: NormalizedRegion


def bind_target_region(
    observation: FrameObservation,
    target_region: FrameRegion,
    *,
    target_identity_sha256: str,
    dependent_regions: tuple[tuple[str, FrameRegion], ...] = (),
) -> FrameTargetBinding:
    """Bind target-relative evidence to one exact frame observation.

    This helper deliberately does not accept a viewport-relative evidence
    region. A caller with a non-target region must name the exact anchor that
    owns it and use :func:`bind_anchor_relative_region`.
    """

    target = _validate_frame_region(
        target_region,
        viewport=observation.viewport,
        name="target_region",
    )
    identity = _validate_identity_sha256(
        target_identity_sha256,
        name="target_identity_sha256",
    )
    tx, ty, tw, th = target
    relatives = tuple(
        bind_anchor_relative_region(
            observation,
            name=name,
            region=region,
            anchor_identity_sha256=identity,
            anchor_region=target,
        )
        for name, region in dependent_regions
    )
    vw, vh = observation.viewport
    return FrameTargetBinding(
        frame_sha256=observation.frame_sha256,
        geometry_epoch=observation.geometry_epoch,
        source_viewport=observation.viewport,
        source_target_region=target,
        target_identity_sha256=identity,
        target_relative_regions=relatives,
        presentation_normalized_region=(tx / vw, ty / vh, tw / vw, th / vh),
    )


def bind_anchor_relative_region(
    observation: FrameObservation,
    *,
    name: str,
    region: FrameRegion,
    anchor_identity_sha256: str,
    anchor_region: FrameRegion,
) -> TargetRelativeRegionBinding:
    """Bind a named evidence region to one exact independently resolved anchor."""

    if not name.strip():
        raise ValueError("target-relative region requires a non-empty name")
    source = _validate_frame_region(
        region,
        viewport=observation.viewport,
        name=f"dependent region {name!r}",
    )
    anchor = _validate_frame_region(
        anchor_region,
        viewport=observation.viewport,
        name=f"anchor region for {name!r}",
    )
    identity = _validate_identity_sha256(
        anchor_identity_sha256,
        name=f"anchor identity for {name!r}",
    )
    x, y, width, height = source
    ax, ay, aw, ah = anchor
    return TargetRelativeRegionBinding(
        name=name.strip(),
        source_region=source,
        anchor_identity_sha256=identity,
        source_anchor_region=anchor,
        normalized_offset=(
            (x - ax) / aw,
            (y - ay) / ah,
            width / aw,
            height / ah,
        ),
    )


@runtime_checkable
class FrameObservationBackend(Protocol):
    """Backend that proves frame bytes and geometry in one read operation."""

    def observe_frame(self) -> FrameObservation:
        """Return one immutable frame observation."""
        ...


@runtime_checkable
class ActuationObservationBackend(Protocol):
    """Backend that arms input against one exact frame observation."""

    def acquire_actuation_observation(self) -> FrameObservation:
        """Acquire and arm one fresh observation for the next input edge."""
        ...


@runtime_checkable
class FrameObservationLeaseBackend(Protocol):
    """Input backend that consumes the descriptor resolved by the runtime."""

    def bind_input_observation(self, observation: FrameObservation) -> None:
        """Bind the next input lease to ``observation`` or refuse it."""
        ...


class StructuralResolutionRefused(RuntimeError):
    """A structural backend found candidates but could not prove uniqueness.

    This is a safety refusal, not an ordinary miss. The resolver must not hide
    it by falling through to a weaker visual rung that could choose one of the
    same ambiguous controls.
    """


class ActionDeliveryUncertain(RuntimeError):
    """An action API failed after delivery may already have begun.

    This is deliberately distinct from both a pre-delivery safety refusal and
    a successful delivery receipt.  The runtime must not retry the action.  It
    may report success only after the configured postcondition and independent
    effect contracts fully confirm the intended business outcome.

    The exception retains only bounded, non-payload metadata.  Backend error
    messages can contain page text, URLs, or identifiers and therefore are not
    copied into the run report.
    """

    def __init__(
        self,
        *,
        operation: str,
        native: bool,
        target_fingerprint: Optional[str] = None,
        cause_type: str = "BackendError",
    ) -> None:
        super().__init__(
            "action delivery may have occurred; independent verification required"
        )
        self.operation = operation
        self.native = native
        self.target_fingerprint = target_fingerprint
        self.cause_type = cause_type


class FreshActuationRequired(RuntimeError):
    """The observed surface changed before the first input edge.

    This exception is the narrow, retryable counterpart to
    :class:`ActionDeliveryUncertain`.  A backend may raise it only after it has
    proved that the current gesture emitted no input edge.  The runtime can
    then acquire a new frame and repeat target and identity resolution without
    risking a duplicate action.

    The diagnostic fields describe only pixel geometry and counts.  They never
    retain the rejected frame or any pixel values.
    """

    def __init__(
        self,
        *,
        operation: str,
        changed_pixel_count: int,
        changed_bbox: Optional[tuple[int, int, int, int]],
        frame_size: tuple[int, int],
        expected_geometry_epoch: Optional[str] = None,
        observed_geometry_epoch: Optional[str] = None,
        expected_observation: Optional[FrameObservation] = None,
        observed_observation: Optional[FrameObservation] = None,
    ) -> None:
        super().__init__(
            "surface changed before input because frame content changed; "
            "acquire a fresh actuation frame"
        )
        if changed_pixel_count < 1:
            raise ValueError("fresh-actuation mismatch must change at least one pixel")
        if frame_size[0] <= 0 or frame_size[1] <= 0:
            raise ValueError("fresh-actuation frame size must be positive")
        if changed_bbox is None:
            raise ValueError("fresh-actuation mismatch requires a bounding box")
        x, y, width, height = changed_bbox
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise ValueError("fresh-actuation bounding box must be positive")
        if x + width > frame_size[0] or y + height > frame_size[1]:
            raise ValueError("fresh-actuation bounding box exceeds the frame")
        if (expected_geometry_epoch is None) != (observed_geometry_epoch is None):
            raise ValueError(
                "fresh-actuation geometry epochs must be supplied together"
            )
        if (expected_observation is None) != (observed_observation is None):
            raise ValueError("fresh-actuation observations must be supplied together")
        if expected_observation is not None and observed_observation is not None:
            if expected_geometry_epoch is None:
                expected_geometry_epoch = expected_observation.geometry_epoch
                observed_geometry_epoch = observed_observation.geometry_epoch
            elif (
                expected_geometry_epoch != expected_observation.geometry_epoch
                or observed_geometry_epoch != observed_observation.geometry_epoch
            ):
                raise ValueError(
                    "fresh-actuation observations do not match supplied epochs"
                )
        for value in (expected_geometry_epoch, observed_geometry_epoch):
            if value is not None and (
                len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ValueError("fresh-actuation geometry epoch must be SHA-256")
        self.operation = operation
        self.changed_pixel_count = changed_pixel_count
        self.changed_bbox = changed_bbox
        self.frame_size = frame_size
        self.expected_geometry_epoch = expected_geometry_epoch
        self.observed_geometry_epoch = observed_geometry_epoch
        self.expected_observation = expected_observation
        self.observed_observation = observed_observation


class DisplayTopologyChanged(RuntimeError):
    """The complete display topology changed outside an admitted transition.

    This is not a retryable stale-frame mismatch. Hot-plug, display removal,
    arrangement changes, and DPI changes can change the meaning or availability
    of every coordinate. The runtime must start a separately qualified session
    transition before it can continue.
    """

    def __init__(
        self,
        *,
        expected_observation: FrameObservation,
        observed_observation: FrameObservation,
    ) -> None:
        if expected_observation.topology_sha256 == observed_observation.topology_sha256:
            raise ValueError("display-topology exception requires a topology change")
        super().__init__(
            "display topology changed outside an admitted topology-transition "
            "contract; invalidate this execution session"
        )
        self.expected_observation = expected_observation
        self.observed_observation = observed_observation


@runtime_checkable
class SystemOfRecordBackend(Protocol):
    """Optional system-of-record observation a backend MAY expose.

    Vision (and even structural URL/title) cannot see whether a consequential
    write actually reached the system of record — a partial save, a phantom
    optimistic-UI success, a duplicate submission all look identical on screen
    (``docs/LIMITS.md`` "5 of 7 write faults silent"). A backend that can read
    the app's authoritative store (a JSON ``/api/db`` endpoint, an EMR's own
    API) exposes it here; the recorder snapshots it before and after each event
    (``sor_before`` / ``sor_after`` on the event, exactly as it already records
    ``url_before`` / ``url_after``), and the compiler's effect miner
    (``compiler.effect_mining``) derives typed ``record_written`` /
    ``field_equals`` effects from the observed delta.

    Backends without a readable system of record (pixel-only substrates) simply
    do not implement this; the miner then falls back to a flagged placeholder
    or an honest "no verifiable effect derivable" (never a fabricated binding).
    """

    @property
    def system_of_record(self) -> Optional[list[dict[str, Any]]]:
        """Current system-of-record records, or None if unobservable.

        None (not ``[]``) when the store cannot be read right now — the miner
        distinguishes "not observed" from "observed empty" (a legitimate
        baseline for a first write).
        """
        ...


@runtime_checkable
class StructuralBackend(Protocol):
    """Optional structural observations a backend MAY expose.

    Vision alone cannot see effects that never render in the frame — a
    new-tab click, an SPA route change below the fold. Backends that can
    cheaply observe URL / title / page count expose these read-only
    properties; the recorder captures them per event and the compiler mines
    *structural* postconditions (URL_CHANGED, TITLE_CHANGED, NEW_TAB_OPENED)
    as a fallback for steps that would otherwise assert nothing. Backends
    without these observations (native OS, RDP) simply don't implement them;
    such steps stay honestly unverified (docs/LIMITS.md).

    Each property returns None when the observation is momentarily
    unavailable (e.g. mid-navigation).
    """

    @property
    def url(self) -> Optional[str]:
        """Current page URL, or None if unobservable."""
        ...

    @property
    def page_title(self) -> Optional[str]:
        """Current page title, or None if unobservable."""
        ...

    @property
    def page_count(self) -> Optional[int]:
        """Number of open pages/tabs, or None if unobservable."""
        ...


@runtime_checkable
class IdentityBackend(Protocol):
    """Optional STRUCTURED-TEXT identity capability a backend MAY expose.

    The runtime resolves targets by VISION alone (screenshot in, clicks out);
    that never changes. But *identity verification* -- proving the resolved
    target is the recorded entity, not a look-alike sibling -- does not have
    to be OCR-based when the backend can hand back the real, structured
    characters under a point.

    An adversarial review proved the OCR-only identity path cannot close the
    same-name / same-DOB glyph-collapse case: two DIFFERENT patients whose MRN
    differs only by an O/0 or l/1 glyph ("MG4408" vs "MG44O8") render to a
    byte-identical OCR band -- the same input a legit re-read produces -- so
    no function downstream of OCR can distinguish them (see docs/LIMITS.md and
    benchmark/dense_surface/DENSE_SURFACE.md). The escape is to stop relying
    on OCR for identity where a higher-fidelity signal exists.

    ``structured_text_at`` returns that higher-fidelity signal: the accessible
    / DOM text at (or around) a coordinate, in the REAL characters --
    "MG4408" with a genuine digit 0, not an OCR guess. Backends implement it
    from whatever structured layer they own:

    - a browser backend (Playwright) reads the DOM element under the point
      (``elementFromPoint`` -> row/cell ``textContent`` + ``aria-label``);
    - a native desktop backend reads the accessibility tree -- Windows UI
      Automation ``Name``/``Value``/text, macOS AX attributes, or Linux AT-SPI
      accessible text. Crucially,
      an element lacking a stable ``AutomationId`` usually STILL exposes
      Name/Value text, so UIA/AX identity is viable on most native apps even
      where an AutomationId-keyed selector is not.

    The identity ladder treats DOM and UIA/AX text identically -- both are
    "structured text". A pure-pixel substrate (Citrix/RDP/VDI, or a backend
    with no a11y tree) returns None from every point, and identity falls back
    to the OCR name+DOB-primary tier (docs/LIMITS.md). This is an ADDITIVE
    identity capability: the 4-method vision resolution protocol
    (:class:`Backend`) is unchanged.
    """

    def structured_text_at(self, x: int, y: int) -> Optional[str]:
        """Return the structured (DOM / a11y) text at/around pixel (x, y).

        The coordinate space matches :meth:`Backend.click` -- the same pixels
        the resolver emits. Returns the target's row/element text in its REAL
        characters, or None when the backend cannot observe structured text at
        that point (pixel-only substrate, no a11y node, or a momentary
        failure -- never raises).
        """
        ...


@runtime_checkable
class FieldLabelBackend(Protocol):
    """Optional RECORD-TIME field-label capability a backend MAY expose.

    When a demonstrator types into a field, the RECORDER (not the replayer)
    asks the backend for the focused field's best available human label so a
    non-engineer never has to name parameters in code: the label becomes
    passive evidence (``field_label`` on the TYPE event and
    ``ir.Step.field_label``) that the compile-time parameter-proposal pass
    turns into a slugified, OPERATOR-CONFIRMED parameter name (see
    ``compiler.annotate.FieldLabelAnnotator``).

    Implementations read whatever structured layer they own:

    - a browser backend (Playwright) reads the focused element's associated
      DOM ``<label>``, ``aria-label`` / ``aria-labelledby``, ``placeholder``,
      ``name`` attribute, or ``title`` -- in that order;
    - a native desktop backend reads the accessibility label of the focused
      control (UIA ``Name``, macOS ``AXTitle``/``AXDescription``, AT-SPI
      accessible name) where that seam exists.

    This is PASSIVE metadata capture only: querying the label must never
    change focus, field contents, or timing beyond a cheap read, and a
    pixel-only substrate simply returns None (the compiler may then fall back
    to nearby-OCR evidence where the recording carries a field rectangle).
    Never called at replay.
    """

    def focused_field_label(self) -> Optional[str]:
        """Return the focused field's best available label, or None.

        Whitespace-collapsed; None when no field is focused, the backend has
        no structured layer, or on any momentary failure (never raises).
        """
        ...


@runtime_checkable
class ExecutionContextIdentityBackend(Protocol):
    """Optional live identities that cannot be inferred from record-row text."""

    def application_identity(self) -> Optional[str]:
        """Return the current application/window identity."""
        ...

    def session_identity(self) -> Optional[str]:
        """Return the current runner/remote-session identity digest."""
        ...

    def workflow_state_identity(self) -> Optional[str]:
        """Return the current application workflow-state identity."""
        ...


@runtime_checkable
class BrowserPresentationGeometryBackend(Protocol):
    """Optional exact browser geometry for an out-of-page presentation layer.

    This capability does not inject markup, expose a selector, or participate
    in target resolution.  It lets the presentation-only control-overlay rail
    normalize an already-resolved rectangle to the top-level browser CSS
    viewport.  Native desktop and pixel-only RDP/Citrix backends deliberately
    do not implement it; those coordinate spaces require their own contracts.
    """

    def browser_presentation_viewport(self) -> Optional[tuple[int, int, float]]:
        """Return ``(CSS width, CSS height, device pixel ratio)`` or ``None``."""
        ...


@runtime_checkable
class StructuralActionBackend(Protocol):
    """Optional STRUCTURAL action capability a backend MAY expose.

    The runtime resolves targets by a LADDER (see
    :mod:`openadapt_flow.runtime.resolver`). Its top rung is structural: where
    the backend owns a structured layer (a browser's DOM, a native app's UIA/AX
    tree) the runtime re-finds the recorded target as an ELEMENT and acts on its
    center DETERMINISTICALLY, instead of pixel-matching a template that render
    drift (relabel, theme, zoom, layout shift) can defeat. The desktop benchmark
    measured UIA execution 21/21 vs compiled visual replay 6/21 under drift.

    This is the thesis shift from "vision-only" to "deterministic compiled
    automation with visual FALLBACK". It is ADDITIVE and backend-optional: a
    pixel-only substrate (RDP/Citrix/canvas, or a backend with no structured
    layer) simply does not implement it, and resolution falls through to the
    visual rungs (template/ocr/geometry) UNCHANGED -- the healthcare/Citrix
    floor is never removed.

    Crucially, the structurally-resolved point flows through the IDENTICAL click
    path as any visual resolution, so the pre-click identity gate and the
    irreversible risk gate still fire on it: structure makes identity STRONGER
    (an exact element), it never bypasses it.

    Two methods, mirroring the record/replay split of the identity capability
    (:class:`IdentityBackend`):

    - ``structural_locator_at`` runs at RECORD time: given the demonstrated
      click point, return a STABLE structural locator the runtime can re-resolve
      later (a DOM ``#id`` / role+name, a UIA ``AutomationId`` / role+name,
      or an AT-SPI accessible ID / role+name).
    - ``locate_structural`` runs at REPLAY time: given that recorded locator,
      find the element on the LIVE surface and return its center point.

    Each returns None when the capability is momentarily unavailable, the
    element is absent/ambiguous, or the substrate has no structured layer at
    that point (never raises) -- resolution then uses the visual ladder.
    """

    def structural_locator_at(self, x: int, y: int) -> Optional["StructuralLocator"]:
        """Return a stable structural locator for the element at pixel (x, y).

        The coordinate space matches :meth:`Backend.click`. Returns None when
        the backend cannot derive a stable locator (no structured node under the
        point, or nothing that identifies the element durably) -- the step then
        relies on the visual anchor alone.
        """
        ...

    def locate_structural(
        self, locator: "StructuralLocator"
    ) -> Optional["StructuralHandle"]:
        """Locate ``locator``'s unique element on the live surface.

        A backend MUST raise :class:`StructuralResolutionRefused` when one or
        more candidates exist but uniqueness cannot be established. ``None``
        is reserved for an ordinary miss/unavailable structural substrate, so
        the runtime cannot hide ambiguity by falling through to visual match.
        """
        ...


@runtime_checkable
class NativeStructuralActionBackend(Protocol):
    """Optional native element actuation after governed structural resolution.

    ``act_structural`` re-resolves the exact locator, requires the candidate
    fingerprint returned by ``locate_structural``, and uses the strongest UIA
    pattern available. Its receipt proves input delivery only; postconditions
    and independent effects remain authoritative for outcome verification.
    """

    def act_structural(
        self,
        locator: "StructuralLocator",
        handle: "StructuralHandle",
        *,
        double: bool = False,
    ) -> "ActionDeliveryReceipt":
        """Deliver a native action to the same unique structural candidate."""
        ...


@runtime_checkable
class GuardedCoordinateActionBackend(Protocol):
    """Optional atomic local coordinate actuation.

    A backend implementing this seam must bind the identity-verified target to
    delivery and reject a changed frame/element/record before the first input
    edge. A pixel backend can compare the expected frame under its input lock; a
    DOM backend can use an opaque element token plus a mutation guard and retain
    its native pointer-event semantics. This is the local counterpart to a
    remote backend's one-shot actuation lease.
    """

    def arm_guarded_coordinate(self, x: int, y: int) -> None:
        """Bind one identity-bearing actionable target before identity readback."""
        ...

    def cancel_guarded_coordinate(self) -> None:
        """Cancel and clean any unconsumed guarded-coordinate binding."""
        ...

    def act_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
        button: str = "left",
    ) -> "ActionDeliveryReceipt":
        """Verify the exact frame and deliver one coordinate action atomically."""
        ...


@runtime_checkable
class FocusedElementActuationLeaseBackend(Protocol):
    """Optional focused-control binding layered onto a remote frame lease.

    Native backends that already use :class:`RemoteActuationBackend` for an
    exact frame/window lease can additionally bind the freshly resolved target
    point to one accessibility-focused element before final identity readback.
    Delivery must refuse if that opaque element changes while the pixels remain
    indistinguishable.
    """

    def arm_focused_element_lease(self, x: int, y: int) -> None:
        """Bind the current focused element to the resolved target point."""
        ...

    def cancel_focused_element_lease(self) -> None:
        """Cancel an unconsumed focused-element binding."""
        ...


@runtime_checkable
class GuardedKeyboardActionBackend(Protocol):
    """Optional browser-local focus/record lease for consequential keyboard I/O.

    The backend arms the exact focused element and its identity-bearing record
    before the runtime's final identity observation. Focus changes or any
    target/record mutation invalidate the one-shot lease before a key or text
    input can be delivered.
    """

    def guarded_keyboard_frame(self) -> bytes:
        """Capture the exact frame without mutating focused-element caret state."""
        ...

    def arm_guarded_keyboard(self, x: int, y: int) -> None:
        """Bind the focused element at the freshly resolved identity point."""
        ...

    def cancel_guarded_keyboard(self) -> None:
        """Cancel and clean any unconsumed guarded-keyboard binding."""
        ...

    def press_guarded(
        self,
        key: str,
        *,
        expected_frame_sha256: str,
    ) -> "ActionDeliveryReceipt":
        """Deliver one key/chord to the pre-identity focused-element lease."""
        ...

    def type_text_guarded(
        self,
        text: str,
        *,
        expected_frame_sha256: str,
    ) -> "ActionDeliveryReceipt":
        """Type into the pre-identity focused-element lease."""
        ...


@runtime_checkable
class TextValueBackend(Protocol):
    """Optional exact readback for the editable control under a point.

    A screenshot/OCR verifier cannot reliably distinguish a long unrendered
    value from password dots or platform-specific glyph noise. Backends with a
    structural accessibility surface can return the control's current value so
    the runtime can confirm text delivery without weakening to pixel change.
    The value is compared in memory only and must never be logged.
    """

    def text_value_at(self, x: int, y: int) -> Optional[str]:
        """Return the exact editable value, or None when unavailable."""
        ...

    def focused_text_value(self) -> Optional[str]:
        """Return the exact focused editable value, or None."""
        ...


@runtime_checkable
class SelectOptionBackend(Protocol):
    """Atomic parameterized option selection for opaque/native controls.

    Implementations keep the type-ahead text and Enter/Tab commit under one
    input/focus lease. This protocol reports delivery only; the runtime still
    requires the intended committed value inside a compiler-qualified readback
    band mapped onto the freshly re-resolved live field before continuing.
    """

    def select_option(self, text: str, commit_key: str) -> None:
        """Type ``text`` and commit it with Enter/Tab as one input operation."""
        ...


@runtime_checkable
class GuardedSelectOptionBackend(Protocol):
    """Exact option selection bound to a fresh target/frame observation."""

    def select_option_guarded(
        self,
        text: str,
        commit_key: str,
        *,
        target_point: "Point",
        expected_frame_sha256: str,
    ) -> "ActionDeliveryReceipt":
        """Deliver text+commit and return a typed delivery-only receipt."""
        ...


@runtime_checkable
class RemoteActuationBackend(Protocol):
    """Optional two-phase actuation seam for opaque remote surfaces.

    A remote backend implements this when input crosses an RDP/Citrix/VDI
    boundary whose guest-side focus and contents can change after ordinary
    target resolution.  The method acquires the exact remote client/session,
    validates readiness, and captures the frame that the runtime must
    re-resolve immediately before a consequential action.

    The backend owns a one-shot content lease for the returned frame.  Its next
    input method captures once more under the backend input lock and refuses
    before the first input edge if the window/session, dimensions, readiness,
    or exact frame content changed.  A sealed remote frame contract can exclude
    reviewed volatile regions from a derived comparison.  Raw frame evidence
    and the exact lease stay unmodified.  The lease is consumed once so a
    multi-character type or double-click gesture cannot invalidate itself.
    """

    def acquire_actuation_frame(self) -> bytes:
        """Acquire focus/readiness and return the freshly leased PNG frame."""
        ...


@runtime_checkable
class RemoteFrameContractBackend(Protocol):
    """Optional pre-input protected-region binding for remote comparison masks."""

    def arm_remote_frame_contract(
        self, *, protected_regions: tuple[tuple[int, int, int, int], ...]
    ) -> None: ...


@runtime_checkable
class FreshActuationReacquisitionBackend(Protocol):
    """Reset one proved zero-input invalidation for bounded reacquisition.

    The runtime calls this seam only after :class:`FreshActuationRequired`.
    Implementations must refuse every lease state except the typed invalidated
    state.  The reset grants no actuation authority; the runtime must repeat
    its complete fresh-frame, target, identity, and authorization checks before
    another delivery attempt.
    """

    def reset_fresh_actuation_state(self) -> None:
        """Clear one typed invalidation so a complete fresh lease can be made."""
        ...


@runtime_checkable
class PreparedPointerActuationBackend(Protocol):
    """Optional pointer pre-positioning for opaque remote-display clients.

    Moving a remote cursor can legitimately repaint hover, caret, or remote
    cursor pixels.  A backend implementing this seam positions the pointer
    *before* the runtime acquires its final consequential actuation frame.  The
    runtime then re-resolves the target and identity on that post-hover frame;
    delivery must use the same prepared point without another pointer move.
    """

    def prepare_pointer_actuation(self, x: int, y: int) -> None:
        """Position the pointer without pressing a button or claiming success."""
        ...


@runtime_checkable
class RichPointerActionBackend(Protocol):
    """Pointer actions beyond the primary/double click contract."""

    def right_click(self, x: int, y: int) -> None: ...

    def drag(self, x: int, y: int, end_x: int, end_y: int) -> None: ...


@runtime_checkable
class GuardedDragActionBackend(Protocol):
    """Consume a pre-identity coordinate lease for one exact drag."""

    def drag_guarded(
        self,
        x: int,
        y: int,
        end_x: int,
        end_y: int,
        *,
        expected_frame_sha256: str,
    ) -> "ActionDeliveryReceipt": ...


@runtime_checkable
class GuardedRemotePointerActionBackend(Protocol):
    """Consume one exact remote frame lease for a pointer gesture.

    The receipt proves which freshly observed frame and point received the
    input. It does not prove the workflow outcome. Postconditions and effect
    verification remain authoritative.
    """

    def click_guarded(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
    ) -> "ActionDeliveryReceipt": ...

    def right_click_guarded(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
    ) -> "ActionDeliveryReceipt": ...

    def drag_guarded(
        self,
        x: int,
        y: int,
        end_x: int,
        end_y: int,
        *,
        expected_frame_sha256: str,
    ) -> "ActionDeliveryReceipt": ...


@runtime_checkable
class Backend(Protocol):
    @property
    def viewport(self) -> tuple[int, int]:
        """(width, height) of the screen surface in pixels."""
        ...

    def screenshot(self) -> bytes:
        """Return the current frame as PNG bytes."""
        ...

    def click(self, x: int, y: int, *, double: bool = False) -> None: ...

    def type_text(self, text: str) -> None:
        """Type text into the currently focused element."""
        ...

    def press(self, key: str) -> None:
        """Press a key or chord, e.g. 'Enter', 'Tab', 'Meta+a'."""
        ...

    def scroll(self, dx: int, dy: int) -> None:
        """Scroll by (dx, dy) pixels — a wheel gesture at the current
        pointer position (positive dy scrolls content up / view down)."""
        ...

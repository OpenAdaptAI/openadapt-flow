"""Intermediate representation for compiled workflows.

A Workflow is the compiled artifact: an ordered list of Steps, each carrying
redundant evidence about its target (Anchor), the action to perform, and
assertions about what the screen should look like afterwards (Postconditions).

The canonical serialized form is a *bundle directory*:

    <bundle>/
      workflow.json        # Workflow.model_dump_json()
      templates/*.png      # anchor template crops, referenced by relative path

All coordinates are pixels in the recorded frame's coordinate space.
Regions are (x, y, w, h). Points are (x, y).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

Region = tuple[int, int, int, int]
Point = tuple[int, int]


class ActionKind(str, Enum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    TYPE = "type"
    KEY = "key"
    WAIT = "wait"


class Landmark(BaseModel):
    """A stable nearby text element, used by the geometry resolution rung.

    ``relation`` describes where the LANDMARK sits relative to the target:
    a landmark that is ``left_of`` the target implies the target is
    ``distance_px`` to the landmark's right. ``dx_px``/``dy_px``, when set,
    are the exact pixel offsets from the landmark's center to the target
    click point (target = landmark_center + (dx_px, dy_px)); the geometry
    rung prefers them over the coarser relation/distance estimate.
    """

    relation: Literal["left_of", "right_of", "above", "below"]
    ocr_text: str
    distance_px: int
    dx_px: Optional[int] = Field(
        default=None,
        description="Exact x offset landmark center -> target click point",
    )
    dy_px: Optional[int] = Field(
        default=None,
        description="Exact y offset landmark center -> target click point",
    )


class Anchor(BaseModel):
    """Redundant visual evidence for locating a step's target on screen.

    Resolution ladder consumes fields in order of preference:
    template (local, then global) -> ocr_text -> landmarks -> grounder.
    """

    template: str = Field(description="Bundle-relative path to the PNG crop")
    region: Region = Field(description="Crop location in the recorded frame")
    click_point: Point = Field(description="Click point in the recorded frame")
    ocr_text: Optional[str] = Field(
        default=None, description="Text label at/near the target, if any"
    )
    landmarks: list[Landmark] = Field(default_factory=list)
    search_pad: int = Field(
        default=80,
        description="Pixels of padding around `region` for the local search",
    )


class PostconditionKind(str, Enum):
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    REGION_STABLE = "region_stable"  # phash of `region` within tolerance


class Postcondition(BaseModel):
    kind: PostconditionKind
    text: Optional[str] = None
    region: Optional[Region] = None
    phash: Optional[str] = None
    phash_tolerance: int = 8
    timeout_s: float = 5.0


class Step(BaseModel):
    id: str
    intent: str = Field(description="Human-readable purpose of the step")
    action: ActionKind
    anchor: Optional[Anchor] = None  # None for pure keyboard/wait steps
    text: Optional[str] = None  # literal text for TYPE
    param: Optional[str] = None  # if set, TYPE text comes from params[param]
    key: Optional[str] = None  # for KEY, e.g. "Enter"
    expect: list[Postcondition] = Field(default_factory=list)
    risk: Literal["reversible", "irreversible"] = "reversible"
    timeout_s: float = 10.0


class Workflow(BaseModel):
    schema_version: int = 1
    name: str
    recording_id: Optional[str] = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    viewport: Optional[tuple[int, int]] = None
    params: dict[str, str] = Field(
        default_factory=dict, description="param name -> example/default value"
    )
    steps: list[Step] = Field(default_factory=list)

    # -- bundle I/O ---------------------------------------------------------

    def save(self, bundle_dir: Path | str) -> Path:
        """Write workflow.json into bundle_dir (templates are written by the
        compiler / healer, which own the crop images)."""
        bundle = Path(bundle_dir)
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "templates").mkdir(exist_ok=True)
        path = bundle / "workflow.json"
        path.write_text(self.model_dump_json(indent=2))
        return path

    @classmethod
    def load(cls, bundle_dir: Path | str) -> "Workflow":
        bundle = Path(bundle_dir)
        return cls.model_validate(
            json.loads((bundle / "workflow.json").read_text())
        )


# -- runtime results ---------------------------------------------------------

Rung = Literal["template", "template_global", "ocr", "geometry", "grounder"]


class Resolution(BaseModel):
    rung: Rung
    point: Point
    confidence: float
    elapsed_ms: float


class HealEvent(BaseModel):
    step_id: str
    kind: Literal["anchor_refresh"] = "anchor_refresh"
    rung_used: Rung
    old_anchor: Anchor
    new_anchor: Anchor
    screenshot: Optional[str] = None  # run-dir-relative path
    applied: bool = False


class StepResult(BaseModel):
    step_id: str
    intent: str
    ok: bool
    resolution: Optional[Resolution] = None
    postconditions_ok: Optional[bool] = None
    heal: Optional[HealEvent] = None
    error: Optional[str] = None
    before_png: Optional[str] = None  # run-dir-relative paths
    after_png: Optional[str] = None
    elapsed_ms: float = 0.0


class RunReport(BaseModel):
    workflow_name: str
    started_at: str
    params: dict[str, str] = Field(default_factory=dict)
    results: list[StepResult] = Field(default_factory=list)
    success: bool = False
    rung_counts: dict[str, int] = Field(default_factory=dict)
    heal_count: int = 0
    model_calls: int = 0
    est_model_cost_usd: float = 0.0
    total_ms: float = 0.0

    def save(self, run_dir: Path | str) -> Path:
        run = Path(run_dir)
        run.mkdir(parents=True, exist_ok=True)
        path = run / "report.json"
        path.write_text(self.model_dump_json(indent=2))
        return path

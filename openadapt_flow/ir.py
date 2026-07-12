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
    SCROLL = "scroll"


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
    context_text: Optional[str] = Field(
        default=None,
        description=(
            "Identity evidence: OCR text on the target's row (full-width"
            " band at the crop's height) EXCLUDING the target's own crop"
            " and timestamp-bearing lines. Verified before every click"
            " (see runtime.identity); None when the band had no usable"
            " text at compile time."
        ),
    )
    structured_identity: Optional[str] = Field(
        default=None,
        description=(
            "STRUCTURED identity text (DOM / accessibility tree) of the"
            " clicked target's row, captured at record time when the"
            " recording backend exposes it"
            " (openadapt_flow.backend.IdentityBackend.structured_text_at)."
            " The REAL characters (a genuine digit 0 vs a letter O), so"
            " replay verifies identity by exact/normalized string compare"
            " with NO OCR ambiguity -- the structured-text tier of the"
            " identity ladder (see runtime.identity). None on pixel-only"
            " substrates or bundles recorded before this capability; the"
            " ladder then falls back to the OCR context_text tier."
        ),
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
    # Structural postconditions — mined as a fallback for steps whose action
    # changed nothing visible in the single-page frame (new-tab navigation,
    # SPA route changes off-screen), so such steps are no longer vacuous.
    # They compare the step's END state against its START state on the live
    # backend; nothing instance-specific (no literal URL/title) is baked in.
    # On a backend that cannot observe the property, they pass with the step
    # honestly still unverified (see docs/LIMITS.md).
    URL_CHANGED = "url_changed"  # page URL differs from the step's start
    TITLE_CHANGED = "title_changed"  # page title differs from the step's start
    NEW_TAB_OPENED = "new_tab_opened"  # browser page count increased


class Postcondition(BaseModel):
    kind: PostconditionKind
    text: Optional[str] = None
    region: Optional[Region] = None
    phash: Optional[str] = None
    phash_tolerance: int = 8
    timeout_s: float = 5.0
    template: Optional[str] = Field(
        default=None,
        description=(
            "Bundle-relative PNG crop of the expected REGION_STABLE content;"
            " lets the check tolerate small layout shifts (content found"
            " near, not exactly at, the recorded region)"
        ),
    )


class Step(BaseModel):
    id: str
    intent: str = Field(description="Human-readable purpose of the step")
    action: ActionKind
    anchor: Optional[Anchor] = None  # None for pure keyboard/wait steps
    text: Optional[str] = None  # literal text for TYPE
    param: Optional[str] = None  # if set, TYPE text comes from params[param]
    key: Optional[str] = None  # for KEY, e.g. "Enter"
    scroll_dx: Optional[int] = None  # for SCROLL: wheel delta, px right
    scroll_dy: Optional[int] = None  # for SCROLL: wheel delta, px down
    expect: list[Postcondition] = Field(default_factory=list)
    risk: Literal["reversible", "irreversible"] = "reversible"
    timeout_s: float = 10.0
    # Identity-protection audit trail (clicks and anchored TYPE steps):
    # whether this step's click is guarded by the pre-click identity check
    # (anchor.context_text present). Written by the compiler so an
    # operator can audit a bundle's protection coverage BEFORE running it;
    # None on non-click steps and on bundles compiled before this field
    # existed. An UNARMED click proceeds with NO identity verification
    # (see docs/LIMITS.md).
    identity_armed: Optional[bool] = Field(
        default=None,
        description=(
            "Clicks/anchored TYPE only: True when the pre-click identity"
            " check is armed (context band recorded); False when the step"
            " will click WITHOUT identity verification; None for steps"
            " the check does not apply to (or pre-metric bundles)."
        ),
    )
    identity_unarmed_reason: Optional[str] = Field(
        default=None,
        description=(
            "Why an applicable step compiled unarmed (no readable band"
            " text, band too generic, ...); None when armed or not"
            " applicable."
        ),
    )


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


class IdentityCheck(BaseModel):
    """Outcome of the pre-click target-identity check (runtime.identity).

    Attributes:
        status: ``verified`` (band matched), ``mismatch`` (band readable but
            wrong — the run must halt, never click), or ``unreadable`` (OCR
            found no usable text in the live band; identity could not be
            judged — the step proceeds flagged, and irreversible steps
            refuse).
        mode: ``structured`` compares the recorded DOM/a11y identity text
            against the live structured text at the resolved point (the
            highest-fidelity tier -- no OCR ambiguity); ``context`` compares
            against the recorded OCR band text (the pixel-substrate fallback);
            ``param`` re-anchors on the RUN's value for a parameter whose demo
            value was embedded in the recorded band.
        coverage: Matched fraction (context mode) or run/required ratio
            (param mode), diagnostic.
        expected: What the check looked for (recorded band text, or the
            run's param value on a param-mode mismatch).
        observed: Live band text the verdict was based on.
        param: The parameter that drove a param-mode check, if any.
    """

    status: Literal["verified", "mismatch", "unreadable"]
    mode: Literal["context", "param", "structured"] = "context"
    coverage: float = 0.0
    expected: str = ""
    observed: str = ""
    param: Optional[str] = None


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
    identity: Optional[IdentityCheck] = None  # pre-click identity verdict
    input_verified: Optional[bool] = None  # TYPE steps: typed input landed
    input_retried: bool = False  # TYPE steps: refocus-and-retype fired
    postconditions_ok: Optional[bool] = None
    heal: Optional[HealEvent] = None
    error: Optional[str] = None
    before_png: Optional[str] = None  # run-dir-relative paths
    after_png: Optional[str] = None
    elapsed_ms: float = 0.0


class UnarmedStep(BaseModel):
    """A click step that will proceed with NO identity verification."""

    step_id: str
    intent: str = ""
    reason: str = ""


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
    # Identity-protection coverage of the WHOLE workflow (computed at run
    # start from the bundle, not just from executed steps): how many of
    # the identity-applicable steps (clicks / anchored TYPE) carry an
    # armed pre-click identity check, and which proceed unguarded.
    identity_applicable_steps: int = 0
    identity_armed_steps: int = 0
    identity_unarmed: list[UnarmedStep] = Field(default_factory=list)

    def save(self, run_dir: Path | str) -> Path:
        run = Path(run_dir)
        run.mkdir(parents=True, exist_ok=True)
        path = run / "report.json"
        path.write_text(self.model_dump_json(indent=2))
        return path

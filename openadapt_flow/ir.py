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
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    # Type-only import for the Step.effects forward reference. The RUNTIME
    # import is at the BOTTOM of this module (see the note there) to avoid a
    # circular import through openadapt_flow.runtime's package init.
    from openadapt_flow.runtime.effects.effect import Effect

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


class StructuralLocator(BaseModel):
    """A stable structural (DOM / accessibility) locator for a step's target.

    Captured at record time from the recording backend's structured layer
    (:meth:`openadapt_flow.backend.StructuralActionBackend.structural_locator_at`)
    and consumed at replay by the structural ACTION rung -- the TOP of the
    resolution ladder (:mod:`openadapt_flow.runtime.resolver`). The runtime
    re-finds the SAME element by its stable identity -- a DOM id / CSS selector
    / ARIA role+name, or a Windows UIA ``AutomationId`` / ``ControlType``+
    ``Name`` -- and acts on the element's center DETERMINISTICALLY, with no
    pixel matching. This is the thesis shift from "vision-only" to
    "deterministic compiled automation with visual FALLBACK": the desktop
    benchmark measured UIA execution 21/21 vs compiled visual replay 6/21 under
    render drift.

    The visual anchor (template / ocr_text / landmarks) is ALWAYS kept too: the
    ladder falls through to it UNCHANGED when this locator is absent (pixel-only
    substrate -- RDP/Citrix/canvas) or when the element cannot be located at
    replay (see docs/LIMITS.md). Structural resolution is ADDITIVE -- it never
    removes the visual floor.

    Fields are backend-neutral; a browser backend fills ``selector`` / ``role``
    / ``name`` from the DOM, a native backend fills ``automation_id`` / ``role``
    / ``name`` from the UIA/AX tree. Each backend uses whichever fields it
    recorded; unset fields are ignored.
    """

    selector: Optional[str] = Field(
        default=None,
        description="Stable CSS/DOM selector, e.g. '#open-p1' (browser)",
    )
    role: Optional[str] = Field(
        default=None,
        description="ARIA / UIA control role, e.g. 'button', 'link'",
    )
    name: Optional[str] = Field(
        default=None,
        description="Accessible name / label / text of the target element",
    )
    automation_id: Optional[str] = Field(
        default=None,
        description="Windows UIA AutomationId (native desktop backends)",
    )


class Anchor(BaseModel):
    """Redundant evidence for locating a step's target on screen.

    Resolution ladder consumes fields in order of preference (strongest, most
    drift-tolerant first): ``structural`` (DOM / UIA element, when the backend
    supports it) -> template (local, then global) -> ocr_text -> landmarks ->
    grounder. ``structural`` is the deterministic top rung; the remaining
    (visual) rungs are the FALLBACK floor for pixel-only substrates.
    """

    template: str = Field(description="Bundle-relative path to the PNG crop")
    structural: Optional[StructuralLocator] = Field(
        default=None,
        description=(
            "STRUCTURAL locator (DOM selector / role+name, or UIA"
            " AutomationId / role+name) of the clicked target, captured at"
            " record time when the recording backend exposes it"
            " (openadapt_flow.backend.StructuralActionBackend). Drives the"
            " structural ACTION rung -- the TOP of the resolution ladder --"
            " which re-finds the SAME element deterministically (no pixel"
            " match) and is far more drift-tolerant than the visual rungs"
            " (21/21 vs 6/21 on the desktop benchmark). None on pixel-only"
            " substrates or bundles compiled before this capability; the"
            " ladder then resolves via the visual rungs below."
        ),
    )
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
    identifier_crop: Optional[str] = Field(
        default=None,
        description=(
            "Bundle-relative PNG crop of the target row's DISCRIMINATIVE"
            " IDENTIFIER cell (the MRN / name+DOB region), captured at record"
            " time on PIXEL-ONLY substrates that expose no structured text."
            " Feeds the pixel-compare and optional VLM tiers of the identity"
            " ladder (see runtime.identity): the rendered PIXELS retain the"
            " O/0 and l/1 distinction OCR collapses, so a crop-vs-crop compare"
            " catches the glyph-collapse wrong-patient where the DOM/a11y"
            " tree is unavailable. None on browser/desktop substrates (the"
            " structured tier handles those) or bundles recorded before this"
            " capability; the ladder then falls through to the OCR band tier."
        ),
    )
    identifier_region: Optional[Region] = Field(
        default=None,
        description=(
            "Location of `identifier_crop` in the recorded frame (x, y, w, h)."
            " Replay re-crops the SAME box at the resolved point (translated"
            " by the recorded region's offset from the recorded click point,"
            " exactly as the OCR band's exclude region is) so the pixel/VLM"
            " tiers compare like-for-like. Set iff `identifier_crop` is set."
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


class ApiBinding(BaseModel):
    """A declarative API/tool call that performs a step's write WITHOUT the GUI.

    The TOP of the capability ladder (RFC ``docs/design/WORKFLOW_PROGRAM_IR.md``
    section 4, the ``api`` implementation of a ``TransitionContract``): where the
    target app exposes a real API, driving its GUI to make the same write is the
    wrong tool. When a step carries an ``ApiBinding`` AND the run configures an
    :class:`~openadapt_flow.runtime.actuators.ApiActuator`, the runtime performs
    the write by CALLING the API deterministically (``$0``, zero model calls),
    confirms it with the same
    :class:`~openadapt_flow.runtime.effects.EffectVerifier` that gates a GUI
    write, and SKIPS the GUI resolution/act for that step. This is the
    ``api`` leaf of the same contract the structural rung realizes as ``dom_uia``
    and the visual ladder realizes as ``vision_rdp`` -- one semantic effect,
    backend-specific implementation.

    ADDITIVE and back-compatible: the field is optional and defaults absent, so a
    bundle carrying no binding replays EXACTLY as today (GUI actuation). A binding
    present with no actuator configured also falls through to the GUI ladder --
    the API tier is an OPTIMIZATION whose safe fallback is the GUI, never a gate
    that can block a runnable step.

    Fields are REST/JSON-first but shaped so a FHIR / MCP / tool binding fits the
    same model (``kind`` selects the substrate; a FHIR resource POST, an MCP tool
    invocation, and a plain REST write all reduce to method + endpoint + body +
    the expected effect). Placeholders ``{param}`` in the URL / query / body are
    substituted from the run's typed params (``Workflow.params`` overlaid by the
    caller's values) at actuation time.
    """

    kind: Literal["rest", "fhir", "mcp", "tool"] = Field(
        default="rest",
        description="Substrate: 'rest'/'fhir' HTTP, or an 'mcp'/'tool' call",
    )
    method: str = Field(
        default="POST",
        description="HTTP verb (REST/FHIR) or logical operation name (mcp/tool)",
    )
    url_template: str = Field(
        description=(
            "Endpoint template; absolute (http...) or relative to the"
            " actuator's base_url. `{param}` placeholders are substituted from"
            " the run's typed params."
        ),
    )
    body_template: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "JSON request body template; string leaves may carry `{param}`"
            " placeholders substituted from the run's params."
        ),
    )
    query: dict[str, str] = Field(
        default_factory=dict,
        description="Query-string params; values may carry `{param}` too",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Extra request headers; values may carry `{param}`",
    )
    expected_status: list[int] = Field(
        default_factory=list,
        description=(
            "Explicit acceptable HTTP status codes; empty means any 2xx is"
            " success (anything else is treated as an attempted write of"
            " unknown effect and HALTs -- never GUI-retried)."
        ),
    )
    timeout_s: float = Field(
        default=5.0, description="Per-request timeout in seconds"
    )
    effects: list["Effect"] = Field(
        default_factory=list,
        description=(
            "The system-of-record effect(s) this call is expected to produce."
            " Used to CONFIRM the API write via the run's EffectVerifier when"
            " the step itself declares no `effects` (an API write must be"
            " confirmable, exactly as a GUI write with declared effects is)."
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
    # System-of-record effects (RFC docs/design/WORKFLOW_PROGRAM_IR.md 2.2):
    # typed assertions verified against the REAL system of record (an API/DB
    # read), NOT the screen — closing the transactional-write gap the vision
    # `expect` postconditions above are blind to (docs/LIMITS.md "5 of 7
    # silent"). Verified by the run's configured EffectVerifier AFTER the
    # action executes; a non-CONFIRMED verdict (REFUTED / INDETERMINATE) HALTS
    # the run (see openadapt_flow.runtime.replayer and
    # docs/design/EFFECT_VERIFIER.md). Additive and back-compatible: the
    # default is empty, so a bundle carrying no effects replays exactly as
    # before, and a declared effect with no verifier configured is a
    # deployment error that HALTS (never a silent unverifiable write).
    effects: list["Effect"] = Field(default_factory=list)
    # API/tool binding (RFC section 4, the `api` implementation of the
    # transition contract): a declarative description of the API call that
    # performs THIS step's write. When present AND the run configures an
    # ApiActuator, the runtime performs the write via the API (deterministic,
    # $0, no model), confirms it with the EffectVerifier, and SKIPS the GUI
    # resolve/act for this step (see openadapt_flow.runtime.replayer). Additive
    # and back-compatible: None (default) means the step actuates through the
    # GUI resolution ladder EXACTLY as today; a binding present with no actuator
    # configured also falls through to the GUI (the API tier's safe fallback).
    api_binding: Optional[ApiBinding] = None
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

Rung = Literal[
    "structural", "template", "template_global", "ocr", "geometry", "grounder"
]


class StructuralHandle(BaseModel):
    """Result of a backend's structural locate: the resolved element's point.

    ``point`` is the element's center in the SAME coordinate space as
    :meth:`openadapt_flow.backend.Backend.click` (the pixels the resolver
    emits), so a structurally-resolved target flows through the IDENTICAL click
    path -- the pre-click identity gate and the irreversible risk gate still
    fire (structural resolution makes identity STRONGER, an exact element; it
    never bypasses it). ``confidence`` is 1.0 for a deterministic exact locate.
    """

    point: Point
    confidence: float = 1.0


class Resolution(BaseModel):
    rung: Rung
    point: Point
    confidence: float
    elapsed_ms: float


class IdentityCheck(BaseModel):
    """Outcome of the pre-click target-identity check (runtime.identity).

    Attributes:
        status: ``verified`` (band matched), ``mismatch`` (band readable and
            AFFIRMATIVELY wrong — a different entity; the run must halt, never
            click), ``abstain`` (the band is readable and its name/DOB match,
            but it rests on a GLYPH-CONFUSABLE identifier OCR may have
            collapsed — a same-name/same-DOB homonym cannot be ruled out, so
            OCR cannot honestly certify SAME *or* assert DIFFERENT; the OCR
            tier defers and the ladder HALTs if no higher-fidelity tier
            verifies — the 8th wrong-patient reopening), or ``unreadable`` (OCR
            found no usable text in the live band; identity could not be
            judged). ``abstain`` and ``unreadable`` both mean "could not
            certify": the step proceeds flagged, and irreversible steps refuse.
        mode: ``structured`` compares the recorded DOM/a11y identity text
            against the live structured text at the resolved point (the
            highest-fidelity tier -- no OCR ambiguity); ``pixel`` compares the
            recorded vs live identifier-crop PIXELS (catches the O/0 glyph
            collapse OCR discards, on stable renders); ``vlm`` is the optional
            local-VLM same/different veto for glyph-confusable identifiers
            under render drift; ``context`` compares against the recorded OCR
            band text (the pixel-substrate fallback); ``param`` re-anchors on
            the RUN's value for a parameter whose demo value was embedded in
            the recorded band.
        coverage: Matched fraction (context mode) or run/required ratio
            (param mode), diagnostic.
        expected: What the check looked for (recorded band text, or the
            run's param value on a param-mode mismatch).
        observed: Live band text the verdict was based on.
        param: The parameter that drove a param-mode check, if any.
    """

    status: Literal["verified", "mismatch", "abstain", "unreadable"]
    mode: Literal["context", "param", "structured", "pixel", "vlm"] = "context"
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
    # System-of-record effect verification (runtime.effects.EffectVerifier).
    # None when the step declared no `effects`; True when every declared
    # effect was CONFIRMED (or a duplicate was RECONCILED by compensation);
    # False when one HALTED the run (REFUTED / INDETERMINATE / escalated, or
    # effects were declared with no verifier configured). ``effect_results``
    # holds one human-readable verdict line per declared effect, for the
    # audit trail (mirrors the identity check's report surface). ZERO model
    # calls on this path — effect verification reads the system of record.
    effect_verified: Optional[bool] = None
    effect_results: list[str] = Field(default_factory=list)
    # How this step's write was PERFORMED: "api" when actuated via an
    # ApiBinding (GUI resolve/act skipped), None when it went through the GUI
    # resolution ladder (the default). Diagnostic/audit — lets an operator see
    # which steps ran on the deterministic API tier vs the visual floor.
    actuation: Optional[str] = None
    # Drift-oracle: postconditions that deterministically FAILED but were
    # confirmed by the optional on-prem VLM state-verifier under render drift
    # (recorded for audit; empty unless an appliance is configured).
    postcondition_drift_rescues: list[str] = Field(default_factory=list)
    drift_oracle_calls: int = 0  # VLM state-verifier calls this step
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


# -- forward-reference resolution --------------------------------------------
#
# Step.effects is typed ``list[Effect]`` where Effect lives in
# ``openadapt_flow.runtime.effects.effect``. That type is imported HERE, at the
# very bottom of the module, NOT at the top: importing it eagerly triggers
# ``openadapt_flow.runtime``'s package __init__, which imports the Replayer,
# which imports THIS module — so a top-level import would recurse through a
# half-initialized ``ir`` (Step/Workflow not yet defined) and fail. By the time
# this line runs every class above is fully defined, so the (import-light — no
# OCR/cv2/model deps) runtime package loads cleanly and Step's schema can be
# completed. Effect enters this module's globals so ``model_rebuild`` resolves
# the forward reference; bundles with no effects are unaffected.
from openadapt_flow.runtime.effects.effect import Effect  # noqa: E402,F401

ApiBinding.model_rebuild()
Step.model_rebuild()
Workflow.model_rebuild()

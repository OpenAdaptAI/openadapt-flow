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


class TokenTemplate(BaseModel):
    """Salted-hash + shape descriptor for one recorded identity-band token.

    Carries NO plaintext (the PHI audit's REM-2): ``c``/``r`` are salted hashes
    of the token's OCR-canonical and squashed-raw forms; the rest are
    non-identifying shape flags and a length. Enough to reproduce the
    wrong-patient guard's per-token budgets at replay
    (:mod:`openadapt_flow.runtime.identity_template`) without persisting the
    identifier itself.
    """

    c: str = Field(description="salted hash of ocr_canonical(squashed token)")
    r: str = Field(description="salted hash of squashed raw token")
    n: int = Field(description="squashed length")
    alpha: bool = False
    name: bool = False
    digit: bool = False
    idsh: bool = False
    glyph: bool = False
    gen: bool = False


class ConcatTemplate(BaseModel):
    """Precomputed SPLIT-match key (consecutive recorded tokens the live OCR
    may glue into one), since hashes cannot be concatenated at replay."""

    i: int
    size: int
    c: str
    r: str
    digit: bool
    name: bool
    n: int


class IdentityTemplate(BaseModel):
    """PHI-free stand-in for ``Anchor.context_text`` / ``structured_identity``.

    A salted-hash, shape-preserving template of the recorded identity band. It
    lets the runtime re-run the SAME wrong-patient identity check
    (:mod:`openadapt_flow.runtime.identity_template`) with no readable name /
    DOB / MRN in the artifact. NOT a cryptographic control (a salted hash of a
    low-entropy identifier is brute-forceable by a holder of the bundle + salt);
    it removes *plaintext* PHI. The at-rest control is bundle encryption
    (docs/phi_at_rest.md, deferred). Set ``OPENADAPT_FLOW_IDENTITY_SALT`` at
    compile+replay to keep the salt out of the bundle and make the hashes
    one-way to anyone without the external secret.
    """

    schema_version: int = 1
    salt: str = Field(
        default="", description="per-bundle salt (hex); empty => env salt"
    )
    band_len: int = 0
    tokens: list[TokenTemplate] = Field(default_factory=list)
    concats: list[ConcatTemplate] = Field(default_factory=list)
    structured: Optional[str] = Field(
        default=None, description="salted hash of the structured identity string"
    )
    param_token_indices: dict[str, list[int]] = Field(default_factory=dict)
    rests_on_confusable_identifier: bool = False


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
    identity_template: Optional[IdentityTemplate] = Field(
        default=None,
        description=(
            "PHI-FREE identity template (salted-hash + shape) of the recorded"
            " context band and structured identity. When present, the runtime"
            " verifies target identity from THIS (no plaintext name/DOB/MRN in"
            " the artifact — the PHI audit's REM-2) and ``context_text`` /"
            " ``structured_identity`` are None. Bundles compiled before this"
            " capability carry the plaintext fields instead and still replay"
            " unchanged (backward compatible). See"
            " openadapt_flow.runtime.identity_template."
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
    timeout_s: float = Field(default=5.0, description="Per-request timeout in seconds")
    effects: list["Effect"] = Field(
        default_factory=list,
        description=(
            "The system-of-record effect(s) this call is expected to produce."
            " Used to CONFIRM the API write via the run's EffectVerifier when"
            " the step itself declares no `effects` (an API write must be"
            " confirmable, exactly as a GUI write with declared effects is)."
        ),
    )


# -- Workflow-program IR, Phase 1 (RFC docs/design/WORKFLOW_PROGRAM_IR.md §6) --
#
# Additive, backward-compatible first step toward the parameterized workflow
# program: typed parameters, a per-step `wait_until` readiness predicate, and a
# per-step `guard` precondition. ALL optional -- a bundle that declares none of
# them loads and replays EXACTLY as a v0 linear bundle does. These fields are
# deliberately ORTHOGONAL to Step.effects / Step.risk / the Anchor identity
# rungs: they add control-flow *around* the existing hardened action leaf, they
# do not restructure it (RFC §2.1). Full branches/loops/subflows are Phase 2 --
# NOT built here.


class ParamKind(str, Enum):
    """Typed-parameter kinds (RFC §2.2 ``ParamSpec.type``).

    ``entity_ref`` names an ENTITY to be re-resolved by the identity ladder at
    run time (the "which patient" fix, docs/LIMITS.md), not a literal to blindly
    substitute; the other kinds are literal values. Phase 1 stores the type for
    typing/validation/emit -- kind-specific run-time resolution (entity_ref
    re-resolution) is Phase 2+.
    """

    STRING = "string"
    DATE = "date"
    ENUM = "enum"
    NUMBER = "number"
    ENTITY_REF = "entity_ref"


class ParamSpec(BaseModel):
    """A TYPED workflow parameter (RFC §2.2). Supersedes a bare
    ``params: dict[str, str]`` entry by carrying a type, the recorded demo
    value (``example``, which doubles as the replay default), whether it is
    required, and enum choices. Additive: ``Workflow.param_specs`` lives
    ALONGSIDE the frozen ``Workflow.params`` dict; a bundle with an empty
    ``param_specs`` behaves exactly as before.
    """

    name: str
    type: ParamKind = ParamKind.STRING
    example: Optional[str] = Field(
        default=None,
        description="Recorded demo value; also the replay default when the "
        "caller supplies no value for this parameter.",
    )
    required: bool = True
    choices: list[str] = Field(
        default_factory=list, description="Allowed values for an enum param."
    )


class PredicateKind(str, Enum):
    """Deterministic, model-free predicate kinds (RFC §2.2 ``Predicate``).

    A predicate is evaluated over the CURRENT observed frame / run parameters
    with ZERO model calls -- it is the thing a linear IR cannot express. Phase 1
    ships the concrete kinds needed to (a) subsume today's SCROLL closed loop
    (``anchor_resolves``), (b) turn the optional-modal case into a guarded
    branch (``text_present``), and (c) branch on a parameter (``param_equals``),
    plus boolean composition. ``worklist_nonempty`` (loops) is Phase 2.
    """

    #: The embedded ``anchor`` resolves on the current frame via the (model-free)
    #: resolution ladder -- today's closed-loop scroll stop condition, now a
    #: first-class predicate.
    ANCHOR_RESOLVES = "anchor_resolves"
    #: ``text`` is present on the current frame (tolerant OCR presence check).
    TEXT_PRESENT = "text_present"
    #: ``text`` is NOT present on the current frame.
    TEXT_ABSENT = "text_absent"
    #: The run's value for parameter ``param`` equals ``value`` (string compare).
    PARAM_EQUALS = "param_equals"
    AND = "and"
    OR = "or"
    NOT = "not"


class Predicate(BaseModel):
    """A deterministic condition over observed state (RFC §2.2 ``Predicate``).

    Used two ways in Phase 1: as a ``Step.wait_until`` readiness predicate (the
    replayer polls it, BOUNDED by ``timeout_s``, and HALTS on timeout -- never
    proceeds-anyway) and as the condition inside a ``Guard``. Model-free by
    construction (see ``runtime.replayer._predicate_holds``); an unknown kind
    fails safe (does not hold).
    """

    kind: PredicateKind
    anchor: Optional[Anchor] = None  # ANCHOR_RESOLVES
    text: Optional[str] = None  # TEXT_PRESENT / TEXT_ABSENT
    param: Optional[str] = None  # PARAM_EQUALS
    value: Optional[str] = None  # PARAM_EQUALS
    intent: Optional[str] = Field(
        default=None,
        description="Human-readable label (also the resolution-ladder intent "
        "for an ANCHOR_RESOLVES predicate).",
    )
    operands: list["Predicate"] = Field(
        default_factory=list, description="Sub-predicates for AND / OR / NOT."
    )
    timeout_s: float = Field(
        default=5.0,
        description="wait_until bound: how long the replayer polls this "
        "predicate before HALTing (fail-safe; never proceed-anyway).",
    )


class Guard(BaseModel):
    """A deterministic precondition on a step (RFC §2.2, Phase 1 scope).

    ``predicate`` is evaluated over the step's entry frame. When it does NOT
    hold, ``on_unmet`` decides: ``"halt"`` (the DEFAULT -- the safe direction
    for an unmet precondition, per the RFC's refuse-rather-than-guess posture)
    stops the run naming the step; ``"skip"`` makes the step a no-op success
    (the expected-but-optional case, e.g. dismissing a survey modal only when it
    appeared -- a guarded branch WITHOUT the Phase-2 state machine). Full
    multi-way branching is Phase 2.
    """

    predicate: Predicate
    on_unmet: Literal["halt", "skip"] = "halt"


Predicate.model_rebuild()  # resolve the self-referential `operands`


class Step(BaseModel):
    id: str
    intent: str = Field(description="Human-readable purpose of the step")
    action: ActionKind
    anchor: Optional[Anchor] = None  # None for pure keyboard/wait steps
    text: Optional[str] = None  # literal text for TYPE
    param: Optional[str] = None  # if set, TYPE text comes from params[param]
    secret: bool = Field(
        default=False,
        description=(
            "TYPE steps only: the parameter is a SECRET (e.g. a password)."
            " Its literal value is NEVER stored in the recording, the events"
            " log, or this bundle; at replay it is injected from the"
            " environment variable OPENADAPT_FLOW_SECRET_<PARAM> (the param"
            " name upper-cased). ``text`` is always None for a secret step,"
            " and ``param`` names the required secret. A missing secret at"
            " replay is a clear, fail-fast error (see runtime.Replayer)."
        ),
    )
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
    # Workflow-program IR, Phase 1 (RFC §6) -- both OPTIONAL and additive; a
    # step with neither replays EXACTLY as a v0 step. Orthogonal to effects /
    # risk / identity above.
    #
    # ``wait_until``: a BOUNDED readiness predicate the replayer polls BEFORE
    # acting; timeout => HALT (fail-safe, never proceed-anyway). This subsumes
    # today's SCROLL closed loop as its first concrete predicate -- a SCROLL
    # step's default readiness is "the next anchored step's anchor resolves",
    # now expressed as an ANCHOR_RESOLVES predicate (see runtime.replayer).
    wait_until: Optional[Predicate] = None
    # ``guard``: a deterministic precondition evaluated on the entry frame.
    # Unmet => HALT (default) or SKIP the step (see Guard.on_unmet).
    guard: Optional[Guard] = None
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


# -- Workflow-program IR, Phase 2 (RFC docs/design/WORKFLOW_PROGRAM_IR.md §2) --
#
# The parameterized STATE MACHINE: the control flow a linear action list cannot
# express -- LOOPS over a worklist, guarded BRANCHES, reusable SUBFLOWS, and
# EXCEPTION paths. Built ADDITIVELY on the Phase-1 pieces: a state's action IS a
# Phase-1 ``Step`` (the unchanged, hardened action leaf -- same anchor/identity/
# effect/risk machinery), a transition's guard IS a Phase-1 ``Predicate``, and a
# branch reuses the SAME model-free predicate evaluation. BACKWARD-COMPATIBLE:
# ``Workflow.program`` is OPTIONAL -- when it is None the runtime executes
# today's linear ``Workflow.steps`` loop byte-for-byte, and a linear bundle
# lifts mechanically to the degenerate single-path graph (``lift_to_program``,
# RFC §2.6). ZERO model calls at run time -- guards, branches, loops, and
# subflow dispatch are all deterministic ($0 replay).


class StateKind(str, Enum):
    """The kinds of node in a workflow-program graph (RFC §2.2)."""

    ACTION = "action"  # perform a Step (today's hardened action leaf)
    BRANCH = "branch"  # pick an outgoing transition by guard; performs no action
    LOOP = "loop"  # iterate a worklist, running a body subflow per row
    SUBFLOW_CALL = "subflow_call"  # invoke a reusable named subflow
    TERMINAL = "terminal"  # end this (sub)graph: success | halt | escalate


class Transition(BaseModel):
    """A guarded edge to a target state (RFC §2.2) -- the thing a linear IR
    cannot express.

    ``guard`` is a Phase-1 :class:`Predicate` evaluated (model-free) over the
    current frame / run params; ``None`` means UNCONDITIONAL (the RFC's ``TRUE``
    edge -- the default fall-through, and the only edge kind a degenerate linear
    program has). A state's ``transitions`` are evaluated IN ORDER; the first
    whose guard holds wins. Multiple non-``TRUE`` transitions make a multi-way
    branch.
    """

    guard: Optional[Predicate] = None
    target: str = Field(description="Id of the state this edge leads to")
    label: str = Field(default="", description="Human-readable edge label")


class Relation(BaseModel):
    """A worklist a ``loop`` state iterates over (RFC §2.3).

    Variable-length ``rows``; each row is a mapping of param name -> value that
    is bound into the run params in scope for that loop iteration. Rows may be
    INLINED here (deterministic, $0 -- the authored/compiled case) or supplied
    at run time (``Replayer.run(worklists=...)``) for a genuinely data-dependent
    queue whose length is unknown until run time. Either way iteration stays
    BOUNDED (see :class:`LoopSpec.max_iterations`).
    """

    name: str
    rows: list[dict[str, str]] = Field(
        default_factory=list,
        description="Inline worklist rows; each binds params for one iteration",
    )
    description: str = ""


class LoopSpec(BaseModel):
    """The body of a ``loop`` state (RFC §2.3; Rousillon / Helena / WebRobot).

    Binds a ``relation`` (worklist) and a ``body`` subflow that runs ONCE PER
    ROW, the row's fields merged into the run params for that iteration (so an
    ``entity_ref`` param re-resolves by the identity ladder each pass --
    iteration N acts on the RIGHT row, not a recorded pixel position). A
    zero-row worklist runs the body ZERO times. Iteration is BOUNDED by
    ``max_iterations`` -- a worklist longer than the bound HALTs (fail-safe),
    never runs unbounded.
    """

    relation: str = Field(description="Name of the Relation / worklist to loop")
    body: str = Field(description="SubflowId run once per row")
    var: str = Field(
        default="",
        description="Optional human label for the loop variable (for reports)",
    )
    max_iterations: int = Field(
        default=1000, description="Hard upper bound on iterations (fail-safe)"
    )


class State(BaseModel):
    """A node in the workflow-program graph (RFC §2.2).

    Its ``kind`` selects the payload: ``action`` carries a hardened Phase-1
    :class:`Step`; ``branch`` picks an edge purely by guard; ``loop`` iterates a
    worklist; ``subflow_call`` invokes a reusable subgraph; ``terminal`` ends the
    (sub)graph. ``transitions`` are the outgoing edges (empty on a terminal, a
    single unconditional edge on a degenerate linear node). ``on_exception``
    routes a FAILED action to a local handler instead of aborting the whole run.
    """

    id: str
    kind: StateKind
    # kind == ACTION: the hardened Phase-1 Step to perform (unchanged leaf --
    # anchor resolution, identity gate, effects, risk all ride along on it).
    step: Optional[Step] = None
    # kind == LOOP: the worklist + per-row body subflow.
    loop: Optional[LoopSpec] = None
    # kind == SUBFLOW_CALL: the reusable subflow to invoke, then continue.
    subflow: Optional[str] = None
    # Outgoing edges, evaluated IN ORDER (first matching guard wins). Empty on a
    # terminal; a single unconditional Transition on a degenerate linear node.
    transitions: list[Transition] = Field(default_factory=list)
    # Local exception handler (RFC §2.4): when this state's action FAILS (a
    # resolution / identity / postcondition / effect HALT), route to THIS state
    # instead of aborting the whole run -- the graph analog of try/except. None
    # (default) => an unhandled failure HALTs the run, exactly as today.
    on_exception: Optional[str] = None
    # kind == TERMINAL: how this (sub)graph ends. "success" completes normally
    # (returns to the caller for a subflow); "halt" / "escalate" stop the ENTIRE
    # run (success=False) -- the safe default for an underdetermined/failed path.
    outcome: Optional[Literal["success", "halt", "escalate"]] = None
    reason: str = ""


class ProgramGraph(BaseModel):
    """A directed graph of :class:`State`s with a single ``entry`` (RFC §2.2).

    Used both as the top-level program (``Workflow.program``) and as a reusable
    subflow (``Workflow.subflows[name]``, or a ``loop`` body). Walked state by
    state from ``entry`` until a terminal (or, for a subflow, until it falls off
    / reaches a ``success`` terminal, which RETURNS to the caller).
    """

    entry: str
    states: dict[str, State] = Field(default_factory=dict)


def lift_to_program(workflow: "Workflow") -> ProgramGraph:
    """Mechanically lift a linear ``Workflow`` to the degenerate straight-line
    program (RFC §2.6): each ``Step[i]`` becomes an ``action`` State with a
    single unconditional ``Transition`` to ``Step[i+1]``, and a final ``success``
    terminal. The graph interpreter over this lift replays byte-for-byte
    identically to the linear ``Replayer`` -- the proof that "a linear bundle is
    the degenerate single-path graph".
    """
    states: dict[str, State] = {}
    steps = workflow.steps
    end_id = "__end__"
    for i, step in enumerate(steps):
        sid = f"s::{step.id}"
        target = end_id if i + 1 >= len(steps) else f"s::{steps[i + 1].id}"
        states[sid] = State(
            id=sid,
            kind=StateKind.ACTION,
            step=step,
            transitions=[Transition(target=target, label="")],
        )
    states[end_id] = State(id=end_id, kind=StateKind.TERMINAL, outcome="success")
    entry = f"s::{steps[0].id}" if steps else end_id
    return ProgramGraph(entry=entry, states=states)


class Workflow(BaseModel):
    schema_version: int = 1
    name: str
    recording_id: Optional[str] = None
    # -- PHI governance manifest (PHI audit REM-1) --------------------------
    # A compiled bundle is a HIPAA-designated record; these fields let an
    # operator's compliance inventory classify it, and let the pre-commit / CI
    # guard (scripts/check_bundle_phi.py) block a bundle that still carries
    # plaintext identifiers from reaching git.
    #
    # ``contains_phi``: True when this bundle still carries a PLAINTEXT identity
    # band (``anchor.context_text`` / ``structured_identity``) — the flagship
    # PHI-at-rest leak (GAP-1a). PHI-free bundles store a salted-hash
    # ``identity_template`` instead and set this False. (It does NOT certify the
    # absence of every identifier in every free-text postcondition — that needs
    # the optional Presidio pass; see ``phi_scrubbed``.)
    contains_phi: bool = False
    # ``phi_scrubbed``: True when the optional openadapt-privacy (Presidio) pass
    # was ACTIVE on the compile path, so identifier-bearing TEXT_PRESENT
    # postconditions were dropped. False = the scrub was unavailable/off (the
    # bundle may retain identifier text in postconditions / labels).
    phi_scrubbed: bool = False
    # ``encrypted``: format-ready flag for the deferred at-rest encryption
    # (REM-1 crypto, docs/phi_at_rest.md). Always False today — a bundle is
    # plaintext-serialized JSON + PNGs, protected by the governance guards and
    # the operator's disk encryption, NOT by bundle encryption yet.
    encrypted: bool = False
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    viewport: Optional[tuple[int, int]] = None
    params: dict[str, str] = Field(
        default_factory=dict, description="param name -> example/default value"
    )
    # Workflow-program IR, Phase 1 (RFC §2.2, §6): TYPED parameter specs, ADDITIVE
    # alongside the frozen ``params`` dict above. Keyed by param name. Empty by
    # default, so a v0 bundle is unaffected; when present, the replayer folds each
    # spec's ``example`` in as a default and fails fast on a missing required one.
    param_specs: dict[str, "ParamSpec"] = Field(default_factory=dict)
    secret_params: list[str] = Field(
        default_factory=list,
        description=(
            "Names of SECRET parameters (e.g. passwords). Their values are"
            " NEVER stored here or in ``params``; each is injected at replay"
            " from OPENADAPT_FLOW_SECRET_<PARAM> (see Step.secret)."
        ),
    )
    steps: list[Step] = Field(default_factory=list)
    # Workflow-program IR, Phase 2 (RFC §2): the parameterized STATE MACHINE.
    # ALL optional and additive -- when ``program`` is None the runtime executes
    # the linear ``steps`` loop above byte-for-byte (today's behavior); a linear
    # bundle carries none of these. When ``program`` is present the runtime
    # interprets the graph (loops / branches / subflows / exception paths),
    # reusing the SAME per-action machinery (identity/effect/risk/heal gates) for
    # every ``action`` state. ``subflows`` are reusable named subgraphs (a loop
    # body or a shared component); ``data_sources`` are the worklists loops
    # iterate. See ``lift_to_program`` for the degenerate linear lift (RFC §2.6).
    program: Optional["ProgramGraph"] = None
    subflows: dict[str, "ProgramGraph"] = Field(default_factory=dict)
    data_sources: dict[str, "Relation"] = Field(default_factory=dict)

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
        return cls.model_validate(json.loads((bundle / "workflow.json").read_text()))


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
    # Workflow-program IR, Phase 1: True when a step was SKIPPED because its
    # ``guard`` was unmet with ``on_unmet="skip"`` (a no-op success -- the
    # step did not act). False for every executed step; additive.
    skipped: bool = False
    # Workflow-program IR, Phase 2: True when this (failed) action state was
    # routed to its ``State.on_exception`` handler instead of aborting the run
    # (the graph analog of a caught try/except). The result stays ``ok=False``
    # (the action DID fail) but the run continued via the handler; additive,
    # default False for every linear-mode and unhandled result.
    exception_handled: bool = False
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
    # One stable, NON-secret-bearing SHA-256 digest per verified effect, taken
    # AFTER the effect's ValueExpr contract was bound to THIS run's params
    # (P0-3). Records THAT a parameterized run verified against its own resolved
    # record/value/idempotency-key (and lets an auditor confirm two runs
    # resolved differently) without persisting the underlying value (e.g. a
    # patient identifier). Empty when the step declared no effects.
    effect_contract_hashes: list[str] = Field(default_factory=list)
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


class HaltObservation(BaseModel):
    """The structured record a HALT emits — the substrate the halt->learn loop
    consumes (``openadapt_flow.learning.halt_loop``).

    When ``Replayer.run`` stops on an unhandled state (a resolution failure, a
    dead-end branch, an unmet ``halt`` guard, a non-CONFIRMED effect, a ``halt``
    terminal), it records WHERE it stopped (``state_id`` / ``intent`` /
    ``reason``), WHAT unexpected state it observed there (``observed_texts`` — the
    on-screen text the compiled program had no branch for, PHI-scrubbed), and the
    PRE-context needed to learn a resolution (``completed_intents`` — the steps
    that succeeded before the halt). This is deliberately the SAME shape a
    :class:`~openadapt_flow.learning.trace.ExecutionTrace` carries (ordered
    intents + observed screen facts), so the learning bridge lifts it into the
    trace corpus with no reshaping — it is a report/audit field, NOT a parallel
    learning system.

    Additive and backward-compatible: ``RunReport.halt`` defaults to None, so a
    successful run (or a consumer that ignores it) is unaffected.
    """

    state_id: str = ""
    intent: str = ""
    reason: str = ""
    outcome: str = "halt"
    #: On-screen text observed at the halt point (PHI-scrubbed) — the unexpected
    #: UI state the program was not demonstrated to handle. Keyed later as the
    #: ``TEXT_PRESENT`` facts a learned branch guard tests.
    observed_texts: list[str] = Field(default_factory=list)
    #: Intents of the steps that completed successfully BEFORE the halt (the
    #: pre-context a resolution demonstration extends).
    completed_intents: list[str] = Field(default_factory=list)


class RunReport(BaseModel):
    workflow_name: str
    started_at: str
    params: dict[str, str] = Field(default_factory=dict)
    results: list[StepResult] = Field(default_factory=list)
    success: bool = False
    # Workflow-program IR, Phase 2: the outcome of the terminal state the graph
    # interpreter ended on ("success" | "halt" | "escalate"), or None for a
    # linear-mode run (no program graph) or a run that fell off the graph. The
    # ordered ``visited_states`` trace records the state ids the interpreter
    # walked (action/branch/loop/subflow/terminal), for the audit trail. Both
    # additive and empty/None on a linear run.
    terminal_outcome: Optional[str] = None
    visited_states: list[str] = Field(default_factory=list)
    # The structured HALT record (see HaltObservation): populated by
    # Replayer.run when the run stops on an unhandled state, so the halt->learn
    # loop can lift it into the trace corpus. None on a successful run (and on
    # any run whose halt path predates this field) — additive/back-compatible.
    halt: Optional["HaltObservation"] = None
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
    # Egress transparency (PHI audit REM-3): True when an egress-capable model
    # component (a paid-API or on-prem-appliance grounder / identity-VLM /
    # state-verifier) was wired for this run, so a screenshot COULD leave the
    # box. False for the default local replay (which makes zero outbound calls).
    # Wiring an egress component requires the operator's explicit opt-in
    # (Replayer(allow_model_grounding=True) / CLI --allow-model-grounding).
    screenshots_may_leave_box: bool = False

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
# Phase-2 state-machine models: State embeds Step (whose Effect forward ref is
# resolved just above), Transition embeds Predicate, ProgramGraph embeds State,
# and Workflow embeds ProgramGraph/Relation -- rebuild in dependency order so
# every forward reference is resolved before Workflow's schema is completed.
Transition.model_rebuild()
LoopSpec.model_rebuild()
State.model_rebuild()
ProgramGraph.model_rebuild()
Workflow.model_rebuild()

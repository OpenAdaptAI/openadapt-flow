"""Project a compiled :class:`~openadapt_flow.ir.Workflow` onto the shared
:class:`~openadapt_flow.visualize.spec.ProgramGraphSpec`.

This is the ONLY place the bundle IR is read for visualization; every surface
renders the emitted spec. The projection is intentionally lossy-but-honest: it
surfaces the load-bearing compiled structure (target resolution ladder,
identity gate, effect check, verification postconditions, risk class, control
guards, and the resulting HALT points) and rolls the rest into short human
summaries. It never fabricates structure that the compiler did not produce.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from openadapt_flow.visualize.spec import (
    BundleMeta,
    EdgeKind,
    EffectInfo,
    GraphEdge,
    GraphNode,
    IdentityInfo,
    NodeKind,
    ParamInfo,
    ProgramGraphSpec,
    ProvenanceInfo,
    ResolutionInfo,
    ResolutionRung,
)

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.ir import (
        Anchor,
        Predicate,
        State,
        Step,
        Workflow,
    )
    from openadapt_flow.runtime.effects.effect import Effect


# Human labels for the resolution ladder, strongest-first. ``api`` sits ABOVE
# the GUI ladder (a declarative write with no pixel/DOM resolution at all).
_RUNG_LABELS: list[tuple[str, str]] = [
    ("api", "API / tool call"),
    ("structural", "DOM / accessibility selector"),
    ("template", "Image template match"),
    ("ocr", "OCR text match"),
    ("landmarks", "Nearby-landmark geometry"),
]


def _predicate_summary(pred: Optional["Predicate"]) -> Optional[str]:
    """A short human string for a Phase-1 predicate (guard / wait_until)."""
    if pred is None:
        return None
    if pred.intent:
        return pred.intent
    kind = pred.kind.value
    if kind in ("text_present", "text_absent") and pred.text:
        return f"{kind}: {pred.text!r}"
    if kind == "param_equals" and pred.param is not None:
        return f"param {pred.param} == {pred.value!r}"
    if kind in ("and", "or", "not"):
        inner = ", ".join(
            s for s in (_predicate_summary(o) for o in pred.operands) if s
        )
        return f"{kind}({inner})"
    return kind


def _resolution_info(
    anchor: Optional["Anchor"], step: "Step"
) -> Optional[ResolutionInfo]:
    """Build the target-resolution ladder for an action node."""
    has_api = step.api_binding is not None
    if anchor is None and not has_api:
        return None
    rungs: list[ResolutionRung] = []
    top: Optional[str] = None
    for name, label in _RUNG_LABELS:
        present = False
        detail = ""
        if name == "api":
            present = has_api
            if present and step.api_binding is not None:
                detail = f"{step.api_binding.method} {step.api_binding.url_template}"
        elif anchor is not None:
            if name == "structural":
                s = anchor.structural
                present = s is not None
                if present and s is not None:
                    detail = (
                        s.selector
                        or s.automation_id
                        or (f"{s.role or ''} {s.name or ''}".strip())
                    )
            elif name == "template":
                present = bool(anchor.template)
                detail = anchor.template if present else ""
            elif name == "ocr":
                present = bool(anchor.ocr_text)
                detail = anchor.ocr_text or ""
            elif name == "landmarks":
                present = bool(anchor.landmarks)
                if present:
                    detail = f"{len(anchor.landmarks)} landmark(s)"
        rungs.append(
            ResolutionRung(
                name=name, label=label, present=present, detail=detail.strip()
            )
        )
        if present and top is None:
            top = name
    return ResolutionInfo(rungs=rungs, top_rung=top)


def _identity_info(step: "Step") -> IdentityInfo:
    """Build the identity-gate summary for an action node."""
    anchor = step.anchor
    applicable = step.identity_armed is not None
    phi_free = False
    has_structured = False
    has_identifier_crop = False
    if anchor is not None:
        phi_free = anchor.identity_template is not None
        has_structured = anchor.structured_identity is not None or (
            anchor.identity_template is not None
            and anchor.identity_template.structured is not None
        )
        has_identifier_crop = anchor.identifier_crop is not None
    return IdentityInfo(
        applicable=applicable,
        armed=step.identity_armed,
        reason=step.identity_unarmed_reason,
        phi_free=phi_free,
        has_structured=has_structured,
        has_identifier_crop=has_identifier_crop,
    )


def _effect_info(effect: "Effect") -> EffectInfo:
    """Build the effect-check summary for one declared effect."""
    kind = effect.kind.value
    if effect.needs_operator_confirmation:
        summary = (
            "consequential write; system-of-record binding not derivable from the demo"
        )
    elif kind == "field_equals":
        field = effect.field or "?"
        val = effect.value
        val_s = str(val) if val is not None else "?"
        summary = f"record field {field} == {val_s}"
    else:  # record_written
        sel = (
            ", ".join(f"{k}={v}" for k, v in effect.match.items()) or "matching record"
        )
        count = effect.expected_count
        summary = f"exactly {count} record(s) where {sel}"
    return EffectInfo(
        kind=kind,
        summary=summary,
        risk=effect.risk,
        needs_operator_confirmation=effect.needs_operator_confirmation,
    )


def _action_node(step: "Step", index: int, node_id: str, kind: NodeKind) -> GraphNode:
    """Project a compiled :class:`Step` onto an ``action`` graph node, computing
    its resolution ladder, identity gate, effects, verification, and the set of
    HALT points it introduces."""
    resolution = _resolution_info(step.anchor, step)
    identity = _identity_info(step)
    effects = [_effect_info(e) for e in step.effects]
    postconditions = [pc.kind.value for pc in step.expect]

    guard_summary: Optional[str] = None
    guard_on_unmet: Optional[str] = None
    if step.guard is not None:
        guard_summary = _predicate_summary(step.guard.predicate)
        guard_on_unmet = step.guard.on_unmet
    wait_summary = _predicate_summary(step.wait_until)

    api_summary: Optional[str] = None
    if step.api_binding is not None:
        api_summary = f"{step.api_binding.method} {step.api_binding.url_template}"

    # -- HALT points (fail-safe stop conditions this node introduces) --
    halts: list[str] = []
    if effects:
        if any(e.needs_operator_confirmation for e in effects):
            halts.append("halts until an operator binds the placeholder effect")
        else:
            halts.append("halts if the system-of-record effect is not confirmed")
    if step.guard is not None and guard_on_unmet == "halt":
        halts.append(f"halts if precondition unmet ({guard_summary})")
    if step.wait_until is not None:
        halts.append(f"halts if not ready in time ({wait_summary})")
    if step.risk == "irreversible" and identity.applicable and identity.armed is False:
        halts.append("irreversible write WITHOUT an armed identity gate")

    # -- badges (compact capability / risk chips) --
    badges: list[str] = []
    if step.risk == "irreversible":
        badges.append("irreversible")
    if identity.armed is True:
        badges.append("identity gate")
    elif identity.applicable and identity.armed is False:
        badges.append("no identity gate")
    if effects:
        badges.append("effect check")
    if step.api_binding is not None:
        badges.append("API")
    if step.secret:
        badges.append("secret")
    if step.guard is not None and guard_on_unmet == "skip":
        badges.append("optional (skippable)")

    return GraphNode(
        id=node_id,
        index=index,
        kind=kind,
        title=step.intent,
        action=step.action.value,
        risk=step.risk,
        param=step.param,
        secret=step.secret,
        key=step.key,
        resolution=resolution,
        identity=identity,
        effects=effects,
        has_api_binding=step.api_binding is not None,
        api_summary=api_summary,
        postconditions=postconditions,
        guard=guard_summary,
        guard_on_unmet=guard_on_unmet,
        wait_until=wait_summary,
        halts=halts,
        badges=badges,
    )


def _bundle_meta(
    workflow: "Workflow", nodes: list[GraphNode], is_program: bool
) -> BundleMeta:
    """Roll up the bundle-level header from the workflow + projected nodes."""
    params: list[ParamInfo] = []
    seen: set[str] = set()
    for name, spec in (workflow.param_specs or {}).items():
        params.append(
            ParamInfo(
                name=name,
                type=spec.type.value,
                required=spec.required,
                secret=name in (workflow.secret_params or []),
                example=spec.example,
                choices=list(spec.choices),
            )
        )
        seen.add(name)
    for name, example in (workflow.params or {}).items():
        if name in seen:
            continue
        params.append(
            ParamInfo(
                name=name,
                secret=name in (workflow.secret_params or []),
                example=example,
            )
        )
        seen.add(name)
    for name in workflow.secret_params or []:
        if name not in seen:
            params.append(ParamInfo(name=name, secret=True, example=None))
            seen.add(name)

    prov = ProvenanceInfo()
    manifest = workflow.manifest
    if manifest is not None:
        prov = ProvenanceInfo(
            compiler_version=manifest.provenance.compiler_version,
            certified=manifest.provenance.certified,
            certification_status=manifest.provenance.certification_status,
            policy_name=manifest.provenance.policy_name,
            expires_at=manifest.provenance.expires_at,
            content_digest=manifest.content_digest or None,
            source_recording_sha256=manifest.provenance.source_recording_sha256,
        )

    action_nodes = [n for n in nodes if n.kind == NodeKind.ACTION]
    return BundleMeta(
        name=workflow.name,
        schema_version=workflow.schema_version,
        created_at=workflow.created_at,
        viewport=workflow.viewport,
        is_program=is_program,
        contains_phi=workflow.contains_phi,
        phi_scrubbed=workflow.phi_scrubbed,
        encrypted=workflow.encrypted,
        step_count=len(workflow.steps),
        action_count=len(action_nodes),
        irreversible_count=sum(1 for n in action_nodes if n.risk == "irreversible"),
        identity_armed_count=sum(
            1 for n in action_nodes if n.identity and n.identity.armed is True
        ),
        identity_unarmed_count=sum(
            1
            for n in action_nodes
            if n.identity and n.identity.applicable and n.identity.armed is False
        ),
        effect_count=sum(len(n.effects) for n in action_nodes),
        api_binding_count=sum(1 for n in action_nodes if n.has_api_binding),
        halt_point_count=sum(len(n.halts) for n in nodes),
        params=params,
        provenance=prov,
    )


def _build_linear(workflow: "Workflow") -> tuple[list[GraphNode], list[GraphEdge]]:
    """Project a linear ``Workflow.steps`` list to a straight chain of action
    nodes ending in a ``success`` terminal (the common case today)."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    steps = workflow.steps
    for i, step in enumerate(steps):
        nodes.append(_action_node(step, i, step.id, NodeKind.ACTION))
    end_id = "__end__"
    for i, step in enumerate(steps):
        target = end_id if i + 1 >= len(steps) else steps[i + 1].id
        edges.append(GraphEdge(source=step.id, target=target, kind=EdgeKind.SEQUENCE))
    nodes.append(
        GraphNode(
            id=end_id,
            index=len(steps),
            kind=NodeKind.TERMINAL,
            title="Success",
            outcome="success",
        )
    )
    return nodes, edges


def _build_program(workflow: "Workflow") -> tuple[list[GraphNode], list[GraphEdge]]:
    """Project a Phase-2 ``Workflow.program`` graph to spec nodes/edges,
    preserving branches, loops, subflow calls, exception handlers, and
    terminals so richer compiled structure renders without a spec break."""
    from openadapt_flow.ir import StateKind

    program = workflow.program
    assert program is not None
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    # Deterministic order: entry first, then the rest by id.
    ordered_ids = [program.entry] + sorted(
        sid for sid in program.states if sid != program.entry
    )
    index_by_id = {sid: i for i, sid in enumerate(ordered_ids)}
    for sid in ordered_ids:
        state: "State" = program.states[sid]
        idx = index_by_id[sid]
        if state.kind == StateKind.ACTION and state.step is not None:
            node = _action_node(state.step, idx, sid, NodeKind.ACTION)
        else:
            node = GraphNode(
                id=sid,
                index=idx,
                kind=NodeKind(state.kind.value),
                title=_state_title(state),
                outcome=state.outcome,
                reason=state.reason,
            )
            if state.kind == StateKind.TERMINAL and state.outcome in (
                "halt",
                "escalate",
            ):
                node.halts.append(f"terminal: {state.outcome}")
                node.badges.append(state.outcome)
        # exception handler edge
        if state.on_exception:
            edges.append(
                GraphEdge(
                    source=sid,
                    target=state.on_exception,
                    kind=EdgeKind.EXCEPTION,
                    label="on failure",
                )
            )
        # loop body edge
        if state.kind == StateKind.LOOP and state.loop is not None:
            node.badges.append("loop")
            edges.append(
                GraphEdge(
                    source=sid,
                    target=state.loop.body,
                    kind=EdgeKind.LOOP_BODY,
                    label=f"per row of {state.loop.relation}",
                )
            )
        # transitions
        for tr in state.transitions:
            guard_summary = _predicate_summary(tr.guard)
            edges.append(
                GraphEdge(
                    source=sid,
                    target=tr.target,
                    kind=EdgeKind.BRANCH if tr.guard is not None else EdgeKind.SEQUENCE,
                    label=tr.label or (guard_summary or ""),
                    guard=guard_summary,
                )
            )
        nodes.append(node)
    return nodes, edges


def _state_title(state: "State") -> str:
    from openadapt_flow.ir import StateKind

    if state.kind == StateKind.TERMINAL:
        return {"success": "Success", "halt": "Halt", "escalate": "Escalate"}.get(
            state.outcome or "", "End"
        )
    if state.kind == StateKind.BRANCH:
        return "Branch"
    if state.kind == StateKind.LOOP and state.loop is not None:
        return f"Loop over {state.loop.relation}"
    if state.kind == StateKind.SUBFLOW_CALL and state.subflow:
        return f"Call subflow {state.subflow}"
    return state.id


def build_program_graph(workflow: "Workflow") -> ProgramGraphSpec:
    """Emit the shared :class:`ProgramGraphSpec` for a compiled ``workflow``.

    A linear bundle (``workflow.program is None``) projects to a straight chain
    of ``action`` nodes; a Phase-2 program projects its full state graph. Either
    way the emitted spec is the single artifact every visualization surface
    renders.
    """
    is_program = workflow.program is not None
    if is_program:
        nodes, edges = _build_program(workflow)
    else:
        nodes, edges = _build_linear(workflow)
    meta = _bundle_meta(workflow, nodes, is_program)
    return ProgramGraphSpec(bundle=meta, nodes=nodes, edges=edges)

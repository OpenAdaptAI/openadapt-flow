"""Audience-bound projections of the compiled program graph.

The operator-local graph is the complete diagnostic view. Other surfaces get a
closed projection that removes recorded text, parameter values, selectors,
URLs, free-text predicates, and local provenance. Projection does not sanitize
the source bundle and never changes its governance flags.
"""

from __future__ import annotations

from enum import Enum

from openadapt_flow.visualize.spec import GraphNode, NodeKind, ProgramGraphSpec


class PresentationProfile(str, Enum):
    """Data boundary for one rendered program graph."""

    OPERATOR_LOCAL = "operator-local"
    REMOTE_SAFE = "remote-safe"
    PUBLIC_SYNTHETIC = "public-synthetic"
    SANITIZED_DERIVATIVE = "sanitized-derivative"


def _safe_title(node: GraphNode) -> str:
    if node.kind == NodeKind.TERMINAL:
        return (
            "End of declared steps"
            if (node.outcome or "").lower() == "success"
            else "Stopped for review"
        )
    if node.kind == NodeKind.BRANCH:
        return "Evaluate a declared condition"
    if node.kind == NodeKind.BUSINESS_DECISION:
        return "Request an authorized decision"
    if node.kind == NodeKind.LOOP:
        return "Repeat the bounded steps"
    if node.kind == NodeKind.SUBFLOW_CALL:
        return "Run an approved subflow"
    action = (node.action or "").lower()
    if action in {"click", "double_click"}:
        return "Select an interface target"
    if action == "type":
        if node.secret:
            return "Enter an approved secret"
        if node.param:
            return "Enter an approved input"
        return "Enter an approved value"
    if action == "key":
        return "Send an approved key"
    if action == "scroll":
        return "Move through the current view"
    if action in {"wait", "sleep"}:
        return "Wait for the declared state"
    if node.has_api_binding:
        return "Run an approved API action"
    return "Run an approved action"


def _safe_halts(node: GraphNode) -> list[str]:
    if not node.halts:
        return []
    return [f"declared stop rule {index + 1}" for index in range(len(node.halts))]


def project_program_graph(
    spec: ProgramGraphSpec,
    profile: PresentationProfile | str,
) -> ProgramGraphSpec:
    """Return the exact graph structure for the requested data boundary.

    A non-local projection retains node and edge identities. It removes fields
    whose values can contain recorded application data. It also removes local
    provenance. The function does not assert that the source is PHI-free.
    """

    selected = PresentationProfile(profile)
    if selected == PresentationProfile.OPERATOR_LOCAL:
        return spec.model_copy(deep=True)

    projected = spec.model_copy(deep=True)
    projected.bundle.name = "Compiled program"
    projected.bundle.created_at = None
    projected.bundle.viewport = None
    projected.bundle.params = [
        param.model_copy(update={"name": f"input_{index + 1}", "example": None, "choices": []})
        for index, param in enumerate(projected.bundle.params)
    ]
    projected.bundle.provenance.content_digest = None
    projected.bundle.provenance.source_recording_sha256 = None
    projected.bundle.provenance.policy_name = None

    for node in projected.nodes:
        node.title = _safe_title(node)
        node.param = None
        node.key = None
        node.api_summary = None
        node.guard = None
        node.wait_until = None
        node.reason = ""
        node.halts = _safe_halts(node)
        if node.resolution is not None:
            for rung in node.resolution.rungs:
                rung.detail = ""
        if node.identity is not None:
            node.identity.reason = None
        for effect in node.effects:
            effect.summary = "Independent effect contract"

    for edge in projected.edges:
        edge.guard = None
        edge.label = {
            "sequence": "",
            "branch": "declared branch",
            "exception": "declared exception",
            "loop_body": "declared loop",
        }[edge.kind.value]

    return projected

"""Policy schema, certifier, and linter — turn coverage DISCLOSURE into
pre-deploy ENFORCEMENT.

The compiler and run reports already *disclose* weak coverage (unarmed clicks,
vacuous postconditions, opt-in risk). "Compiled successfully" is not the same
as "safe to run unattended": a broad study compiled 29/29 bundles but only 17
replayed safely. This module makes ``runnable`` distinct from ``certified
safe``:

- :func:`lint_workflow` reports a bundle's coverage GAPS (unarmed clicks,
  vacuous postconditions, under-classified risk) with a severity each — advice,
  policy-independent.
- :class:`Policy` + :func:`evaluate_policy` ENFORCE a named policy: a compiled
  bundle either passes or is REFUSED before it ever runs, with a structured
  report naming every violating step and why.

This is a compile-time / pre-deploy layer. It does NOT touch the replayer's
runtime behaviour or the identity/heal logic (``docs/LIMITS.md`` still governs
what happens once a certified bundle runs).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.risk import classify_step_risk, step_text

# Action kinds that a pre-click / pre-type identity check applies to — kept in
# lockstep with Replayer._record_identity_coverage (anchored click/type).
_IDENTITY_ACTIONS = (ActionKind.CLICK, ActionKind.DOUBLE_CLICK, ActionKind.TYPE)
_CLICK_ACTIONS = (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
# Kinds expected to produce an observable effect worth asserting. SCROLL is
# excluded by design (the compiler mines no postconditions for it — its effect
# is verified by the next step's resolution); WAIT asserts nothing either.
_EFFECT_ACTIONS = (
    ActionKind.CLICK,
    ActionKind.DOUBLE_CLICK,
    ActionKind.TYPE,
    ActionKind.KEY,
)


# ---------------------------------------------------------------------------
# Per-step analysis primitives (shared by the linter and the certifier)
# ---------------------------------------------------------------------------


def is_identity_applicable(step: Step) -> bool:
    """True for anchored click / double-click / TYPE steps — the steps a
    pre-click identity check can guard (mirrors the replayer)."""
    return step.anchor is not None and step.action in _IDENTITY_ACTIONS


def is_identity_armed(step: Step) -> bool:
    """True when the step's pre-click identity check will actually run.

    Prefers the compiler-written ``identity_armed`` audit flag; falls back to
    the ground truth the gate itself keys on (``context_text`` or
    ``structured_identity`` present) for bundles compiled before that flag
    existed.
    """
    if step.identity_armed is not None:
        return step.identity_armed
    a = step.anchor
    return a is not None and bool(a.context_text or a.structured_identity)


def expects_effect(step: Step) -> bool:
    """True for steps that should carry an effect assertion (see
    ``_EFFECT_ACTIONS``)."""
    return step.action in _EFFECT_ACTIONS


def is_vacuous(step: Step) -> bool:
    """True when an effect-expecting step asserts NOTHING (empty ``expect``) —
    it will pass vacuously at replay (``docs/LIMITS.md``)."""
    return expects_effect(step) and not step.expect


def step_confidence(step: Step) -> float:
    """A compile-time confidence PROXY in ``[0, 1]`` (not a runtime resolution
    confidence — that is only known during replay).

    Deterministic, non-anchored actuations (key, scroll, un-anchored type)
    replay exactly and score ``1.0``. An anchored step scores by how much
    redundant evidence it carries for locating and verifying its target:
    ``+0.5`` a template crop, ``+0.3`` an OCR label, ``+0.2`` an armed identity
    band — so a fully-evidenced click reaches ``1.0`` and a template-only click
    (no label, no identity) sits at ``0.5``. Used only by the
    ``require_human_approval_below_confidence`` rule to flag thinly-evidenced
    steps for human sign-off before unattended deployment.
    """
    a = step.anchor
    if a is None:
        return 1.0
    score = 0.0
    if a.template:
        score += 0.5
    if a.ocr_text:
        score += 0.3
    if a.context_text or a.structured_identity:
        score += 0.2
    return min(1.0, score)


def step_tags(step: Step) -> set[str]:
    """Semantic tags a policy's ``require_*`` lists can match against (as an
    alternative to raw keywords).

    - ``click`` / ``type`` / ``key`` / ``scroll`` — the action kind.
    - ``irreversible`` / ``reversible`` — the compiled risk; ``write`` is an
      alias of ``irreversible`` (write-shaped consequential action).
    - ``identity_applicable`` — a pre-click identity check applies.
    - ``navigation`` — a benign (reversible) click.
    - ``entity_navigation`` — a benign click that lands on a specific on-screen
      entity/row (identity-applicable): the wrong-entity surface, and the
      practical stand-in for "repeated-structure / entity-navigation steps".
    """
    tags: set[str] = set()
    act = step.action
    if act in _CLICK_ACTIONS:
        tags.add("click")
    elif act is ActionKind.TYPE:
        tags.add("type")
    elif act is ActionKind.KEY:
        tags.add("key")
    elif act is ActionKind.SCROLL:
        tags.add("scroll")
    if step.risk == "irreversible":
        tags.update(("irreversible", "write"))
    else:
        tags.add("reversible")
    if is_identity_applicable(step):
        tags.add("identity_applicable")
    if act in _CLICK_ACTIONS and step.risk == "reversible":
        tags.add("navigation")
        if is_identity_applicable(step):
            tags.add("entity_navigation")
    return tags


def _matches_token(step: Step, token: str) -> bool:
    """True if ``token`` matches ``step`` — either as a semantic tag
    (:func:`step_tags`) or as a word-boundary keyword in the step's text."""
    token = token.strip()
    if not token:
        return False
    if token.lower() in step_tags(step):
        return True
    return (
        re.search(rf"\b{re.escape(token)}\b", step_text(step), re.IGNORECASE)
        is not None
    )


def step_matches_any(step: Step, tokens: list[str]) -> bool:
    return any(_matches_token(step, t) for t in tokens)


# ---------------------------------------------------------------------------
# Policy schema
# ---------------------------------------------------------------------------


class Policy(BaseModel):
    """A pre-deploy safety policy. Every rule is OPT-IN (a bare policy asserts
    nothing); ``extra="forbid"`` so a mistyped rule key fails loudly rather
    than silently doing nothing — a safety tool must not no-op on a typo.

    Rules:
        prohibit_unarmed_clicks: Fail on any identity-applicable step whose
            pre-click identity check is not armed (would click with NO identity
            verification).
        prohibit_vacuous_postconditions: Fail on any effect-expecting step that
            asserts nothing (empty ``expect`` — a vacuous pass at replay).
        require_identity_for: Every step matching one of these tokens (a
            :func:`step_tags` tag such as ``entity_navigation`` / ``write``, or
            a keyword) MUST be identity-armed.
        require_effect_verification_for: Every step matching one of these
            tokens (e.g. ``save``, ``submit``, ``create``, or the ``write``
            tag) MUST carry at least one postcondition. Names the requirement
            even though today's only effect verifier is the vision
            postconditions (system-of-record verification is future work; see
            ``docs/LIMITS.md``).
        max_unverified_steps: Maximum number of vacuous (effect-expecting,
            no-postcondition) steps allowed. ``None`` = unlimited.
        require_human_approval_below_confidence: Any step whose compile-time
            confidence proxy (:func:`step_confidence`) is below this threshold
            must be signed off by a human before unattended deployment — the
            bundle is REFUSED until then.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = "unnamed-policy"
    description: str = ""

    prohibit_unarmed_clicks: bool = False
    prohibit_vacuous_postconditions: bool = False
    require_identity_for: list[str] = Field(default_factory=list)
    require_effect_verification_for: list[str] = Field(default_factory=list)
    max_unverified_steps: Optional[int] = None
    require_human_approval_below_confidence: Optional[float] = None


# Built-in example policies live beside this module in ``policies/``.
_BUILTIN_DIR = Path(__file__).parent / "policies"


def builtin_policy_names() -> list[str]:
    """Names (stems) of the shipped example policies."""
    if not _BUILTIN_DIR.is_dir():
        return []
    return sorted(p.stem for p in _BUILTIN_DIR.glob("*.yaml"))


def load_policy(source: str | Path) -> Policy:
    """Load a :class:`Policy` from a YAML file path, or by built-in name.

    ``source`` is resolved as: an existing file path first, else a shipped
    built-in name (with or without ``.yaml``; see :func:`builtin_policy_names`).

    Raises:
        FileNotFoundError: If neither a file nor a built-in matches.
        ValueError: If the YAML is malformed or violates the schema (an
            unknown rule key, a wrong type).
    """
    import yaml

    path = Path(source)
    if not path.is_file():
        name = str(source)
        candidate = _BUILTIN_DIR / (name if name.endswith(".yaml") else f"{name}.yaml")
        if candidate.is_file():
            path = candidate
        else:
            raise FileNotFoundError(
                f"policy {source!r} is neither an existing file nor a built-in "
                f"policy (built-ins: {', '.join(builtin_policy_names()) or 'none'})"
            )
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:  # pragma: no cover - passthrough
        raise ValueError(f"could not parse policy YAML {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"policy {path} must be a YAML mapping, got {type(data).__name__}")
    try:
        return Policy.model_validate(data)
    except Exception as e:
        raise ValueError(f"invalid policy {path}: {e}") from e


# ---------------------------------------------------------------------------
# Certifier
# ---------------------------------------------------------------------------


class Violation(BaseModel):
    """A single policy violation."""

    rule: str
    step_id: Optional[str] = None
    reason: str


class CertifyReport(BaseModel):
    """Structured pass/fail outcome of certifying a bundle against a policy."""

    policy_name: str
    workflow_name: str
    passed: bool
    n_steps: int
    violations: list[Violation] = Field(default_factory=list)

    def render(self) -> str:
        """Human-readable multi-line report."""
        head = (
            f"{'PASS' if self.passed else 'FAIL'}: "
            f"workflow {self.workflow_name!r} vs policy {self.policy_name!r} "
            f"({self.n_steps} steps)"
        )
        if self.passed:
            return head + "\n  no violations — certified safe under this policy."
        lines = [head, f"  {len(self.violations)} violation(s):"]
        for v in self.violations:
            where = f"[{v.step_id}] " if v.step_id else ""
            lines.append(f"  - ({v.rule}) {where}{v.reason}")
        return "\n".join(lines)


def evaluate_policy(workflow: Workflow, policy: Policy) -> CertifyReport:
    """Certify ``workflow`` against ``policy`` → a structured pass/fail report.

    Pure function of the compiled bundle and the policy; runs nothing. A report
    with an empty ``violations`` list (``passed=True``) means the bundle is
    certified safe UNDER THIS POLICY — not that it is safe in the absolute
    (``docs/LIMITS.md`` still governs the residual runtime risks).
    """
    violations: list[Violation] = []

    for step in workflow.steps:
        if policy.prohibit_unarmed_clicks and is_identity_applicable(step):
            if not is_identity_armed(step):
                violations.append(
                    Violation(
                        rule="prohibit_unarmed_clicks",
                        step_id=step.id,
                        reason=(
                            "identity-applicable step is UNARMED — would act "
                            "with no identity verification"
                            + (
                                f" ({step.identity_unarmed_reason})"
                                if step.identity_unarmed_reason
                                else ""
                            )
                        ),
                    )
                )

        if policy.prohibit_vacuous_postconditions and is_vacuous(step):
            violations.append(
                Violation(
                    rule="prohibit_vacuous_postconditions",
                    step_id=step.id,
                    reason=(
                        f"{step.action.value} step asserts no postcondition — "
                        "passes vacuously at replay"
                    ),
                )
            )

        if policy.require_identity_for and step_matches_any(
            step, policy.require_identity_for
        ):
            if not (is_identity_applicable(step) and is_identity_armed(step)):
                violations.append(
                    Violation(
                        rule="require_identity_for",
                        step_id=step.id,
                        reason=(
                            "step matches require_identity_for but is not "
                            "identity-armed (no verified target identity "
                            "before it acts)"
                        ),
                    )
                )

        if policy.require_effect_verification_for and step_matches_any(
            step, policy.require_effect_verification_for
        ):
            if not step.expect:
                violations.append(
                    Violation(
                        rule="require_effect_verification_for",
                        step_id=step.id,
                        reason=(
                            "step matches require_effect_verification_for but "
                            "carries no postcondition to verify its effect"
                        ),
                    )
                )

        thr = policy.require_human_approval_below_confidence
        if thr is not None:
            conf = step_confidence(step)
            if conf < thr:
                violations.append(
                    Violation(
                        rule="require_human_approval_below_confidence",
                        step_id=step.id,
                        reason=(
                            f"compile-time confidence {conf:.2f} < {thr:.2f} — "
                            "requires human approval before unattended deployment"
                        ),
                    )
                )

    if policy.max_unverified_steps is not None:
        vacuous = [s.id for s in workflow.steps if is_vacuous(s)]
        if len(vacuous) > policy.max_unverified_steps:
            violations.append(
                Violation(
                    rule="max_unverified_steps",
                    step_id=None,
                    reason=(
                        f"{len(vacuous)} vacuous (no-postcondition) steps exceed "
                        f"the limit of {policy.max_unverified_steps}: "
                        f"{', '.join(vacuous)}"
                    ),
                )
            )

    return CertifyReport(
        policy_name=policy.name,
        workflow_name=workflow.name,
        passed=not violations,
        n_steps=len(workflow.steps),
        violations=violations,
    )


# ---------------------------------------------------------------------------
# Linter
# ---------------------------------------------------------------------------

SEVERITY_ORDER = {"info": 0, "warn": 1, "error": 2}


class Finding(BaseModel):
    """A single lint finding (policy-independent coverage advice)."""

    severity: str  # "info" | "warn" | "error"
    code: str
    step_id: Optional[str] = None
    message: str


class LintReport(BaseModel):
    """Coverage-gap report for a bundle."""

    workflow_name: str
    n_steps: int
    findings: list[Finding] = Field(default_factory=list)

    @property
    def max_severity(self) -> str:
        if not self.findings:
            return "info"
        return max(self.findings, key=lambda f: SEVERITY_ORDER[f.severity]).severity

    def counts(self) -> dict[str, int]:
        out = {"info": 0, "warn": 0, "error": 0}
        for f in self.findings:
            out[f.severity] += 1
        return out

    def render(self) -> str:
        """Human-readable report; findings ordered most-severe first."""
        c = self.counts()
        head = (
            f"lint {self.workflow_name!r} ({self.n_steps} steps): "
            f"{c['error']} error, {c['warn']} warn, {c['info']} info"
        )
        icon = {"error": "✗", "warn": "!", "info": "·"}
        ordered = sorted(
            self.findings,
            key=lambda f: (-SEVERITY_ORDER[f.severity], f.step_id or ""),
        )
        lines = [head]
        if not ordered:
            lines.append("  no coverage gaps found.")
        for f in ordered:
            where = f"[{f.step_id}] " if f.step_id else ""
            lines.append(f"  {icon[f.severity]} {f.severity:5} ({f.code}) {where}{f.message}")
        return "\n".join(lines)


def lint_workflow(workflow: Workflow) -> LintReport:
    """Report a compiled bundle's coverage GAPS with a severity each.

    Policy-independent. Findings (severity depends on the step's risk — a gap on
    a consequential/irreversible step is an ``error``, the same gap on a benign
    step a ``warn``):

    - ``unarmed_click`` — an identity-applicable step that clicks with no
      identity verification.
    - ``vacuous_postcondition`` — an effect-expecting step that asserts nothing.
    - ``under_classified_risk`` — a step whose text looks write-shaped but that
      compiled ``reversible`` (typically a bundle predating auto risk-
      classification; recompile or set ``risk_overrides``). Always ``warn``.
    """
    findings: list[Finding] = []
    for step in workflow.steps:
        irreversible = step.risk == "irreversible"

        if is_identity_applicable(step) and not is_identity_armed(step):
            reason = step.identity_unarmed_reason or (
                "no identity context recorded at compile time"
            )
            findings.append(
                Finding(
                    severity="error" if irreversible else "warn",
                    code="unarmed_click",
                    step_id=step.id,
                    message=(
                        f"{step.action.value} proceeds with NO identity check "
                        f"({reason})"
                        + (" — and it is an IRREVERSIBLE write" if irreversible else "")
                    ),
                )
            )

        if is_vacuous(step):
            findings.append(
                Finding(
                    severity="error" if irreversible else "warn",
                    code="vacuous_postcondition",
                    step_id=step.id,
                    message=(
                        f"{step.action.value} step asserts no postcondition — "
                        "passes vacuously at replay"
                        + (" (IRREVERSIBLE write)" if irreversible else "")
                    ),
                )
            )

        if not irreversible and classify_step_risk(step) == "irreversible":
            findings.append(
                Finding(
                    severity="warn",
                    code="under_classified_risk",
                    step_id=step.id,
                    message=(
                        "text looks write-shaped but the step is marked "
                        "reversible (recompile to auto-classify, or set "
                        "risk_overrides)"
                    ),
                )
            )

    return LintReport(
        workflow_name=workflow.name,
        n_steps=len(workflow.steps),
        findings=findings,
    )

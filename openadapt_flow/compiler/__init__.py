"""Compiler: recording directory -> workflow bundle."""

from openadapt_flow.compiler.annotate import (
    AnnotationResult,
    AnthropicStepAnnotator,
    FakeStepAnnotator,
    StepAnnotator,
    WorkflowProposals,
    apply_annotations,
)
from openadapt_flow.compiler.codegen import render_workflow_py
from openadapt_flow.compiler.compile import compile_recording, lint_param_leakage
from openadapt_flow.compiler.effect_mining import (
    StepEffectMining,
    mine_step_effects,
)
from openadapt_flow.compiler.induction import (
    HeldOutValidation,
    InductionResult,
    Proposer,
    induce_program,
    reproduction_score,
    validate_held_out,
)

__all__ = [
    "compile_recording",
    "lint_param_leakage",
    "render_workflow_py",
    "mine_step_effects",
    "StepEffectMining",
    # Multi-trace induction (RFC §3 [4]+[5]): one demo is the single-trace
    # bootstrap; multiple demos induce a parameterized program or refuse.
    "induce_program",
    "validate_held_out",
    "reproduction_score",
    "InductionResult",
    "HeldOutValidation",
    "Proposer",
    "apply_annotations",
    "AnnotationResult",
    "AnthropicStepAnnotator",
    "FakeStepAnnotator",
    "StepAnnotator",
    "WorkflowProposals",
]

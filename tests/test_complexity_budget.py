"""Keep the existing long-function debt from increasing."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
THRESHOLD = 200
MAX_LONG_FUNCTIONS = 70
MAX_REPLAYER_LINES = 13_291
LARGE_FUNCTION_LIMITS = {
    "openadapt_flow/__main__.py::build_parser": 2_631,
    "openadapt_flow/qualification.py::_case_run_report_integrity_error": 1_570,
    "openadapt_flow/compiler/compile.py::compile_recording": 1_195,
    "openadapt_flow/runtime/replayer.py::Replayer._run_step": 1_110,
    "openadapt_flow/execution_profiles.py::_program_action_trace": 936,
    "openadapt_flow/runtime/replayer.py::Replayer._act": 913,
    "openadapt_flow/runtime/replayer.py::Replayer.run": 793,
    "openadapt_flow/qualification.py::evaluate_qualification": 709,
    "openadapt_flow/execution_profiles.py::classify_execution_outcome": 670,
    "openadapt_flow/console/app.py::create_app": 549,
}


class _FunctionLengths(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.stack: list[str] = []
        self.lengths: dict[str, int] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        assert node.end_lineno is not None
        lines = node.end_lineno - node.lineno + 1
        self.lengths[f"{self.path}::{'.'.join(self.stack)}"] = lines
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function


def _current_lengths() -> dict[str, int]:
    lengths: dict[str, int] = {}
    for path in sorted((ROOT / "openadapt_flow").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        visitor = _FunctionLengths(relative)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        lengths.update(visitor.lengths)
    return lengths


def test_long_function_debt_does_not_grow() -> None:
    lengths = _current_lengths()
    long_functions = {key: lines for key, lines in lengths.items() if lines > THRESHOLD}

    assert len(long_functions) <= MAX_LONG_FUNCTIONS
    for key, limit in LARGE_FUNCTION_LIMITS.items():
        assert lengths[key] <= limit, f"{key} grew from its reviewed limit"


def test_replayer_module_does_not_grow() -> None:
    replayer = ROOT / "openadapt_flow" / "runtime" / "replayer.py"
    assert len(replayer.read_text(encoding="utf-8").splitlines()) <= MAX_REPLAYER_LINES

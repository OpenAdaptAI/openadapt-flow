"""Next-step hints use the spelling the person typed."""

from __future__ import annotations

import pytest

from openadapt_flow.cli_hints import (
    ENGINE_PREFIX,
    LAUNCHER_PREFIX,
    command,
    command_prefix,
)


@pytest.mark.parametrize(
    "argv0",
    [
        "/usr/local/bin/openadapt",
        "openadapt",
        r"C:\Users\me\venv\Scripts\openadapt.exe",
        "OPENADAPT.EXE",
    ],
)
def test_launcher_spelling(argv0: str) -> None:
    assert command_prefix([argv0, "flow", "demo"]) == LAUNCHER_PREFIX
    assert command("demo", [argv0]) == "openadapt flow demo"


@pytest.mark.parametrize(
    "argv0",
    [
        "/usr/local/bin/openadapt-flow",
        "openadapt-flow.exe",
        "/x/openadapt_flow/__main__.py",
        "-m",
        "pytest",
        "",
    ],
)
def test_engine_spelling(argv0: str) -> None:
    assert command_prefix([argv0]) == ENGINE_PREFIX
    assert command("explain run", [argv0]) == "openadapt-flow explain run"


def test_empty_argv_keeps_engine_spelling() -> None:
    assert command_prefix([]) == ENGINE_PREFIX

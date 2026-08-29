"""CLI wiring for process contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.__main__ import build_parser, main
from openadapt_flow.admitted_composition import is_process_contract_artifact
from tests.test_admitted_composition_authoring import _two_admitted


def test_parser_dispatches_process() -> None:
    args = build_parser().parse_args(
        [
            "process",
            "--child",
            "intake=./a",
            "--admission",
            "intake=./a.json",
            "--child",
            "posting=./b",
            "--admission",
            "posting=./b.json",
            "--handoff",
            "intake.patient_id=posting.patient_id",
            "--out",
            "process",
        ]
    )
    assert args.func.__name__ == "_cmd_process"
    assert args.child == ["intake=./a", "posting=./b"]
    assert args.admission == ["intake=./a.json", "posting=./b.json"]


def test_cli_process_writes_artifact(tmp_path: Path) -> None:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    out = tmp_path / "process"
    rc = main(
        [
            "process",
            "--child",
            f"intake={intake}",
            "--admission",
            f"intake={intake_env}",
            "--child",
            f"posting={posting}",
            "--admission",
            f"posting={posting_env}",
            "--handoff",
            "intake.patient_id=posting.patient_id",
            "--out",
            str(out),
            "--name",
            "two-step",
        ]
    )
    assert rc == 0
    assert is_process_contract_artifact(out)


def test_cli_process_without_admission_refuses_compose_style_children(
    tmp_path: Path,
) -> None:
    intake, _, posting, _ = _two_admitted(tmp_path)
    with pytest.raises(SystemExit, match="admission"):
        main(
            [
                "process",
                "--child",
                f"intake={intake}",
                "--child",
                f"posting={posting}",
                "--out",
                str(tmp_path / "process"),
            ]
        )


def test_replay_refuses_process(tmp_path: Path) -> None:
    intake, intake_env, posting, posting_env = _two_admitted(tmp_path)
    out = tmp_path / "process"
    rc = main(
        [
            "process",
            "--child",
            f"intake={intake}",
            "--admission",
            f"intake={intake_env}",
            "--child",
            f"posting={posting}",
            "--admission",
            f"posting={posting_env}",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    with pytest.raises(SystemExit, match="replay refuses a process contract"):
        main(["replay", str(out), "--url", "http://example.invalid"])

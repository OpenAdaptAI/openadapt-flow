from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "qualify_macos_textedit.py"
SPEC = importlib.util.spec_from_file_location("macos_qualification", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


def test_file_oracle_confirms_exact_bytes_and_refutes_drift(tmp_path: Path) -> None:
    target = tmp_path / "document.txt"
    target.write_bytes(b"expected\n")
    assert qualification.file_oracle(target, b"expected\n")["status"] == "confirmed"
    assert qualification.file_oracle(target, b"different\n")["status"] == "refuted"


def test_missing_permissions_block_before_launch(monkeypatch) -> None:
    class BlockedClient:
        def capture_trusted(self):
            return False

        def input_trusted(self):
            return False

    monkeypatch.setattr(qualification, "MacWindowClient", BlockedClient)

    def unexpected_launch(*_args, **_kwargs):
        raise AssertionError("permission refusal must happen before app launch")

    monkeypatch.setattr(qualification, "_open_isolated", unexpected_launch)
    code, report = qualification.qualify(3)
    assert code == 2
    assert report["status"] == "blocked"
    assert report["trials_completed"] == 0
    assert report["missing_permissions"] == ["screen_recording", "accessibility"]


def test_harness_enforces_three_trial_minimum() -> None:
    assert qualification.MIN_TRIALS == 3

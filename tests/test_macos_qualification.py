from __future__ import annotations

import importlib.util
from pathlib import Path

from openadapt_flow.backends.remote_display import WindowInfo

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


def test_oracle_failure_taxonomy_distinguishes_refuted_and_unverifiable() -> None:
    assert qualification._oracle_failure_type({"status": "confirmed"}) is None
    assert (
        qualification._oracle_failure_type({"status": "refuted"})
        == "file_effect_refuted"
    )


def test_trial_metrics_separate_silent_incorrect_from_over_halt() -> None:
    results = [
        {
            "backend_sequence_reported_success": True,
            "oracle": {"status": "confirmed"},
        },
        {
            "backend_sequence_reported_success": True,
            "oracle": {"status": "refuted"},
        },
        {
            "backend_sequence_reported_success": True,
            "oracle": {"status": "unverifiable"},
        },
        {
            "backend_sequence_reported_success": False,
            "oracle": {"status": "refuted"},
        },
    ]
    assert qualification._trial_metrics(results) == {
        "silent_incorrect_successes": 2,
        "over_halts": 1,
    }


def test_run_trial_records_backend_success_before_refuted_oracle(
    monkeypatch, tmp_path: Path
) -> None:
    class SuccessfulBackend:
        def __init__(self, *_args, **_kwargs):
            pass

        def screenshot(self):
            return b"frame"

        def press(self, _key):
            return None

        def type_text(self, _text):
            return None

    monkeypatch.setattr(qualification, "MacOSBackend", SuccessfulBackend)
    monkeypatch.setattr(qualification, "_open_isolated", lambda _path: None)
    monkeypatch.setattr(
        qualification,
        "_wait_for_matches",
        lambda *_args, **_kwargs: [
            WindowInfo(1, "TextEdit", "trial.txt", 9001, (0, 0, 100, 100))
        ],
    )
    monkeypatch.setattr(
        qualification,
        "_wait_oracle",
        lambda *_args, **_kwargs: {
            "status": "refuted",
            "expected_bytes": 10,
            "observed_bytes": 1,
        },
    )
    monkeypatch.setattr(qualification, "_close_target", lambda *_args, **_kwargs: None)

    result = qualification._run_trial(object(), tmp_path, "run", 1, [])
    assert result["status"] == "failed"
    assert result["backend_sequence_reported_success"] is True
    assert qualification._trial_metrics([result]) == {
        "silent_incorrect_successes": 1,
        "over_halts": 0,
    }
    assert (
        qualification._oracle_failure_type({"status": "unverifiable"})
        == "file_effect_unverifiable"
    )


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


def test_dirty_candidate_blocks_before_launch(monkeypatch) -> None:
    class TrustedClient:
        def capture_trusted(self):
            return True

        def input_trusted(self):
            return True

    monkeypatch.setattr(qualification, "MacWindowClient", TrustedClient)
    monkeypatch.setattr(
        qualification,
        "_candidate_state",
        lambda trials: {
            "git_sha": "abc123",
            "git_dirty": True,
            "dirty_paths": [" M openadapt_flow/backends/macos_backend.py"],
            "flow_version": "1.9.1",
            "qualification_config": qualification._qualification_config(trials),
        },
    )

    def unexpected_launch(*_args, **_kwargs):
        raise AssertionError("dirty candidate must block before app launch")

    monkeypatch.setattr(qualification, "_open_isolated", unexpected_launch)
    code, report = qualification.qualify(3)
    assert code == 2
    assert report["status"] == "blocked"
    assert report["evidence_classification"] == "diagnostic_only_not_acceptance"
    assert report["trials_completed"] == 0
    assert report["environment"]["candidate"]["git_sha"] == "abc123"


def test_harness_enforces_three_trial_minimum() -> None:
    assert qualification.MIN_TRIALS == 3


def test_window_wait_ignores_one_poll_title_race() -> None:
    transient = WindowInfo(1, "TextEdit", "oa-trial.txt", 10, (0, 0, 100, 100))
    stable = WindowInfo(2, "TextEdit", "oa-trial.txt", 11, (0, 0, 100, 100))

    class RacingClient:
        calls = 0

        def find_windows(self, _owner, _title):
            self.calls += 1
            return [transient] if self.calls == 1 else [stable]

    client = RacingClient()
    matches = qualification._wait_for_matches(
        client, "oa-trial", count=1, timeout_s=1.0
    )
    assert matches == [stable]
    assert client.calls >= 4

"""Unit tests for the OpenEMR benchmark pieces (no network anywhere).

The Anthropic client and backend are faked, the orchestrator's run helpers
are monkeypatched, and ``verify_note_saved`` runs real OCR on synthetic
cv2-rendered screenshots — the same testing style as ``test_benchmark.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np

from openadapt_flow.benchmark import agent_baseline, openemr_benchmark
from openadapt_flow.benchmark.agent_baseline import (
    openemr_task_prompt,
    run_agent,
)
from openadapt_flow.benchmark.openemr_benchmark import (
    aggregate_openemr_results,
    note_for,
    render_openemr_markdown,
    run_openemr_benchmark,
    write_openemr_outputs,
)
from openadapt_flow.benchmark.verify import verify_note_saved

NOTE = "Insurance card copied and coverage verified by phone."


def to_png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


BLANK_PNG = to_png(np.full((800, 1280, 3), 245, dtype=np.uint8))


def make_screen(*lines: str) -> bytes:
    img = np.full((800, 1280, 3), 245, dtype=np.uint8)
    for i, line in enumerate(lines):
        cv2.putText(
            img,
            line,
            (40, 200 + i * 60),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
    return to_png(img)


# -- verify_note_saved (shared, arm-independent) -------------------------------


class TestVerifyNoteSaved:
    def test_note_embedded_in_message_row_passes(self) -> None:
        # OpenEMR shows the note inside a longer list line.
        screen = make_screen(
            "Patient Messages",
            f"2026-07-08 (admin to admin) {NOTE}",
        )
        verdict = verify_note_saved(screen, NOTE)
        assert verdict.success
        assert verdict.longest_run >= 16

    def test_wrapped_fragment_passes(self) -> None:
        # A wrapped note where OCR only captured one distinctive fragment
        # of at least 16 contiguous characters still counts.
        screen = make_screen("coverage verified by")
        verdict = verify_note_saved(screen, NOTE)
        assert verdict.success
        assert verdict.longest_run >= 16

    def test_blank_screen_fails(self) -> None:
        verdict = verify_note_saved(BLANK_PNG, NOTE)
        assert not verdict.success
        assert verdict.longest_run < 16

    def test_wrong_note_fails(self) -> None:
        # A different run's note visible on screen must not satisfy this
        # run's check (contiguous-run criterion, dissimilar note texts).
        screen = make_screen(
            "2026-07-08 (admin to admin) "
            "Dermatology biopsy site healing cleanly, no drainage.",
        )
        verdict = verify_note_saved(screen, NOTE)
        assert not verdict.success

    def test_empty_note_fails(self) -> None:
        assert not verify_note_saved(BLANK_PNG, "  ").success


# -- task prompt ---------------------------------------------------------------


class TestOpenemrTaskPrompt:
    def test_states_intent_not_coordinates(self) -> None:
        prompt = openemr_task_prompt(NOTE)
        assert '"admin"' in prompt
        assert '"pass"' in prompt
        assert "Belford, Phil" in prompt
        assert NOTE in prompt
        assert "coordinate" not in prompt.lower()
        assert "px" not in prompt.lower()


# -- agent scroll action -------------------------------------------------------


class FakeBackend:
    def __init__(self, png: bytes = BLANK_PNG) -> None:
        self.png = png
        self.calls: list[tuple[Any, ...]] = []

    @property
    def viewport(self) -> tuple[int, int]:
        return (1280, 800)

    def screenshot(self) -> bytes:
        return self.png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.calls.append(("click", x, y, double))

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))

    def press(self, key: str) -> None:
        self.calls.append(("press", key))

    def scroll(self, dx: int, dy: int) -> None:
        self.calls.append(("scroll", dx, dy))


def tool_use(action: dict[str, Any], block_id: str = "tu_1") -> Any:
    return SimpleNamespace(
        type="tool_use", id=block_id, name="computer", input=action
    )


def response(blocks: list[Any], stop_reason: str) -> Any:
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


class FakeClient:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.beta = SimpleNamespace(
            messages=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs: Any) -> Any:
        return self.script.pop(0)


class TestAgentScroll:
    def test_scroll_dispatches_wheel_pixels(self) -> None:
        backend = FakeBackend()
        client = FakeClient(
            [
                response(
                    [
                        tool_use(
                            {
                                "action": "scroll",
                                "coordinate": [640, 400],
                                "scroll_direction": "down",
                                "scroll_amount": 4,
                            }
                        )
                    ],
                    "tool_use",
                ),
                response(
                    [tool_use({"action": "scroll", "scroll_direction": "up"})],
                    "tool_use",
                ),
                response([], "end_turn"),
            ]
        )
        result = run_agent(backend, "task", client=client)
        px = agent_baseline.SCROLL_PX_PER_UNIT
        assert ("scroll", 0, 4 * px) in backend.calls
        assert ("scroll", 0, -3 * px) in backend.calls
        assert result.actions == 2

    def test_scroll_listed_as_supported(self) -> None:
        assert "scroll" in agent_baseline._SUPPORTED_ACTIONS


# -- notes ---------------------------------------------------------------------


class TestNotes:
    def test_distinct_across_both_arms(self) -> None:
        notes = [note_for("compiled", i) for i in range(20)] + [
            note_for("agent", i) for i in range(10)
        ]
        assert len(set(notes)) == 30

    def test_pairwise_dissimilarity_below_run_threshold(self) -> None:
        # Several runs' notes are visible on the same final screen, and
        # verify_note_saved accepts a contiguous 16-char match — so no two
        # notes may share a 16-char squashed substring, or one run's note
        # would satisfy another run's check.
        import difflib

        def squash(text: str) -> str:
            return "".join(text.lower().split())

        notes = [note_for("compiled", i) for i in range(20)] + [
            note_for("agent", i) for i in range(10)
        ]
        for i, a in enumerate(notes):
            for b in notes[i + 1 :]:
                longest = max(
                    block.size
                    for block in difflib.SequenceMatcher(
                        None, squash(a), squash(b), autojunk=False
                    ).get_matching_blocks()
                )
                assert longest < 16, (a, b, longest)


# -- orchestrator --------------------------------------------------------------


def compiled_row(i: int, *, success: bool = True, wall: float = 38.0) -> dict:
    return {
        "arm": "compiled",
        "i": i,
        "note": note_for("compiled", i),
        "wall_s": wall,
        "success": success,
        "matched_ratio": 1.0 if success else 0.1,
        "longest_run": 40 if success else 3,
        "replayer_success": success,
        "heal_count": 1,
        "actions": 18,
        "api_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "error": None,
    }


def agent_row(i: int, *, success: bool = True, wall: float = 150.0) -> dict:
    return {
        "arm": "agent",
        "i": i,
        "note": note_for("agent", i),
        "wall_s": wall,
        "success": success,
        "matched_ratio": 1.0 if success else 0.2,
        "longest_run": 40 if success else 4,
        "actions": 30,
        "api_calls": 31,
        "input_tokens": 900_000,
        "output_tokens": 9_000,
        "cost_usd": 0.5,
        "stopped": "model_done" if success else "budget_exhausted",
        "model_stop_reason": "end_turn",
        "error": None,
    }


class TestOpenemrOrchestrator:
    def make_results(self) -> dict:
        compiled = [compiled_row(i, success=i != 3) for i in range(20)]
        agents = [agent_row(i, success=i not in (1, 5)) for i in range(10)]
        results = aggregate_openemr_results(compiled, agents)
        results["pace_s"] = 30.0
        return results

    def test_aggregates(self) -> None:
        results = self.make_results()
        c = results["arms"]["compiled"]
        a = results["arms"]["agent"]
        assert c["n"] == 20
        assert c["success_count"] == 19
        assert c["cost_usd_total"] == 0.0
        assert a["n"] == 10
        assert a["success_count"] == 8
        assert a["cost_usd_total"] == 5.0
        assert results["model"] == agent_baseline.MODEL
        assert results["target"].startswith("https://demo.openemr.io")

    def test_markdown_names_anchor_and_caveats(self) -> None:
        md = render_openemr_markdown(self.make_results())
        assert "MockMed" in md  # methodology anchor called out
        assert "CI-reproducible" in md
        assert "Caveats" in md
        assert "resets daily" in md
        assert "95%" in md  # compiled success rate
        assert "80%" in md  # agent success rate
        assert "agent run 2" in md and "agent run 6" in md
        assert "compiled run 4" in md
        for banned in (
            "delve",
            "leverage",
            "seamless",
            "robust",
            "comprehensive",
            "transformative",
        ):
            assert banned not in md.lower(), banned

    def test_write_outputs(self, tmp_path: Path) -> None:
        import json

        write_openemr_outputs(self.make_results(), tmp_path)
        loaded = json.loads((tmp_path / "results.json").read_text())
        assert loaded["arms"]["agent"]["success_count"] == 8
        assert (tmp_path / "latency_cost.png").stat().st_size > 1000
        assert "latency_cost.png" in (tmp_path / "BENCHMARK.md").read_text()

    def test_run_paces_and_records_failures(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        sleeps: list[float] = []

        def fake_compiled(bundle, url, run_dir, note, **kwargs):
            if note == note_for("compiled", 1):
                raise RuntimeError("demo instance hiccup")
            return compiled_row(0)

        def fake_agent(url, note, **kwargs):
            assert kwargs["max_actions"] == 40
            assert note in kwargs["task"]
            return agent_row(0)

        monkeypatch.setattr(
            openemr_benchmark, "_compiled_run", fake_compiled
        )
        monkeypatch.setattr(openemr_benchmark, "_agent_run", fake_agent)
        results = run_openemr_benchmark(
            tmp_path,
            tmp_path / "bundle",
            n_compiled=3,
            n_agent=2,
            pace_s=30.0,
            sleep=sleeps.append,
            log=lambda _msg: None,
        )
        # 3 compiled runs pace twice (no sleep before the first), 2 agent
        # runs pace twice (gap after the compiled arm and between runs).
        assert sleeps == [30.0, 30.0, 30.0, 30.0]
        rows = results["runs"]["compiled"]
        assert [r["success"] for r in rows] == [True, False, True]
        assert "demo instance hiccup" in rows[1]["error"]
        assert (tmp_path / "results.json").is_file()
        assert (tmp_path / "BENCHMARK.md").is_file()

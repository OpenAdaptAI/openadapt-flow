"""CLI wiring for the new-architecture commands: worklist / effects / actuator
/ durable resume + approve / deployment ``run``.

Arg-parsing + delegation only — the heavy browser/vision stack is faked, and
the durable paths run against a real ``CheckpointStore`` on disk (import-light,
no backend). The point is to prove the CLI CONSTRUCTS and INJECTS the library
objects; the objects' own behavior is covered by their unit suites.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from openadapt_flow.__main__ import (
    _deployment_runtime,
    _load_worklist_file,
    _resolve_worklists,
    build_parser,
    main,
)
from openadapt_flow.compiler.induction import induce_program
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)

# ---------------------------------------------------------------------------
# worklist loading + relation binding
# ---------------------------------------------------------------------------


def test_load_worklist_csv(tmp_path: Path) -> None:
    f = tmp_path / "wl.csv"
    f.write_text("patient,dose\nAlice,5mg\nBob,10mg\n")
    assert _load_worklist_file(f) == [
        {"patient": "Alice", "dose": "5mg"},
        {"patient": "Bob", "dose": "10mg"},
    ]


def test_load_worklist_json_list_and_object(tmp_path: Path) -> None:
    lst = tmp_path / "l.json"
    lst.write_text('[{"patient":"Cy","dose":"2mg"}]')
    assert _load_worklist_file(lst) == [{"patient": "Cy", "dose": "2mg"}]
    obj = tmp_path / "o.json"
    obj.write_text('{"patient":"Dee"}')
    assert _load_worklist_file(obj) == [{"patient": "Dee"}]


def test_load_worklist_bad_suffix(tmp_path: Path) -> None:
    f = tmp_path / "wl.txt"
    f.write_text("x")
    with pytest.raises(SystemExit, match="must be .csv or .json"):
        _load_worklist_file(f)


def _loop_program() -> Workflow:
    """An induced program with exactly one loop relation."""

    def trace(vals: list[str]) -> Workflow:
        steps = [Step(id="open", intent="click open", action=ActionKind.CLICK)]
        for i, v in enumerate(vals):
            steps.append(
                Step(id=f"r{i}", intent="type row", action=ActionKind.TYPE, text=v)
            )
        return Workflow(name="t", steps=steps)

    result = induce_program([trace(["a", "b"]), trace(["a", "b", "c"])])
    assert result.workflow is not None and result.workflow.program is not None
    return result.workflow


def test_resolve_worklists_bare_and_named(tmp_path: Path) -> None:
    wf = _loop_program()
    (rel,) = tuple(wf.data_sources)
    csv = tmp_path / "wl.csv"
    csv.write_text("row\nX\nY\n")
    expected = {rel: [{"row": "X"}, {"row": "Y"}]}
    assert _resolve_worklists([str(csv)], wf) == expected
    assert _resolve_worklists([f"{rel}={csv}"], wf) == expected


def test_resolve_worklists_unknown_relation(tmp_path: Path) -> None:
    wf = _loop_program()
    with pytest.raises(SystemExit, match="not one of this program"):
        _resolve_worklists(["nope=/x"], wf)


def test_resolve_worklists_linear_bundle_refused(tmp_path: Path) -> None:
    lin = Workflow(
        name="lin", steps=[Step(id="a", intent="c", action=ActionKind.CLICK)]
    )
    with pytest.raises(SystemExit, match="only to a PROGRAM bundle"):
        _resolve_worklists(["/x.csv"], lin)


# ---------------------------------------------------------------------------
# deployment runtime resolution (the effect/actuator/durable injection seam)
# ---------------------------------------------------------------------------


def _replay_ns(**over) -> argparse.Namespace:
    base = dict(
        config=None,
        effects_kind=None,
        effects_base_url=None,
        effects_root=None,
        api_actuator=False,
        api_base_url=None,
        durable=False,
        allow_model_grounding=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_deployment_runtime_defaults_are_off() -> None:
    cfg, ev, act, durable, egress = _deployment_runtime(_replay_ns())
    assert (ev, act, durable, egress) == (None, None, False, False)


def test_deployment_runtime_flags_wire_objects() -> None:
    ns = _replay_ns(
        effects_kind="rest",
        effects_base_url="http://sor",
        api_base_url="http://api",
        durable=True,
    )
    cfg, ev, act, durable, egress = _deployment_runtime(ns)
    assert type(ev).__name__ == "RestRecordVerifier"
    assert type(act).__name__ == "ApiActuator"  # api_base_url implies enabled
    assert durable is True


def test_deployment_runtime_reads_config_file(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.yaml"
    cfg_file.write_text(
        "effects:\n  kind: document-hash\n  root: /tmp/store\n"
        "runtime:\n  durable: true\n"
    )
    cfg, ev, act, durable, egress = _deployment_runtime(
        _replay_ns(config=str(cfg_file))
    )
    assert type(ev).__name__ == "DocumentHashVerifier"
    assert durable is True


def test_deployment_runtime_bad_effects_config_exits() -> None:
    with pytest.raises(SystemExit, match="base_url"):
        _deployment_runtime(_replay_ns(effects_kind="rest"))  # no base_url


# ---------------------------------------------------------------------------
# approve / resume durable paths
# ---------------------------------------------------------------------------


def _paused_run(tmp_path: Path, *, verified_first: bool = True) -> Path:
    run = tmp_path / "run"
    store = CheckpointStore(run)
    store.write_manifest(
        RunManifest(workflow_name="w", bundle_dir=str(tmp_path / "b"), params={})
    )
    if verified_first:
        store.write_checkpoint(
            RunCheckpoint(
                workflow_name="w", step_index=0, step_id="s0", next_step_index=1
            )
        )
    store.write_pending(
        PendingEscalation(
            workflow_name="w",
            step_index=1,
            step_id="s1",
            category="identity",
            reason="boom",
            resume_from_index=1,
        )
    )
    return run


def test_approve_flips_status_and_is_idempotent(tmp_path: Path, capsys) -> None:
    run = _paused_run(tmp_path)
    assert main(["approve", str(run)]) == 0
    assert CheckpointStore(run).read_pending().status == "approved"
    # Second approve is a no-op success.
    assert main(["approve", str(run)]) == 0
    assert "already approved" in capsys.readouterr().out


def test_approve_no_pending(tmp_path: Path, capsys) -> None:
    run = tmp_path / "empty"
    run.mkdir()
    assert main(["approve", str(run)]) == 1
    assert "nothing to approve" in capsys.readouterr().out


def test_resume_no_pending_returns_1(tmp_path: Path) -> None:
    run = tmp_path / "empty"
    run.mkdir()
    assert main(["resume", str(run)]) == 1


def test_resume_require_approval_refuses_pending(tmp_path: Path, capsys) -> None:
    run = _paused_run(tmp_path)
    rc = main(["resume", str(run), "--require-approval"])
    assert rc == 3
    assert "not 'approved'" in capsys.readouterr().out


def test_resume_without_url_exits(tmp_path: Path) -> None:
    run = _paused_run(tmp_path)
    # Approved + no --url and no backend.url -> a clear config error.
    main(["approve", str(run)])
    with pytest.raises(SystemExit, match="needs the target app URL"):
        main(["resume", str(run), "--require-approval"])


# ---------------------------------------------------------------------------
# parser wiring for every new command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,func_name",
    [
        (["induce", "r1", "r2", "--out", "o"], "_cmd_induce"),
        (["replay", "b", "--worklist", "w.csv"], "_cmd_replay"),
        (["run", "b", "--config", "d.yaml"], "_cmd_run"),
        (["resume", "run_dir"], "_cmd_resume"),
        (["approve", "run_dir"], "_cmd_approve"),
    ],
)
def test_parser_dispatches(argv, func_name) -> None:
    args = build_parser().parse_args(argv)
    assert args.func.__name__ == func_name


def test_replay_parser_has_deployment_flags() -> None:
    args = build_parser().parse_args(
        [
            "replay",
            "b",
            "--config",
            "d.yaml",
            "--effects-kind",
            "fhir",
            "--effects-base-url",
            "http://sor",
            "--api-actuator",
            "--durable",
            "--worklist",
            "rel=w.csv",
        ]
    )
    assert args.config == "d.yaml"
    assert args.effects_kind == "fhir"
    assert args.api_actuator is True
    assert args.durable is True
    assert args.worklist == ["rel=w.csv"]


# ---------------------------------------------------------------------------
# replay/run integration: prove the deployment objects reach the Replayer
# ---------------------------------------------------------------------------


class _FakePage:
    video = None

    def goto(self, url):  # noqa: D401 - fake
        self.url = url


class _FakeBrowser:
    def new_page(self, viewport=None):
        return _FakePage()

    def close(self):
        pass


class _FakeChromium:
    def launch(self, headless=True):
        return _FakeBrowser()


class _FakePlaywright:
    chromium = _FakeChromium()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeReport:
    success = True
    screenshots_may_leave_box = False


def _install_fake_browser(monkeypatch, captured: dict) -> None:
    class _FakeReplayer:
        def __init__(self, backend, **kwargs):
            captured["ctor"] = kwargs

        def run(self, workflow, **kwargs):
            captured["run"] = kwargs
            return _FakeReport()

    import playwright.sync_api as psa

    import openadapt_flow._browser_setup as bs
    import openadapt_flow.backends.playwright_backend as pwb
    import openadapt_flow.report as report_mod
    import openadapt_flow.runtime as runtime_mod
    import openadapt_flow.runtime.grounder as grounder_mod
    import openadapt_flow.runtime.remote_vlm as remote_mod

    monkeypatch.setattr(psa, "sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(pwb, "PlaywrightBackend", lambda page: "backend")
    monkeypatch.setattr(bs, "ensure_chromium_installed", lambda: None)
    monkeypatch.setattr(grounder_mod, "build_grounder", lambda fallback=None: None)
    monkeypatch.setattr(remote_mod, "appliance_from_env", lambda: None)
    monkeypatch.setattr(report_mod, "render_run_report", lambda run_dir: "REPORT.md")
    monkeypatch.setattr(runtime_mod, "Replayer", _FakeReplayer)


def test_replay_injects_effects_actuator_durable_and_worklist(
    tmp_path: Path, monkeypatch
) -> None:
    # A program bundle with one loop relation, saved to disk.
    wf = _loop_program()
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    (rel,) = tuple(wf.data_sources)
    csv = tmp_path / "wl.csv"
    csv.write_text("row\nX\nY\n")

    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)

    rc = main(
        [
            "replay",
            str(bundle),
            "--url",
            "http://app.example",
            "--effects-kind",
            "rest",
            "--effects-base-url",
            "http://sor",
            "--api-base-url",
            "http://api",
            "--durable",
            "--worklist",
            f"{rel}={csv}",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 0
    ctor = captured["ctor"]
    assert type(ctor["effect_verifier"]).__name__ == "RestRecordVerifier"
    assert type(ctor["api_actuator"]).__name__ == "ApiActuator"
    assert ctor["durable"] is True
    assert captured["run"]["worklists"] == {rel: [{"row": "X"}, {"row": "Y"}]}


def test_run_delegates_to_replay_with_config(tmp_path: Path, monkeypatch) -> None:
    wf = Workflow(name="lin", steps=[Step(id="a", intent="c", action=ActionKind.CLICK)])
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    cfg = tmp_path / "d.yaml"
    cfg.write_text(
        "backend:\n  url: http://from-config\n"
        "effects:\n  kind: rest\n  base_url: http://sor\n"
        "runtime:\n  durable: true\n"
    )

    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)

    rc = main(
        ["run", str(bundle), "--config", str(cfg), "--run-dir", str(tmp_path / "r")]
    )
    assert rc == 0
    # backend.url from config was used; effects + durable wired from config.
    assert type(captured["ctor"]["effect_verifier"]).__name__ == "RestRecordVerifier"
    assert captured["ctor"]["durable"] is True

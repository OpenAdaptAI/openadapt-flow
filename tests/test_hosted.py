"""Unit tests for the hosted-connectivity wrapper (login / push / break-emit).

Network is fully mocked (monkeypatched ``httpx.get`` / ``httpx.post`` returning
``httpx.Response`` objects, mirroring ``tests/test_remote_vlm.py``). No real
credentials, no real host, no real recording — every artifact is a tmp fixture.
The engine internals (compiler / IR / replay) are untouched; only the new
``openadapt_flow.hosted`` module + its CLI wiring are exercised.
"""

from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import httpx
import pytest

from openadapt_flow import hosted, privacy
from openadapt_flow.__main__ import build_parser, main
from openadapt_flow.ir import HaltObservation, Resolution, RunReport, StepResult


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Root config.toml under a tmp dir and clear the token env for every test."""
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(hosted.TOKEN_ENV, raising=False)
    yield
    privacy.reset_scrubbers()


# ---------------------------------------------------------------------------
# host + token resolution + config.toml
# ---------------------------------------------------------------------------


def test_resolve_host_precedence(monkeypatch):
    assert hosted.resolve_host() == hosted.DEFAULT_HOST
    assert hosted.resolve_host("https://example.test/") == "https://example.test"
    hosted._update_hosted_config({"host": "https://stored.test"})
    assert hosted.resolve_host() == "https://stored.test"
    # explicit arg still wins over stored config
    assert hosted.resolve_host("https://arg.test") == "https://arg.test"


def test_resolve_token_precedence(monkeypatch):
    with pytest.raises(hosted.HostedError):
        hosted.resolve_token()
    hosted._update_hosted_config({"token": "from_config"})
    assert hosted.resolve_token() == "from_config"
    monkeypatch.setenv(hosted.TOKEN_ENV, "from_env")
    assert hosted.resolve_token() == "from_env"
    assert hosted.resolve_token("from_arg") == "from_arg"


def test_config_toml_roundtrip_and_perms():
    path = hosted._update_hosted_config({"host": "https://h.test", "token": "tok"})
    assert path.is_file()
    section = hosted._hosted_config()
    assert section["host"] == "https://h.test"
    assert section["token"] == "tok"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    # a second update merges, not clobbers
    hosted._update_hosted_config({"deployment_lane": "byoc"})
    section = hosted._hosted_config()
    assert section["host"] == "https://h.test"
    assert section["deployment_lane"] == "byoc"


def test_load_toml_minimal_fallback(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('[hosted]\nhost = "https://x.test"\nphi = true\npoll = 60\n')
    data = hosted._load_toml_minimal(f)
    assert data["hosted"] == {"host": "https://x.test", "phi": True, "poll": 60}


# ---------------------------------------------------------------------------
# recording discovery + zipping
# ---------------------------------------------------------------------------


def _make_recording(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{}")
    (d / "events.jsonl").write_text("{}\n")
    return d


def test_find_latest_recording_picks_newest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = _make_recording(tmp_path, "rec_old")
    new = _make_recording(tmp_path, "rec_new")
    import os
    import time

    os.utime(old, (1, 1))
    os.utime(new, (time.time(), time.time()))
    assert hosted.find_latest_recording() == new


def test_find_latest_recording_none(tmp_path):
    with pytest.raises(hosted.HostedError):
        hosted.find_latest_recording(tmp_path)


def test_zip_dir_contents_at_root(tmp_path):
    rec = _make_recording(tmp_path, "rec")
    zip_path = hosted._zip_dir(rec)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
        assert "meta.json" in names
        assert "events.jsonl" in names
    finally:
        import shutil

        shutil.rmtree(zip_path.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_success_saves_config(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: httpx.Response(200, json={"count": 0})
    )
    result = hosted.login(token="tok", host="https://h.test")
    assert result["valid"] is True
    assert result["host"] == "https://h.test"
    assert hosted._hosted_config()["token"] == "tok"


def test_login_no_save(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: httpx.Response(200, json={"count": 0})
    )
    result = hosted.login(token="tok", host="https://h.test", save=False)
    assert result["config_path"] is None
    assert "token" not in hosted._hosted_config()


def test_login_rejected_token(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(401))
    with pytest.raises(hosted.HostedError, match="401"):
        hosted.login(token="bad", host="https://h.test")


def test_login_network_error(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(hosted.HostedError, match="reach"):
        hosted.login(token="tok", host="https://h.test")


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def _capture_post(recorder, status=201, json_body=None):
    def fake(url, **kw):
        recorder["url"] = url
        recorder["kw"] = kw
        return httpx.Response(status, json=json_body or {})

    return fake


def test_push_success(tmp_path, monkeypatch):
    rec = _make_recording(tmp_path, "rec")
    recorder: dict = {}
    body = {
        "ingest": {
            "workflow_id": "wf_123",
            "workflow_name": "Pushed recording",
            "kind": "recording",
            "compile": {"status": "compiled", "steps": 4},
        }
    }
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 201, body))
    result = hosted.push(rec, name="My flow", host="https://h.test", token="tok")
    assert result["workflow_id"] == "wf_123"
    assert result["dashboard_url"] == "https://h.test/dashboard/workflows/wf_123"
    assert recorder["url"] == "https://h.test/api/ingest"
    assert recorder["kw"]["data"] == {"kind": "recording", "name": "My flow"}
    assert recorder["kw"]["headers"]["Authorization"] == "Bearer tok"
    assert "file" in recorder["kw"]["files"]


def test_push_default_path_uses_latest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_recording(tmp_path, "rec")
    recorder: dict = {}
    monkeypatch.setattr(
        httpx,
        "post",
        _capture_post(recorder, 201, {"ingest": {"workflow_id": "wf_9"}}),
    )
    result = hosted.push(host="https://h.test", token="tok")
    assert result["workflow_id"] == "wf_9"


def test_push_bad_kind(tmp_path):
    with pytest.raises(hosted.HostedError, match="kind"):
        hosted.push(tmp_path, kind="nonsense", token="tok", host="https://h.test")


def test_push_non_201(tmp_path, monkeypatch):
    rec = _make_recording(tmp_path, "rec")
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Response(502, text="store down")
    )
    with pytest.raises(hosted.HostedError, match="502"):
        hosted.push(rec, token="tok", host="https://h.test")


def test_push_401(tmp_path, monkeypatch):
    rec = _make_recording(tmp_path, "rec")
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(401))
    with pytest.raises(hosted.HostedError, match="401"):
        hosted.push(rec, token="tok", host="https://h.test")


# ---------------------------------------------------------------------------
# report_break
# ---------------------------------------------------------------------------


def _halted_run(run_dir: Path) -> Path:
    report = RunReport(
        workflow_name="triage",
        started_at="2026-01-01T00:00:00Z",
        success=False,
        total_ms=2500.0,
        results=[
            StepResult(
                step_id="s1",
                intent="click Save for Jane Doe",
                ok=False,
                error="element not found for MRN 12345",
                resolution=Resolution(
                    rung="ocr", point=(0, 0), confidence=0.5, elapsed_ms=1.0
                ),
            )
        ],
        halt=HaltObservation(
            state_id="st1",
            intent="click Save for Jane Doe",
            reason="unexpected dialog blocking MRN 12345",
            observed_texts=["Jane Doe"],
        ),
    )
    report.save(run_dir)
    return run_dir / "report.json"


def test_report_break_success(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    recorder: dict = {}
    body = {
        "ok": True,
        "run_id": "run_1",
        "halt_id": "halt_1",
        "status": "halt",
        "deployment_kind": "byoc",
        "teach_url": "/dashboard/runs/run_1/teach",
    }
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, body))
    result = hosted.report_break(
        run_dir,
        workflow_id="wf_1",
        deployment_kind="byoc",
        org_id="org_1",
        host="https://h.test",
        token="tok",
    )
    assert result["emitted"] is True
    assert result["teach_url"] == "https://h.test/dashboard/runs/run_1/teach"
    posted = recorder["kw"]["json"]
    assert posted["workflow_id"] == "wf_1"
    assert posted["deployment_kind"] == "byoc"
    assert posted["org_id"] == "org_1"
    assert posted["status"] == "halt"
    assert posted["resolver_rung"] == "ocr"
    assert posted["metrics"] == {"steps": 1, "duration_s": 2.5}
    assert posted["report_path"].endswith("report.json")
    assert len(posted["drift_signature"]) == 16
    # no screenshots / dom / field values leak
    assert "screenshots" not in posted


def test_report_break_scrubs_phi(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)

    class FakeScrubber:
        def scrub_text(self, text, is_separated=False):
            return text.replace("Jane Doe", "<PERSON>").replace("12345", "<NUM>")

    privacy.set_text_scrubber(FakeScrubber())
    recorder: dict = {}
    monkeypatch.setattr(httpx, "post", _capture_post(recorder, 202, {"ok": True}))
    hosted.report_break(run_dir, workflow_id="wf_1", host="https://h.test", token="tok")
    posted = recorder["kw"]["json"]
    assert "Jane Doe" not in json.dumps(posted)
    assert "12345" not in json.dumps(posted)
    assert "<PERSON>" in posted["step_intent"]


def test_report_break_422_falls_back_local(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: httpx.Response(422, json={"error": "phi"})
    )
    result = hosted.report_break(
        run_dir, workflow_id="wf_1", host="https://h.test", token="tok"
    )
    assert result["emitted"] is False
    assert result["local_only"] is True


def test_report_break_422_no_fallback_raises(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(422))
    with pytest.raises(hosted.HostedError, match="422"):
        hosted.report_break(
            run_dir,
            workflow_id="wf_1",
            host="https://h.test",
            token="tok",
            allow_local_fallback=False,
        )


def test_report_break_success_run_emits_nothing(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    RunReport(
        workflow_name="triage",
        started_at="2026-01-01T00:00:00Z",
        success=True,
    ).save(run_dir)
    # No httpx call should happen; make post explode if it does.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: pytest.fail("should not POST for a successful run"),
    )
    result = hosted.report_break(
        run_dir, workflow_id="wf_1", host="https://h.test", token="tok"
    )
    assert result["emitted"] is False


def test_report_break_missing_report(tmp_path):
    with pytest.raises(hosted.HostedError, match="report.json"):
        hosted.report_break(
            tmp_path, workflow_id="wf_1", host="https://h.test", token="tok"
        )


def test_report_break_scrubber_unavailable_fails_closed(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    _halted_run(run_dir)
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    privacy.reset_scrubbers()
    privacy.set_text_scrubber(None)

    def boom(*a, **k):
        raise privacy.PrivacyNotAvailable("missing")

    monkeypatch.setattr(privacy, "scrub_text", boom)
    result = hosted.report_break(
        run_dir, workflow_id="wf_1", host="https://h.test", token="tok"
    )
    assert result["local_only"] is True


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_parser_has_new_commands():
    parser = build_parser()
    # smoke: each subcommand parses to its handler
    for cmd in ("login", "push", "report-break"):
        assert cmd in parser._subparsers._group_actions[0].choices


def test_cli_login_dispatch(monkeypatch, capsys):
    called: dict = {}

    def fake_login(token=None, host=None, save=True):
        called.update(token=token, host=host, save=save)
        return {
            "host": host or "https://h.test",
            "valid": True,
            "settings_url": "https://h.test/dashboard/settings/ingest",
            "config_path": "/tmp/config.toml",
        }

    monkeypatch.setattr(hosted, "login", fake_login)
    rc = main(["login", "--token", "tok", "--host", "https://h.test"])
    assert rc == 0
    assert called == {"token": "tok", "host": "https://h.test", "save": True}
    assert "Logged in" in capsys.readouterr().out


def test_cli_push_dispatch(monkeypatch, capsys):
    def fake_push(path, kind="recording", name=None, host=None, token=None):
        return {
            "workflow_id": "wf_7",
            "workflow_name": "n",
            "kind": kind,
            "compile": {"status": "compiled"},
            "dashboard_url": "https://h.test/dashboard/workflows/wf_7",
        }

    monkeypatch.setattr(hosted, "push", fake_push)
    rc = main(["push", "some/rec", "--kind", "bundle"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "wf_7" in out
    assert "Dashboard" in out


def test_cli_report_break_dispatch(monkeypatch, capsys):
    def fake_report_break(run_dir, **kw):
        assert kw["workflow_id"] == "wf_1"
        return {
            "emitted": True,
            "run_id": "r",
            "halt_id": "h",
            "status": "halt",
            "teach_url": "https://h.test/dashboard/runs/r/teach",
        }

    monkeypatch.setattr(hosted, "report_break", fake_report_break)
    rc = main(["report-break", "runs/r1", "--workflow-id", "wf_1"])
    assert rc == 0
    assert "Break reported" in capsys.readouterr().out


def test_cli_push_error_returns_1(monkeypatch, capsys):
    def fake_push(*a, **k):
        raise hosted.HostedError("no token")

    monkeypatch.setattr(hosted, "push", fake_push)
    rc = main(["push"])
    assert rc == 1
    assert "push failed" in capsys.readouterr().out

"""Unit tests for the Parallels control layer (no live VM).

``prlctl`` is mocked by monkeypatching ``subprocess.run`` in the module, so
these run anywhere (CI included) and pin the parsing/So the sequencing logic.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from openadapt_flow.backends import parallels_vm as pv
from openadapt_flow.backends.parallels_vm import ParallelsError, ParallelsVM

UUID = "{d4f9c29a-52e1-4793-9334-7e971c3d0ab3}"


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _mock_run(monkeypatch, responses):
    """Patch subprocess.run to return canned CompletedProcess per call.

    ``responses`` is a list consumed in order; each item is a
    (predicate_or_None, CompletedProcess). Records calls for assertions.
    """
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        # args[0] is the prlctl binary; the rest is the subcommand.
        sub = args[1] if len(args) > 1 else ""
        for key, cp in responses.items():
            if key in sub or key in " ".join(str(a) for a in args):
                return cp
        return _completed("")

    monkeypatch.setattr(pv.subprocess, "run", fake_run)
    return calls


def test_status_parses_running(monkeypatch):
    # `prlctl list --all -o status,uuid` puts STATUS in the first column.
    out = f"STATUS   UUID\nrunning  {UUID}\n"
    _mock_run(monkeypatch, {"list": _completed(out)})
    assert ParallelsVM(UUID).status() == "running"


def test_snapshot_parses_id(monkeypatch):
    out = (
        "Creating the snapshot...\n"
        "The snapshot with id {516f223f-7e3a-48f4-90d0-f69f9aaa7644} "
        "has been successfully created.\n"
    )
    _mock_run(monkeypatch, {"snapshot": _completed(out)})
    sid = ParallelsVM(UUID).snapshot("ready")
    assert sid == "{516f223f-7e3a-48f4-90d0-f69f9aaa7644}"


def test_snapshot_raises_when_unparseable(monkeypatch):
    _mock_run(monkeypatch, {"snapshot": _completed("no id here")})
    with pytest.raises(ParallelsError):
        ParallelsVM(UUID).snapshot("ready")


def test_list_snapshots_marks_current(monkeypatch):
    out = (
        "PARENT_SNAPSHOT_ID  SNAPSHOT_ID\n"
        "                    {6e1057e6-8db0-41b1-b900-c308eb8ec17c}\n"
        "{6e1057e6-8db0-41b1-b900-c308eb8ec17c} *{516f223f-7e3a-48f4-90d0-f69f9aaa7644}\n"
    )
    _mock_run(monkeypatch, {"snapshot-list": _completed(out)})
    snaps = ParallelsVM(UUID).list_snapshots()
    assert len(snaps) == 2
    assert snaps[0].current is False
    assert snaps[1].current is True
    assert snaps[1].snapshot_id == "{516f223f-7e3a-48f4-90d0-f69f9aaa7644}"


def test_guest_ip_skips_apipa(monkeypatch):
    ipconfig = (
        "   Autoconfiguration IPv4 Address. . : 169.254.83.107\n"
        "   IPv4 Address. . . . . . . . . . . : 10.211.55.3\n"
    )
    _mock_run(monkeypatch, {"exec": _completed(ipconfig)})
    assert ParallelsVM(UUID).guest_ip() == "10.211.55.3"


def test_host_ip_matches_guest_subnet(monkeypatch):
    _mock_run(
        monkeypatch,
        {"exec": _completed("   IPv4 Address. . . . . . . . . . . : 10.211.55.3\n")},
    )
    vm = ParallelsVM(UUID)
    ifconfig = types.SimpleNamespace(
        stdout="\tinet 10.211.55.2 netmask 0xffffff00 broadcast 10.211.55.255\n"
        "\tinet 192.168.1.20 netmask 0xffffff00\n"
    )
    monkeypatch.setattr(
        pv.subprocess,
        "run",
        lambda *a, **k: (
            ifconfig
            if a[0] == ["ifconfig"]
            else _completed("   IPv4 Address. . . : 10.211.55.3\n")
        ),
    )
    assert vm.host_ip("10.211.55.3") == "10.211.55.2"


def test_run_raises_on_nonzero(monkeypatch):
    _mock_run(
        monkeypatch, {"start": _completed("boom", returncode=1, stderr="no such VM")}
    )
    with pytest.raises(ParallelsError):
        ParallelsVM(UUID).start()


def test_shim_url_uses_guest_ip(monkeypatch):
    _mock_run(monkeypatch, {"exec": _completed("   IPv4 Address. . . : 10.211.55.3\n")})
    assert ParallelsVM(UUID).shim_url() == "http://10.211.55.3:5000"

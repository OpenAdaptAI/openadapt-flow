"""Fail-closed admission gate for ``openadapt-flow run`` (openadapt_flow.run_gate).

A fully-armed + certified + effect-covered + encrypted + integrity-sealed bundle
is ADMITTED; each individually broken gate (uncertified / unarmed consequential
click / screen-only write / unverifiable write with no approval / unencrypted /
tampered manifest / version-pin mismatch) is REFUSED, naming the failing gate.
A CLI test confirms ``run`` refuses without executing (and ``--dry-run`` reports).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.deployment import (
    DeploymentConfig,
    EffectsConfig,
    PolicySection,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.run_gate import (
    GATE_APPROVAL,
    GATE_CERTIFICATION,
    GATE_EFFECT,
    GATE_ENCRYPTION,
    GATE_IDENTITY,
    GATE_MANIFEST,
    evaluate_run_gate,
)
from openadapt_flow.runtime.effects import Effect, EffectKind

_KEY = "correct horse battery staple"
_PC = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved OK")]


def _click(
    step_id: str,
    intent: str,
    *,
    ocr: str = "Button",
    armed: bool = True,
    risk: str = "reversible",
    effects: list[Effect] | None = None,
    expect=None,
) -> Step:
    return Step(
        id=step_id,
        intent=intent,
        action=ActionKind.CLICK,
        risk=risk,
        anchor=Anchor(
            template=f"{step_id}.png",
            region=(0, 0, 10, 10),
            click_point=(5, 5),
            ocr_text=ocr,
            context_text="Row 42 Jane Doe" if armed else None,
        ),
        identity_armed=armed,
        identity_unarmed_reason=None if armed else "no readable row band",
        effects=list(effects) if effects is not None else [],
        expect=list(expect) if expect is not None else list(_PC),
    )


def _write_effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient_id": "p1"},
        idempotency_key="run-1",
        risk="irreversible",
    )


def _good_workflow(name: str = "good") -> Workflow:
    """A bundle that PASSES clinical-write: an armed navigation click and an
    armed irreversible write declaring a keyed system-of-record effect."""
    return Workflow(
        name=name,
        steps=[
            _click("s0", "click 'Open chart'", ocr="Open"),
            _click(
                "s1",
                "click 'Save note'",
                ocr="Save",
                risk="irreversible",
                effects=[_write_effect()],
            ),
        ],
    )


def _seal(
    wf: Workflow, tmp_path: Path, *, encrypt: bool = True
) -> tuple[Workflow, Path]:
    """Save ``wf`` (optionally encrypted), then load it back so it carries a
    sealed manifest and the real at-rest ``encrypted`` flag."""
    bundle = tmp_path / wf.name
    if encrypt:
        wf.save(bundle, encrypt=True, key=_KEY)
        loaded = Workflow.load(bundle, key=_KEY)
    else:
        wf.save(bundle)
        loaded = Workflow.load(bundle)
    return loaded, bundle


def _deployment(
    *, verifier: bool = True, policy: str = "clinical-write"
) -> DeploymentConfig:
    effects = (
        EffectsConfig(kind="rest", base_url="http://sor.local")
        if verifier
        else EffectsConfig(kind="none")
    )
    return DeploymentConfig(effects=effects, policy=PolicySection(policy=policy))


def _run(
    wf: Workflow,
    bundle: Path,
    *,
    verifier: bool = True,
    **kw,
):
    dep = _deployment(verifier=verifier)
    return evaluate_run_gate(
        wf,
        bundle_dir=bundle,
        deployment=dep,
        effect_verifier=object() if verifier else None,
        **kw,
    )


# ---------------------------------------------------------------------------
# The pass path
# ---------------------------------------------------------------------------


def test_fully_covered_bundle_is_admitted(tmp_path):
    wf, bundle = _seal(_good_workflow(), tmp_path)
    report = _run(wf, bundle)
    assert report.passed, report.render()
    assert report.refusals == []
    # Every gate individually passed.
    assert all(g.passed for g in report.gates)


# ---------------------------------------------------------------------------
# Gate 1: certification
# ---------------------------------------------------------------------------


def test_uncertified_bundle_refused(tmp_path):
    # An unarmed navigation click + unarmed vacuous write: fails clinical-write.
    wf = Workflow(
        name="uncertified",
        steps=[
            _click("s0", "click 'Jane Doe'", ocr="Jane", armed=False),
            _click(
                "s1",
                "click 'Save'",
                ocr="Save",
                armed=False,
                risk="irreversible",
                effects=[],
                expect=[],
            ),
        ],
    )
    wf, bundle = _seal(wf, tmp_path)
    report = _run(wf, bundle)
    assert not report.passed
    g = report.gate(GATE_CERTIFICATION)
    assert g is not None and not g.passed
    assert "not certified" in g.detail.lower()


def test_unknown_policy_refused(tmp_path):
    wf, bundle = _seal(_good_workflow(), tmp_path)
    report = _run(wf, bundle, policy_source="no-such-policy")
    g = report.gate(GATE_CERTIFICATION)
    assert g is not None and not g.passed
    assert "could not be loaded" in g.detail


# ---------------------------------------------------------------------------
# Gate 2: identity coverage
# ---------------------------------------------------------------------------


def test_unarmed_consequential_click_refused(tmp_path):
    wf = _good_workflow("unarmed")
    # Disarm the irreversible write step's identity check.
    wf.steps[1].identity_armed = False
    wf.steps[1].anchor.context_text = None
    wf, bundle = _seal(wf, tmp_path)
    report = _run(wf, bundle)
    assert not report.passed
    g = report.gate(GATE_IDENTITY)
    assert g is not None and not g.passed
    assert "UNARMED" in g.detail
    assert "s1" in g.offenders


# ---------------------------------------------------------------------------
# Gate 3: effect coverage
# ---------------------------------------------------------------------------


def test_screen_only_write_refused(tmp_path):
    wf = _good_workflow("screen_only")
    # Strip the system-of-record effect: the write is verified by screen only.
    wf.steps[1].effects = []
    wf, bundle = _seal(wf, tmp_path)
    report = _run(wf, bundle)
    assert not report.passed
    g = report.gate(GATE_EFFECT)
    assert g is not None and not g.passed
    assert "SCREEN only" in g.detail
    assert "s1" in g.offenders


def test_unconfirmed_effect_binding_refused(tmp_path):
    wf = _good_workflow("unconfirmed")
    eff = _write_effect()
    eff.needs_operator_confirmation = True
    wf.steps[1].effects = [eff]
    wf, bundle = _seal(wf, tmp_path)
    report = _run(wf, bundle)
    g = report.gate(GATE_EFFECT)
    assert g is not None and not g.passed
    assert "UNCONFIRMED" in g.detail
    assert "s1" in g.offenders


# ---------------------------------------------------------------------------
# Gate 4: approval fallback
# ---------------------------------------------------------------------------


def test_unverifiable_write_without_approval_refused(tmp_path):
    # Bundle is fine, but the deployment configures NO verifier.
    wf, bundle = _seal(_good_workflow("no_verifier"), tmp_path)
    report = _run(wf, bundle, verifier=False)
    assert not report.passed
    g = report.gate(GATE_APPROVAL)
    assert g is not None and not g.passed
    assert "cannot be independently verified" in g.detail
    assert "s1" in g.offenders


def test_unverifiable_write_with_explicit_approval_admitted(tmp_path):
    wf, bundle = _seal(_good_workflow("approved"), tmp_path)
    report = _run(wf, bundle, verifier=False, approval_available=True)
    g = report.gate(GATE_APPROVAL)
    assert g is not None and g.passed
    assert "EXPLICITLY approved" in g.detail
    # With the fallback satisfied, the whole run is admitted.
    assert report.passed, report.render()


# ---------------------------------------------------------------------------
# Gate 5: encryption
# ---------------------------------------------------------------------------


def test_unencrypted_bundle_refused(tmp_path):
    wf, bundle = _seal(_good_workflow("plain"), tmp_path, encrypt=False)
    report = _run(wf, bundle)
    assert not report.passed
    g = report.gate(GATE_ENCRYPTION)
    assert g is not None and not g.passed
    assert "NOT encrypted" in g.detail


def test_unencrypted_allowed_with_escape_hatch(tmp_path):
    wf, bundle = _seal(_good_workflow("plain2"), tmp_path, encrypt=False)
    report = _run(wf, bundle, require_encryption=False)
    g = report.gate(GATE_ENCRYPTION)
    assert g is not None and g.passed
    assert report.passed, report.render()


def test_unsealed_templates_warn_by_default_refuse_when_strict(tmp_path):
    wf, bundle = _seal(_good_workflow("tmpl"), tmp_path)
    # Drop a plaintext template asset into the bundle.
    (bundle / "templates").mkdir(exist_ok=True)
    (bundle / "templates" / "s0.png").write_bytes(b"\x89PNG plaintext crop")

    warn = _run(wf, bundle)
    g = warn.gate(GATE_ENCRYPTION)
    assert g is not None and g.passed and g.warning
    assert warn.passed  # a warning does not fail the run

    strict = _run(wf, bundle, strict_templates=True)
    gs = strict.gate(GATE_ENCRYPTION)
    assert gs is not None and not gs.passed
    assert not strict.passed
    assert any("s0.png" in o for o in gs.offenders)


# ---------------------------------------------------------------------------
# Gate 6: sealed manifest + version pin
# ---------------------------------------------------------------------------


def test_tampered_manifest_refused(tmp_path):
    wf, bundle = _seal(_good_workflow("tampered"), tmp_path)
    # Corrupt the loaded manifest's sealed digest so re-verification fails.
    assert wf.manifest is not None
    wf.manifest.content_digest = "0" * 64
    report = _run(wf, bundle)
    g = report.gate(GATE_MANIFEST)
    assert g is not None and not g.passed
    assert "integrity FAILED" in g.detail


def test_missing_manifest_refused(tmp_path):
    wf, bundle = _seal(_good_workflow("nomanifest"), tmp_path)
    wf.manifest = None
    report = _run(wf, bundle)
    g = report.gate(GATE_MANIFEST)
    assert g is not None and not g.passed
    assert "no integrity-sealed manifest" in g.detail


def test_version_pin_mismatch_refused(tmp_path):
    wf, bundle = _seal(_good_workflow("pin"), tmp_path)
    report = _run(wf, bundle, pinned_compiler_version="99.99.99")
    g = report.gate(GATE_MANIFEST)
    assert g is not None and not g.passed
    assert "does not match the pinned version" in g.detail


def test_digest_pin_mismatch_refused(tmp_path):
    wf, bundle = _seal(_good_workflow("dpin"), tmp_path)
    report = _run(wf, bundle, pinned_content_digest="f" * 64)
    g = report.gate(GATE_MANIFEST)
    assert g is not None and not g.passed
    assert "pinned digest" in g.detail


def test_matching_version_pin_admitted(tmp_path):
    wf, bundle = _seal(_good_workflow("pinok"), tmp_path)
    assert wf.manifest is not None
    report = _run(
        wf,
        bundle,
        pinned_content_digest=wf.manifest.content_digest,
        pinned_compiler_version=wf.manifest.provenance.compiler_version or None,
    )
    g = report.gate(GATE_MANIFEST)
    assert g is not None and g.passed
    assert report.passed, report.render()


# ---------------------------------------------------------------------------
# CLI: `run` refuses without executing; `--dry-run` reports only
# ---------------------------------------------------------------------------


def _no_execute(monkeypatch):
    """Make _cmd_replay explode if ever called, so a refusal that still executes
    is caught."""
    import openadapt_flow.__main__ as main

    def boom(_args):  # pragma: no cover - must never run in these tests
        raise AssertionError("run executed a bundle it should have refused")

    monkeypatch.setattr(main, "_cmd_replay", boom)


def test_cli_run_refuses_uncertified_without_executing(tmp_path, monkeypatch, capsys):
    import openadapt_flow.__main__ as main

    _no_execute(monkeypatch)
    # A plaintext, uncertified bundle: refused at the first failing gate.
    wf = Workflow(
        name="cli_bad",
        steps=[_click("s0", "click 'Jane'", ocr="Jane", armed=False)],
    )
    _, bundle = _seal(wf, tmp_path, encrypt=False)

    parser = main.build_parser()
    args = parser.parse_args(["run", str(bundle), "--policy", "clinical-write"])
    rc = args.func(args)
    assert rc == 2
    out = capsys.readouterr().out
    assert "REFUSE" in out
    assert "Nothing was executed" in out


def test_cli_run_dry_run_reports_without_executing(tmp_path, monkeypatch, capsys):
    import openadapt_flow.__main__ as main

    _no_execute(monkeypatch)
    wf, bundle = _seal(_good_workflow("cli_good"), tmp_path)

    parser = main.build_parser()
    args = parser.parse_args(
        [
            "run",
            str(bundle),
            "--config",  # not used; policy via flag
            *[],
        ][:2]
        + [
            "--policy",
            "clinical-write",
            "--effects-kind",
            "rest",
            "--effects-base-url",
            "http://sor.local",
            "--dry-run",
        ]
    )
    # Provide the decryption key via the environment (bundle is encrypted).
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    rc = args.func(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ADMIT" in out
    # Dry-run must NOT execute even on an admitted bundle.
    assert "Replay" not in out


@pytest.mark.parametrize("encrypt", [True, False])
def test_cli_run_encryption_key_required_for_encrypted_bundle(
    tmp_path, monkeypatch, capsys, encrypt
):
    import openadapt_flow.__main__ as main

    _no_execute(monkeypatch)
    wf, bundle = _seal(
        _good_workflow(f"cli_key_{int(encrypt)}"), tmp_path, encrypt=encrypt
    )
    monkeypatch.delenv("OPENADAPT_BUNDLE_KEY", raising=False)

    parser = main.build_parser()
    args = parser.parse_args(
        [
            "run",
            str(bundle),
            "--policy",
            "clinical-write",
            "--effects-kind",
            "rest",
            "--effects-base-url",
            "http://sor.local",
            "--allow-unencrypted",  # isolate the KEY-at-load behavior from gate 5
            "--dry-run",
        ]
    )
    rc = args.func(args)
    out = capsys.readouterr().out
    if encrypt:
        # No key configured -> the encrypted bundle cannot even be loaded: refused.
        assert rc == 2
        assert "could not be loaded safely" in out
    else:
        assert rc == 0
        assert "ADMIT" in out

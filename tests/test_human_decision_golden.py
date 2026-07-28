"""The golden projection contract: what the engine ACTUALLY emits for a pause.

Everything downstream of a durable halt -- the runner-local phone portal and the
hosted dashboard -- renders a sentence per ``(task_kind, question.template)``
pair and a button per entry in ``allowed_actions``. Those consumers ship
hand-written demo and sample fixtures. Nothing anywhere asserted that a fixture
is a shape this engine can still emit.

That gap is not theoretical. A fixture stays schema-valid and digest-consistent
forever: both properties are computed FROM the fixture, so a fixture can be
internally perfect and describe last month's product. The mapping tables in
``console.human_decisions`` and the accept-list in ``console.attention`` have
both changed this cycle, and no downstream test could have noticed.

So this file writes down, from real engine output, exactly one row per halt
category the controller can classify:

    controller.classify_halt() -> category
      -> attention.attention_item()  (the accept-list, `_normal_category`)
      -> human_decisions.portable_remote_decision_task()  (the Cloud-bound
         projection: the mapping tables, the uncertain-delivery override, and
         the signed task with `presentation` already discarded)
      -> (task_kind, template, non-null safe slots, evidence keys, actions)

and asserts the committed artifact still matches. Change a mapping, drop a
category from the accept-list, add a template, widen ``safe_slots``, or extend
the action vocabulary, and this test goes red with a diff naming the field.

The artifact is also the contract a fixture is checked against. It is
closed-vocabulary metadata -- enum names, key names, and counts, no values, no
run content, no PHI -- so it is mechanism, not data, and belongs on the public
side of the source-availability boundary (``AGENTS.md`` 4.2).

Regenerate deliberately, never automatically::

    OPENADAPT_UPDATE_GOLDEN=1 pytest tests/test_human_decision_golden.py

and read the diff: every line of it is a change to what an operator is told.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from openadapt_types import (
    HumanDecisionAction,
    HumanDecisionQuestionTemplate,
    HumanDecisionRequiredAuthn,
    HumanDecisionTaskKind,
)

from openadapt_flow.console.attention import attention_item
from openadapt_flow.console.human_decisions import (
    _ACTION_MAP,
    _DEFAULT_TASK_COPY,
    _TASK_COPY,
    portable_remote_decision_task,
)
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.ir import (
    ActionDeliveryUncertainty,
    ActionKind,
    IdentityCheck,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.runtime.durable.attended import issue_attended_capability
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunManifest,
)
from openadapt_flow.runtime.durable.controller import classify_halt

GOLDEN_PATH = Path(__file__).parent / "golden" / "human_decision_tasks.json"
GOLDEN_SCHEMA = "openadapt.human-decision-golden/v1"

#: What an unresolved delivery replaces the category-derived question with.
#: Mirrors ``console.human_decisions._task_and_presentation``; the real
#: projections below prove it fires.
_DELIVERY_OVERRIDE = {
    "task_kind": "delivery_uncertain",
    "question_template": "review_uncertain_delivery",
}

#: One representative failure per halt category, in ``classify_halt`` match
#: order. Each specimen is asserted to still classify as its category below, so
#: a change to the classifier cannot leave this table quietly generating the
#: wrong rows. The strings are ordinary engine error text, not fixtures of
#: protected content.
_SPECIMENS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "delivery_uncertain",
        {
            "error": "backend error after the action may have been dispatched",
            "delivery_uncertainty": ActionDeliveryUncertainty(
                operation="click",
                native=True,
                observed_at="2026-07-27T12:00:00+00:00",
                cause_type="transport_error",
            ),
        },
    ),
    ("human_required", {"error": "a CAPTCHA challenge is on screen"}),
    (
        "placeholder_effect",
        {
            "error": "system of record effect is a placeholder",
            "effect_verified": False,
        },
    ),
    (
        "effect_unverifiable",
        {
            "error": "no EffectVerifier is configured for this system of record",
            "effect_verified": False,
        },
    ),
    (
        "effect_escalated",
        {
            "error": "system of record compensation escalated",
            "effect_verified": False,
        },
    ),
    (
        "effect_indeterminate",
        {
            "error": "system of record result is indeterminate",
            "effect_verified": False,
        },
    ),
    (
        "effect_refuted",
        {
            "error": "system of record readback refuted the screen",
            "effect_verified": False,
        },
    ),
    ("unmet_guard", {"error": "step precondition was not satisfied"}),
    ("disambiguation", {"error": "the compiled locator is ambiguous"}),
    (
        "identity",
        {
            "error": "identity band could not be certified; refusing to act",
            "identity": IdentityCheck(status="abstain"),
        },
    ),
    ("postcondition", {"error": "postconditions failed after the action"}),
    ("resolution", {"error": "all resolution rungs failed; could not resolve"}),
    ("halt", {"error": "the run stopped without a more specific cause"}),
)

_REMOTE_DEPLOYMENT = DeploymentConfig.model_validate(
    {
        "human_decisions": {
            "remote": {
                "enabled": True,
                "tenant_id": "tenant_golden_0001",
                "runner_id": "runner_golden_0001",
            }
        }
    }
)


def _workflow() -> Workflow:
    return Workflow(
        name="human-decision-golden",
        steps=[
            Step(id="one", intent="the paused step", action=ActionKind.KEY, key="A"),
            Step(id="two", intent="the next step", action=ActionKind.KEY, key="B"),
        ],
    )


def _paused_run(tmp_path: Path, category: str, fields: dict[str, Any]) -> Path:
    """Write one real durable pause whose halt classifies as ``category``."""
    workflow = _workflow()
    bundle = tmp_path / "bundles" / category
    run = tmp_path / "runs" / category
    workflow.save(bundle)
    store = CheckpointStore(run)
    store.write_manifest(
        RunManifest(
            run_id=f"run-golden-{category}",
            workflow_name=workflow.name,
            bundle_dir=str(bundle),
            params={},
        )
    )
    result = StepResult(step_id="one", intent="the paused step", ok=False, **fields)
    # The classifier is the authority on the category, not this table.
    assert classify_halt(workflow.steps[0], result)[0] == category, category
    pending = PendingEscalation(
        workflow_name=workflow.name,
        step_index=0,
        step_id="one",
        intent="the paused step",
        category=category,
        reason=result.error or "",
        resume_from_index=0,
    )
    store.write_pending(pending)
    RunReport(
        workflow_name=workflow.name,
        started_at="2026-07-27T12:00:00+00:00",
        success=False,
        results=[result],
    ).save(run)
    issue_attended_capability(
        run, store=store, pending=pending, workflow=workflow, result=result
    )
    return run


def _non_null(mapping: dict[str, Any]) -> list[str]:
    return sorted(key for key, value in mapping.items() if value is not None)


def _row(tmp_path: Path, category: str, fields: dict[str, Any]) -> dict[str, Any]:
    run = _paused_run(tmp_path, category, fields)
    item = attention_item(run.parent, run)
    assert item is not None, category
    # The exact projection Cloud receives: `portable_remote_decision_task`
    # builds it from the signed task alone and discards `presentation` in the
    # same function, so nothing below can describe evidence the dashboard is
    # not entitled to.
    projection = portable_remote_decision_task(run, item, deployment=_REMOTE_DEPLOYMENT)
    task = projection.task.model_dump(mode="json")
    return {
        "halt_category": category,
        # What `attention._normal_category` made of the engine's own category.
        # A category the accept-list does not know becomes `operator_review`
        # here and loses its engine-derived question.
        "projected_category": item.category,
        "delivery_state": task["delivery_state"],
        "task_kind": task["task_kind"],
        "question_template": task["question"]["template"],
        # Which bounded counts this halt actually carries.
        "non_null_safe_slots": _non_null(task["question"]["safe_slots"]),
        "allowed_actions": list(task["allowed_actions"]),
        "required_authn": task["required_authn"],
        "_safe_slot_fields": sorted(task["question"]["safe_slots"]),
        "_evidence_fields": sorted(task["evidence"]),
    }


def _one(values: list[Any], label: str) -> Any:
    """Collapse a field that must be identical across every projection."""
    assert len({json.dumps(value) for value in values}) == 1, (
        f"{label} is no longer constant across projected tasks: {values!r}"
    )
    return values[0]


def _generate(tmp_path: Path) -> dict[str, Any]:
    rows = [_row(tmp_path, category, fields) for category, fields in _SPECIMENS]
    # The category -> question mapping, read from the projection's own table
    # rather than from the rows, so the mapping is visible even for the
    # categories whose kind is replaced by the uncertain-delivery override.
    mapping = {
        category: {"task_kind": kind, "question_template": template}
        for category, (kind, template, _question) in sorted(_TASK_COPY.items())
    }
    default_kind, default_template, _ = _DEFAULT_TASK_COPY
    kinds = {entry["task_kind"] for entry in mapping.values()}
    templates = {entry["question_template"] for entry in mapping.values()}
    kinds |= {default_kind, _DELIVERY_OVERRIDE["task_kind"]}
    templates |= {default_template, _DELIVERY_OVERRIDE["question_template"]}
    safe_slot_fields = _one(
        [row.pop("_safe_slot_fields") for row in rows], "safe_slot_fields"
    )
    evidence_fields = _one(
        [row.pop("_evidence_fields") for row in rows], "evidence_fields"
    )
    return {
        "schema": GOLDEN_SCHEMA,
        "task_schema_version": "openadapt.human-decision-task/v1",
        # Generated by tests/test_human_decision_golden.py. Do not hand-edit:
        # regenerate with OPENADAPT_UPDATE_GOLDEN=1 and read the diff.
        "action_vocabulary": list(_ACTION_MAP.values()),
        "required_authn_vocabulary": sorted(
            member.value for member in HumanDecisionRequiredAuthn
        ),
        "safe_slot_fields": safe_slot_fields,
        # Slots that were null in EVERY projection above. `candidate_count` is
        # here: the engine has never populated it, so any surface that says
        # "found N matches" is asserting a number it was not given.
        "never_populated_safe_slots": sorted(
            set(safe_slot_fields).difference(
                *[set(row["non_null_safe_slots"]) for row in rows]
            )
            if rows
            else safe_slot_fields
        ),
        "evidence_fields": evidence_fields,
        "category_question_map": mapping,
        "default_category_question": {
            "task_kind": default_kind,
            "question_template": default_template,
        },
        # An unresolved delivery replaces the kind and template above, whatever
        # the halt category was.
        "delivery_uncertain_override": dict(_DELIVERY_OVERRIDE),
        "emitted_task_kinds": sorted(kinds),
        "never_emitted_task_kinds": sorted(
            {member.value for member in HumanDecisionTaskKind} - kinds
        ),
        "emitted_question_templates": sorted(templates),
        "never_emitted_question_templates": sorted(
            {member.value for member in HumanDecisionQuestionTemplate} - templates
        ),
        "halts": rows,
    }


def test_the_golden_projection_still_matches_what_the_engine_emits(tmp_path):
    """One row per halt category, regenerated from real pauses, byte-compared.

    This is the check a demo fixture is measured against. If it fails, the
    engine's operator-facing vocabulary moved: update the artifact deliberately
    and update every consumer that ships a fixture in the same change.
    """
    generated = _generate(tmp_path)
    if os.environ.get("OPENADAPT_UPDATE_GOLDEN"):
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(generated, indent=2) + "\n")
        pytest.skip("golden artifact regenerated; re-run without the flag")
    assert GOLDEN_PATH.is_file(), f"missing golden artifact: {GOLDEN_PATH}"
    committed = json.loads(GOLDEN_PATH.read_text())

    # Key the halt rows by category so a reordering is not a diff but a missing
    # or renamed category is.
    for document in (generated, committed):
        document["halts"] = {
            row["halt_category"]: {
                field: value for field, value in row.items() if field != "halt_category"
            }
            for row in document["halts"]
        }
    _assert_no_drift("", generated, committed)


def _assert_no_drift(path: str, generated: Any, committed: Any) -> None:
    """Walk both documents together so a failure names the field that moved."""
    if isinstance(generated, dict) and isinstance(committed, dict):
        for key in sorted(set(generated) | set(committed)):
            where = f"{path}.{key}" if path else key
            assert key in committed, (
                f"golden drift at {where}: the engine emits this now and the "
                "artifact does not have it"
            )
            assert key in generated, (
                f"golden drift at {where}: the artifact still has this and the "
                "engine no longer emits it"
            )
            _assert_no_drift(where, generated[key], committed[key])
        return
    assert generated == committed, (
        f"golden drift at {path or '<root>'}: the engine now emits "
        f"{generated!r}, the artifact still says {committed!r}. Regenerate with "
        "OPENADAPT_UPDATE_GOLDEN=1 and update every fixture that depends on it."
    )


def test_the_action_vocabulary_is_exactly_the_portable_enum():
    """A fixture may only advertise an action both sides of the wire know.

    ``_ACTION_MAP`` is a bijection between the engine's internal names and the
    portable enum. A new action must appear in both, and in the golden
    artifact, before any fixture may show a button for it.
    """
    assert sorted(_ACTION_MAP.values()) == sorted(
        member.value for member in HumanDecisionAction
    )
    committed = json.loads(GOLDEN_PATH.read_text())
    assert committed["action_vocabulary"] == list(_ACTION_MAP.values())


def test_teach_and_escalate_are_offered_at_every_pause(tmp_path):
    """The two always-available actions, proven from real capabilities.

    ``runtime.durable.attended._allowed_actions`` seeds every action set with
    ``teach`` and ``escalate``; ``continue`` and ``skip`` are conditional on the
    step's own semantics. A projected task that offered neither would mean an
    operator with no way to hand the pause on.
    """
    for category, fields in _SPECIMENS:
        row = _row(tmp_path, category, fields)
        assert {"teach", "escalate"} <= set(row["allowed_actions"]), category


def test_a_replayer_driven_halt_matches_the_golden_row_for_its_category(tmp_path):
    """The anchor: a halt nobody hand-built still lands on a golden row.

    Every other row above starts from a written ``PendingEscalation``, which is
    what makes thirteen categories cheap. This one starts from
    ``Replayer(durable=True).run(...)`` against the acceptance test's own
    workflow, so if the hand-written pause path ever stopped resembling the
    path a real run takes, the two would disagree here.
    """
    from tests.test_mobile_attended_decision_e2e import _halt

    _wf, _bundles, runs_root, _bundle, run, _capability = _halt(tmp_path)
    item = attention_item(runs_root, run)
    assert item is not None
    task = portable_remote_decision_task(
        run, item, deployment=_REMOTE_DEPLOYMENT
    ).task.model_dump(mode="json")

    committed = json.loads(GOLDEN_PATH.read_text())
    rows = {row["halt_category"]: row for row in committed["halts"]}
    assert item.category in rows, (
        f"a real replayer halt projected category {item.category!r}, which the "
        "golden artifact does not cover"
    )
    row = rows[item.category]
    assert task["task_kind"] == row["task_kind"]
    assert task["question"]["template"] == row["question_template"]
    assert _non_null(task["question"]["safe_slots"]) == row["non_null_safe_slots"]
    assert sorted(task["question"]["safe_slots"]) == committed["safe_slot_fields"]
    assert sorted(task["evidence"]) == committed["evidence_fields"]
    assert set(task["allowed_actions"]) <= set(committed["action_vocabulary"])

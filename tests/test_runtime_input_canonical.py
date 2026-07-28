"""Exact parsing contracts for governed runtime-input artifacts."""

from __future__ import annotations

import json

import pytest

from openadapt_flow.ir import Interstitial, Predicate, PredicateKind, Workflow
from openadapt_flow.runtime.authorization import (
    parse_runtime_inputs_bytes,
    runtime_inputs_bytes,
)


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _blocking_interstitial() -> Interstitial:
    return Interstitial(
        name="unexpected dialog",
        detect=Predicate(kind=PredicateKind.TEXT_PRESENT, text="Review required"),
    )


def test_parser_accepts_exact_serialized_interstitial() -> None:
    artifact = runtime_inputs_bytes(
        Workflow(name="runtime-input", steps=[]),
        {"record_id": "42"},
        None,
        interstitials=[_blocking_interstitial()],
    )

    params, worklists = parse_runtime_inputs_bytes(artifact)

    assert params == {"record_id": "42"}
    assert worklists == {}


@pytest.mark.parametrize(
    "interstitials",
    [
        [{}],
        [42],
        [{**_blocking_interstitial().model_dump(mode="json"), "extra": True}],
    ],
)
def test_parser_rejects_invalid_or_noncanonical_interstitial_items(
    interstitials: list[object],
) -> None:
    artifact = _canonical(
        {"params": {}, "worklists": {}, "interstitials": interstitials}
    )

    with pytest.raises(ValueError, match="interstitials"):
        parse_runtime_inputs_bytes(artifact)

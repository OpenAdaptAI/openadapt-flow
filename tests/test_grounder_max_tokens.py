"""``OpenAICompatibleGrounder.max_tokens`` — the completion-token budget knob.

Added after the first hosted real-model run (2026-08-12, Together AI,
``benchmark/hosted_grounder_probe/``): the previously fixed ``max_tokens: 256``
made the hosted reasoning model ``Qwen/Qwen3.5-9B`` truncate mid-reasoning with
EMPTY content on 11/12 dense-list targets — every one a (safe) abstain, but a
100% availability loss. The budget is now a constructor parameter; these tests
pin it without touching the existing wire contract
(``tests/test_grounder_openai_compatible_contract.py``):

* the default stays exactly 256 (the historical wire value);
* an explicit budget rides the wire verbatim;
* a non-positive / non-int budget is rejected at construction (fail loud at
  wiring time, not with a silent per-call 4xx => abstain at run time);
* SAFETY UNCHANGED: a truncated (empty-content) reply abstains — the knob can
  raise availability, never risk.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from openadapt_flow.runtime.grounder import OpenAICompatibleGrounder


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Minimal client capturing the request body (the wire payload)."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.bodies: list[dict] = []

    def post(
        self,
        url: str,
        *,
        json: Any = None,  # noqa: A002 - mirrors httpx signature
        headers: Any = None,
        timeout: Any = None,
    ) -> _FakeResponse:
        self.bodies.append(json)
        return _FakeResponse({"choices": [{"message": {"content": self._content}}]})


def _locate(client: _FakeClient, **kwargs: Any) -> Optional[Any]:
    g = OpenAICompatibleGrounder(
        base_url="http://127.0.0.1:1/v1", model="m", client=client, **kwargs
    )
    return g.locate(b"png", "click Open for patient X (MRN 1)", "Open")


class TestMaxTokensOnTheWire:
    def test_default_budget_is_exactly_256(self) -> None:
        client = _FakeClient('{"x": 1, "y": 2}')
        assert _locate(client) is not None
        assert client.bodies[0]["max_tokens"] == 256

    def test_explicit_budget_rides_the_wire_verbatim(self) -> None:
        client = _FakeClient('{"x": 1, "y": 2}')
        assert _locate(client, max_tokens=2048) is not None
        assert client.bodies[0]["max_tokens"] == 2048
        # And it is valid JSON on the wire, not merely a Python object.
        assert json.loads(json.dumps(client.bodies[0]))["max_tokens"] == 2048


class TestMaxTokensValidation:
    @pytest.mark.parametrize("bad", [0, -1, -256])
    def test_non_positive_budget_is_rejected_at_construction(self, bad: int) -> None:
        with pytest.raises(ValueError, match="max_tokens"):
            OpenAICompatibleGrounder(
                base_url="http://127.0.0.1:1/v1", model="m", max_tokens=bad
            )

    @pytest.mark.parametrize("bad", [256.0, "256", None, True])
    def test_non_int_budget_is_rejected_at_construction(self, bad: Any) -> None:
        with pytest.raises(ValueError, match="max_tokens"):
            OpenAICompatibleGrounder(
                base_url="http://127.0.0.1:1/v1", model="m", max_tokens=bad
            )


class TestTruncationStaysFailSafe:
    def test_truncated_empty_content_reply_abstains(self) -> None:
        # The measured hosted failure shape: finish_reason=length, content "".
        client = _FakeClient("")
        assert _locate(client, max_tokens=1) is None

    def test_truncated_partial_json_reply_abstains(self) -> None:
        client = _FakeClient('{"x": 12')  # cut off mid-object by the budget
        assert _locate(client, max_tokens=8) is None

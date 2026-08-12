"""Wire-protocol contract for ``OpenAICompatibleGrounder`` — real HTTP.

``tests/test_byo_grounding_model.py`` covers the adapter against a *fake
client object*, which proves the parsing logic but never exercises the real
transport. This module stands up an in-process loopback HTTP server speaking
the OpenAI chat-completions shape (the same stdlib pattern as
``tests/test_effect_verifier_transport.py``) and drives the grounder through
its REAL default path — module-level ``httpx.post`` over TCP — so the exact
bytes on the wire are pinned:

* the request is a well-formed ``POST {base_url}/chat/completions`` JSON body
  carrying the ``model`` id, the screenshot as a ``data:image/png;base64,``
  URL that round-trips byte-for-byte, and a text prompt containing the target
  intent and label;
* a valid coordinate reply resolves to exactly that point;
* a refusing, malformed, ``{"x": null}``, or non-JSON reply => None (abstain);
* an unreachable endpoint or non-2xx => None, never an exception;
* WEAKEST AUTHORITY: the model's reply can only ever *propose*. The resolver
  files a grounder proposal on the ``grounder`` rung — the bottom of
  ``RUNG_ORDER``, below ``ocr`` — so the risk gate (``is_below_ocr``) refuses
  it for irreversible steps; the reply cannot raise its own confidence, add
  fields, or reach any authority-bearing surface.

CI-safe: loopback only, stdlib server, no model, no network egress.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from openadapt_flow.runtime.grounder import (
    GrounderMatch,
    OpenAICompatibleGrounder,
    component_may_egress,
)
from openadapt_flow.runtime.resolver import RUNG_ORDER, is_below_ocr

# A tiny but real PNG (1x1 white pixel) so the data: URL carries genuine
# image/png bytes end to end.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

INTENT = "click Open in the row for patient Halloran, Karen MRN MG584224"
LABEL = "Open"


class _Server:
    """Loopback OpenAI-compatible chat-completions endpoint.

    Records every request (path, headers, parsed JSON body) and returns the
    configured status/body, so tests assert on what actually crossed the wire.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.body: Any = {}
        self.raw_body: bytes | None = None  # overrides ``body`` when set

        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
                outer.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "json": parsed,
                    }
                )
                payload = (
                    outer.raw_body
                    if outer.raw_body is not None
                    else json.dumps(outer.body).encode()
                )
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def reply(self, content: Any) -> None:
        """Configure a 200 chat-completions reply with ``content``."""
        self.status = 200
        self.raw_body = None
        self.body = {"choices": [{"message": {"content": content}}]}

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture()
def server() -> Iterator[_Server]:
    srv = _Server()
    try:
        yield srv
    finally:
        srv.close()


def _grounder(server: _Server, **kwargs: Any) -> OpenAICompatibleGrounder:
    kwargs.setdefault("timeout", 5.0)
    return OpenAICompatibleGrounder(
        base_url=server.base_url, model="qwen3-vl:8b", **kwargs
    )


# --------------------------------------------------------------------------
# (a) The request on the wire is well-formed.
# --------------------------------------------------------------------------


class TestRequestShape:
    def test_request_is_a_wellformed_chat_completions_post(
        self, server: _Server
    ) -> None:
        server.reply('{"x": 10, "y": 20}')
        _grounder(server).locate(PNG_1PX, INTENT, LABEL)

        assert len(server.requests) == 1
        req = server.requests[0]
        assert req["path"] == "/v1/chat/completions"
        assert req["headers"]["Content-Type"].startswith("application/json")

        body = req["json"]
        assert body["model"] == "qwen3-vl:8b"
        assert isinstance(body["max_tokens"], int)
        (message,) = body["messages"]
        assert message["role"] == "user"

        parts = {p["type"]: p for p in message["content"]}
        assert set(parts) == {"image_url", "text"}

        # The screenshot rides as a data: URL and round-trips byte-for-byte.
        url = parts["image_url"]["image_url"]["url"]
        prefix = "data:image/png;base64,"
        assert url.startswith(prefix)
        assert base64.b64decode(url[len(prefix) :]) == PNG_1PX

        # The prompt names the target: intent and label both present.
        assert INTENT in parts["text"]["text"]
        assert LABEL in parts["text"]["text"]

    def test_no_api_key_sends_no_authorization_header(self, server: _Server) -> None:
        server.reply('{"x": 1, "y": 2}')
        _grounder(server).locate(PNG_1PX, INTENT, LABEL)
        assert "Authorization" not in server.requests[0]["headers"]

    def test_api_key_rides_as_bearer_on_the_wire(self, server: _Server) -> None:
        server.reply('{"x": 1, "y": 2}')
        _grounder(server, api_key="sekret").locate(PNG_1PX, INTENT, LABEL)
        assert server.requests[0]["headers"]["Authorization"] == "Bearer sekret"


# --------------------------------------------------------------------------
# (b) A valid coordinate reply resolves to exactly that point.
# --------------------------------------------------------------------------


class TestValidReply:
    def test_coordinate_reply_resolves_to_the_point(self, server: _Server) -> None:
        server.reply('{"x": 123, "y": 45}')
        match = _grounder(server).locate(PNG_1PX, INTENT, LABEL)
        assert isinstance(match, GrounderMatch)
        assert match.point == (123, 45)
        x0, y0, w, h = match.region
        assert x0 <= 123 <= x0 + w and y0 <= 45 <= y0 + h

    def test_content_parts_list_shape_is_accepted(self, server: _Server) -> None:
        server.reply([{"type": "text", "text": 'sure: {"x": 7, "y": 9}'}])
        match = _grounder(server).locate(PNG_1PX, INTENT, LABEL)
        assert match is not None and match.point == (7, 9)


# --------------------------------------------------------------------------
# (c) A refusing / malformed reply abstains — never a guessed point.
# --------------------------------------------------------------------------


class TestBadReplyAbstains:
    @pytest.mark.parametrize(
        "content",
        [
            "I cannot locate that control on this screen.",  # NL refusal
            '{"x": null, "y": null}',  # explicit not-visible
            '{"x": "twelve", "y": 4}',  # non-numeric
            '{"x": NaN, "y": 4}',  # not JSON at all
            "",  # empty content
        ],
    )
    def test_unusable_content_abstains(self, server: _Server, content: str) -> None:
        server.reply(content)
        assert _grounder(server).locate(PNG_1PX, INTENT, LABEL) is None

    def test_non_json_response_body_abstains(self, server: _Server) -> None:
        server.raw_body = b"<html>gateway error</html>"
        assert _grounder(server).locate(PNG_1PX, INTENT, LABEL) is None

    def test_wrong_response_shape_abstains(self, server: _Server) -> None:
        server.body = {"choices": []}
        assert _grounder(server).locate(PNG_1PX, INTENT, LABEL) is None

    def test_non_200_abstains(self, server: _Server) -> None:
        server.status = 503
        server.body = {"error": "overloaded"}
        assert _grounder(server).locate(PNG_1PX, INTENT, LABEL) is None


# --------------------------------------------------------------------------
# (d) Transport failure abstains — never a crash.
# --------------------------------------------------------------------------


class TestTransportFailure:
    def test_unreachable_endpoint_returns_none(self) -> None:
        # Grab an ephemeral port with nothing listening on it.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        g = OpenAICompatibleGrounder(
            base_url=f"http://127.0.0.1:{port}/v1", model="m", timeout=2.0
        )
        assert g.locate(PNG_1PX, INTENT, LABEL) is None  # and does not raise


# --------------------------------------------------------------------------
# (e) Weakest authority: the reply can only ever PROPOSE.
# --------------------------------------------------------------------------


class TestWeakestAuthority:
    def test_reply_cannot_inflate_its_own_confidence(self, server: _Server) -> None:
        # Even a reply claiming certainty and extra authority fields yields the
        # fixed grounder confidence and nothing else.
        server.reply('{"x": 5, "y": 6, "confidence": 0.99, "authorized": true}')
        match = _grounder(server).locate(PNG_1PX, INTENT, LABEL)
        assert match is not None
        assert match.confidence == 0.5

    def test_match_carries_no_authority_bearing_fields(self, server: _Server) -> None:
        server.reply('{"x": 5, "y": 6}')
        match = _grounder(server).locate(PNG_1PX, INTENT, LABEL)
        assert match is not None
        assert set(GrounderMatch.model_fields) == {"point", "region", "confidence"}

    def test_grounder_rung_is_bottom_of_the_ladder_and_risk_gated(self) -> None:
        # The resolver files a grounder proposal on the "grounder" rung: the
        # weakest evidence class. The risk gate refuses it for irreversible
        # steps, so a model reply can never authorize an irreversible action.
        assert RUNG_ORDER[-1] == "grounder"
        assert is_below_ocr("grounder") is True

    def test_grounder_is_declared_egress_capable(self, server: _Server) -> None:
        # The egress gate keys off MAY_EGRESS: the replayer refuses to wire
        # this grounder unless the operator explicitly opts in.
        assert component_may_egress(_grounder(server)) is True

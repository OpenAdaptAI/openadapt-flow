"""Instrumented MockMed server for the competitor drift study.

Serves the UNMODIFIED MockMed static app (``openadapt_flow/mockmed/static``)
with two scripts injected into ``index.html`` at serve time:

1. **Drift injection** (before ``app.js``): rewrites ``location.search`` via
   ``history.replaceState`` so the app reads the server-configured
   ``?drift=...`` mode. This lets third-party tools record and replay against
   a CONSTANT URL while the server decides the drift condition per run --
   exactly like a real backend whose data drifted between runs. The app's own
   drift semantics (``openadapt_flow/mockmed/static/app.js``) are untouched.

2. **State beacon** (after ``app.js``): MockMed state is client-side and dies
   with the tool's browser, so ground truth must be captured in-flight. The
   beacon polls ``location.hash``, the saved banner, and the app's
   ``state.encounters`` / ``state.banner`` (top-level ``var``s, reachable as
   window properties) and POSTs a JSON snapshot to ``/__state`` on every
   change. The server appends each snapshot as a JSON line to the state file.

Neither script changes pixels, layout, or DOM structure of the app, so every
tool under test sees the same app our own validation suite tested.

Usage:
    python scripts/competitor_study/mockmed_study_server.py \
        --port 8765 --drift lookalike --state-file /tmp/run1.state.jsonl

The verdict module (``verdict.py``) reads the state file after the tool run.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "openadapt_flow" / "mockmed" / "static"
)

DRIFT_SNIPPET = """<script>
(function () {
  var DRIFT = %(drift)s;
  if (DRIFT) {
    // Make app.js (which reads ?drift= exactly once at load) see the
    // server-configured drift mode WITHOUT touching the URL: the page URL
    // stays byte-identical to what every tool recorded, exactly like a real
    // backend whose data drifted under a constant address.
    var origGet = URLSearchParams.prototype.get;
    URLSearchParams.prototype.get = function (k) {
      var v = origGet.call(this, k);
      if (k === 'drift' && (v === null || v === '')) { return DRIFT; }
      return v;
    };
  }
})();
</script>
"""

BEACON_SNIPPET = """<script>
(function () {
  var last = '';
  function snap(final) {
    var st = window.state || {};
    var banner = document.getElementById('saved-banner');
    var payload = {
      ts: Date.now(),
      hash: location.hash,
      banner: banner ? banner.textContent : null,
      encounters: st.encounters || {},
      state_banner: st.banner || null,
      final: !!final
    };
    var s = JSON.stringify(payload);
    if (s !== last || final) {
      last = s;
      try {
        fetch('/__state', {method: 'POST', body: s, keepalive: true})
          .catch(function () {});
      } catch (e) {
        try { navigator.sendBeacon('/__state', s); } catch (e2) {}
      }
    }
  }
  setInterval(snap, 150);
  window.addEventListener('pagehide', function () { snap(true); });
})();
</script>
"""

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def build_handler(drift: str, state_file: Path):
    drift_js = json.dumps(drift or "")
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            pass

        def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                html = (STATIC_DIR / "index.html").read_text()
                html = html.replace(
                    '<script src="app.js"></script>',
                    DRIFT_SNIPPET % {"drift": drift_js}
                    + '<script src="app.js"></script>\n'
                    + BEACON_SNIPPET,
                )
                self._send(html.encode(), CONTENT_TYPES[".html"])
                return
            if path == "/__state":
                with lock:
                    data = (
                        state_file.read_bytes() if state_file.exists() else b""
                    )
                self._send(data, "application/x-ndjson")
                return
            target = (STATIC_DIR / path.lstrip("/")).resolve()
            if not str(target).startswith(str(STATIC_DIR)) or not target.is_file():
                self._send(b"not found", "text/plain", 404)
                return
            ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
            self._send(target.read_bytes(), ctype)

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/__state":
                self._send(b"not found", "text/plain", 404)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                event = json.loads(body)
            except json.JSONDecodeError:
                self._send(b"bad json", "text/plain", 400)
                return
            event["received_at"] = time.time()
            with lock:
                with state_file.open("a") as f:
                    f.write(json.dumps(event) + "\n")
            self._send(b"ok", "text/plain", 202)

    return Handler


def serve(port: int, drift: str, state_file: Path) -> ThreadingHTTPServer:
    """Start the study server in a daemon thread; returns the server object."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port), build_handler(drift, state_file)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--drift", default="", help="MockMed drift mode")
    parser.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args()
    serve(args.port, args.drift, args.state_file)
    print(f"study server on http://127.0.0.1:{args.port}/ drift={args.drift!r}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

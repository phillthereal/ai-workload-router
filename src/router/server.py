"""OpenAI-compatible HTTP endpoint (stdlib only) in front of ``Router``.

Point any OpenAI SDK's ``base_url`` at ``http://127.0.0.1:8787/v1``.

Limitations: the chat ``messages`` are flattened into ONE labelled prompt, so
the routing decision is single-turn (no per-turn routing); no streaming.
The ``model`` field selects the routing policy, not a vendor model.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .api import (
    CascadePolicy, LearnedPolicy, FrontierOnlyPolicy, ProviderError, Router, RulesPolicy,
)
from .config import ROSTERS

MAX_BODY = 1_000_000  # 1 MB
SELECTORS = {
    "router": lambda: RulesPolicy(use_labels=False),
    "auto": lambda: RulesPolicy(use_labels=False),
    "router:cascade": CascadePolicy,
    "router:learned": lambda: LearnedPolicy(use_labels=False),
    "router:frontier": FrontierOnlyPolicy,
}


def flatten(messages) -> str:
    """system + user/assistant turns -> one labelled prompt. Raises ValueError."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("'messages' must be a non-empty array")
    parts = []
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("role"), str):
            raise ValueError("each message needs a 'role'")
        c = m.get("content")
        if isinstance(c, list):  # SDK content parts: [{"type":"text","text":...}]
            c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
        if not isinstance(c, str):
            raise ValueError("message 'content' must be a string or text parts")
        parts.append(f"[{m['role']}]\n{c}")
    return "\n\n".join(parts)


def make_handler(roster: str, allow_mock: bool, token: Optional[str], log_db=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "awr-router"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # never log paths/bodies/headers
            pass

        def _send(self, code, obj, headers=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code, msg, typ="invalid_request_error"):
            self._send(code, {"error": {"message": msg, "type": typ, "param": None, "code": None}})

        def _authed(self) -> bool:
            if not token:
                return True
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                return True
            self._err(401, "Invalid or missing bearer token", "authentication_error")
            return False

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/healthz":
                return self._send(200, {"status": "ok"})
            if not self._authed():
                return
            if path == "/v1/models":
                data = [{"id": s, "object": "model", "created": 0, "owned_by": "ai-workload-router"}
                        for s in SELECTORS]
                return self._send(200, {"object": "list", "data": data})
            self._err(404, "Not found")

        def _read_json(self):
            """Size-limited JSON object body, or None after sending the error."""
            try:
                n = int(self.headers.get("Content-Length", ""))
                assert n >= 0
            except (ValueError, AssertionError):
                return self._err(400, "Content-Length required")
            if n > MAX_BODY:
                self.close_connection = True
                left = min(n, 8 * MAX_BODY)  # drain a bounded amount so the client sees the 413
                while left > 0:
                    chunk = self.rfile.read(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                return self._err(413, "Request body too large (max 1 MB)")
            try:
                req = json.loads(self.rfile.read(n))
                assert isinstance(req, dict)
            except Exception:
                return self._err(400, "Request body must be a JSON object")
            return req

        def do_POST(self):
            path = self.path.split("?")[0]
            if path not in ("/v1/chat/completions", "/v1/feedback"):
                return self._err(404, "Not found")
            if not self._authed():
                return
            req = self._read_json()
            if req is None:
                return
            if path == "/v1/feedback":
                return self._feedback(req)
            if req.get("stream"):
                return self._err(400, "stream=true is not supported")
            sel = req.get("model")
            if sel not in SELECTORS:
                return self._err(400, f"Unknown model; use one of: {', '.join(SELECTORS)}")
            try:
                prompt = flatten(req.get("messages"))
            except ValueError as e:
                return self._err(400, str(e))
            try:
                r = Router(SELECTORS[sel](), roster_name=roster,
                           allow_mock_fallback=allow_mock, log_to=log_db).complete(prompt)
            except ProviderError:
                return self._err(502, "Upstream providers failed", "api_error")
            except Exception:
                return self._err(500, "Internal error", "api_error")
            u = r.response
            pt, ct = (u.input_tokens, u.output_tokens) if u else (0, 0)
            self._send(200, {
                "id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion",
                "created": int(time.time()), "model": r.model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": r.text}}],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
                "x_router": {"model_chosen": r.model, "policy": sel,
                             "answer_cost_usd": r.answer_cost_usd,
                             "routing_cost_usd": r.routing_cost_usd,
                             "trace": r.trace, "simulated": r.simulated,
                             "request_id": r.request_id},
            }, {"X-Router-Model": r.model})

        def _feedback(self, req):
            """{request_id, score in [0,1]} -> the learned router's live signal."""
            if not log_db:
                return self._err(400, "Feedback needs the server started with --log-db")
            try:
                Router(FrontierOnlyPolicy(), log_to=log_db).feedback(
                    req.get("request_id"), req.get("score"), source="user")
            except KeyError:
                return self._err(404, "Unknown request_id")
            except (ValueError, TypeError) as e:
                return self._err(400, str(e))
            self._send(200, {"status": "recorded"})

    return Handler


def make_server(host="127.0.0.1", port=8787, roster="cross_vendor", allow_mock=False,
                token: Optional[str] = None, log_db=None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(roster, allow_mock, token, log_db))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="OpenAI-compatible router endpoint")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--roster", default="cross_vendor", choices=sorted(ROSTERS))
    p.add_argument("--allow-mock", action="store_true", help="offline demo: mock on provider failure")
    p.add_argument("--log-db", default=None, metavar="PATH",
                   help="log requests (incl. prompt text) to this SQLite db and enable "
                        "POST /v1/feedback; off by default")
    a = p.parse_args(argv)
    token = os.environ.get("AWR_SERVER_TOKEN") or None
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding to non-loopback {a.host}; "
              f"{'bearer auth is on' if token else 'set AWR_SERVER_TOKEN, no auth is enabled'}",
              file=sys.stderr)
    srv = make_server(a.host, a.port, a.roster, a.allow_mock, token, a.log_db)
    print(f"Serving on http://{a.host}:{srv.server_port}/v1 (roster={a.roster})", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

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
import secrets
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .api import (
    CascadePolicy, LearnedPolicy, FrontierOnlyPolicy, ProviderError, Router, RulesPolicy,
)
from .config import ROSTERS

MAX_BODY = 262_144  # 256 KB
LOOPBACK = ("127.0.0.1", "localhost", "::1")
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


def make_handler(roster: str, allow_mock: bool, token: Optional[str], log_db=None,
                 daily_budget_usd: float = 1.0, allow_frontier: bool = False,
                 rate_limit_per_min: int = 30, max_concurrent: int = 8, host: str = "127.0.0.1"):
    lock = threading.Lock()
    slots = threading.BoundedSemaphore(max_concurrent)
    hits: deque = deque()          # request times (monotonic) in the last minute
    spend = {"day": "", "usd": 0.0}  # UTC-day accumulator; in-process, resets on restart

    def today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def rate_ok() -> bool:
        now = time.monotonic()
        with lock:
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= rate_limit_per_min:
                return False
            hits.append(now)
            return True

    def over_budget() -> bool:
        with lock:
            if spend["day"] != today():
                spend.update(day=today(), usd=0.0)
            return spend["usd"] >= daily_budget_usd

    def add_spend(usd: float) -> None:
        with lock:
            if spend["day"] != today():
                spend.update(day=today(), usd=0.0)
            spend["usd"] += usd

    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # socket timeout: slow/idle clients can't pin a thread
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

        def _gate(self) -> bool:
            """Host allowlist (DNS rebinding), no Origin (browsers), concurrency cap."""
            port = self.server.server_port
            ok = {f"{h if h != '::1' else '[::1]'}:{port}" for h in LOOPBACK}
            if host not in LOOPBACK:
                ok.add(f"{host}:{port}")
            if self.headers.get("Host", "").lower() not in ok:
                self._err(403, "Forbidden: bad Host header", "permission_error")
                return False
            if self.headers.get("Origin") is not None:
                self._err(403, "Forbidden: browser requests (Origin) are not allowed", "permission_error")
                return False
            if not slots.acquire(blocking=False):
                self.close_connection = True
                self._err(503, "Server busy", "api_error")
                return False
            return True

        def _authed(self) -> bool:
            if not token:
                return True
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                return True
            self._err(401, "Invalid or missing bearer token", "authentication_error")
            return False

        def do_GET(self):
            if not self._gate():
                return
            try:
                self._get()
            finally:
                slots.release()

        def do_POST(self):
            if not self._gate():
                return
            try:
                self._post()
            finally:
                slots.release()

        def _get(self):
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
                return self._err(413, "Request body too large (max 256 KB)")
            try:
                req = json.loads(self.rfile.read(n))
                assert isinstance(req, dict)
            except Exception:
                return self._err(400, "Request body must be a JSON object")
            return req

        def _post(self):
            path = self.path.split("?")[0]
            if path not in ("/v1/chat/completions", "/v1/feedback"):
                return self._err(404, "Not found")
            if not self._authed():
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                self.close_connection = True  # body unread
                return self._err(415, "Content-Type must be application/json", "invalid_request_error")
            if not rate_ok():
                return self._err(429, f"Rate limit exceeded ({rate_limit_per_min}/min)", "rate_limit_error")
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
            if sel == "router:frontier" and not allow_frontier:
                return self._err(400, "router:frontier is disabled; start the server with --allow-frontier")
            try:
                prompt = flatten(req.get("messages"))
            except ValueError as e:
                return self._err(400, str(e))
            if over_budget():
                return self._err(429, f"Daily budget of ${daily_budget_usd:.2f} reached (resets 00:00 UTC)",
                                 "insufficient_quota")
            try:
                r = Router(SELECTORS[sel](), roster_name=roster,
                           allow_mock_fallback=allow_mock, log_to=log_db).complete(prompt)
            except ProviderError:
                return self._err(502, "Upstream providers failed", "api_error")
            except Exception:
                return self._err(500, "Internal error", "api_error")
            add_spend(r.total_cost_usd)
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
                token: Optional[str] = None, log_db=None, daily_budget_usd: float = 1.0,
                allow_frontier: bool = False, rate_limit_per_min: int = 30,
                max_concurrent: int = 8) -> ThreadingHTTPServer:
    """token=None means NO authentication (tests/embedding only); the CLI always sets one
    unless --no-auth. Defaults: $1/day cap, router:frontier off, 30 req/min, 8 in flight."""
    return ThreadingHTTPServer((host, port), make_handler(
        roster, allow_mock, token, log_db, daily_budget_usd, allow_frontier,
        rate_limit_per_min, max_concurrent, host))


def resolve_token(env_token: Optional[str], no_auth: bool):
    """-> (token or None, generated?). Auth is on unless --no-auth."""
    if no_auth:
        return None, False
    if env_token:
        return env_token, False
    return secrets.token_urlsafe(32), True


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="OpenAI-compatible router endpoint")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--roster", default="cross_vendor", choices=sorted(ROSTERS))
    p.add_argument("--allow-mock", action="store_true", help="offline demo: mock on provider failure")
    p.add_argument("--log-db", default=None, metavar="PATH",
                   help="log requests (incl. prompt text) to this SQLite db and enable "
                        "POST /v1/feedback; off by default")
    p.add_argument("--no-auth", action="store_true", help="DANGEROUS: disable bearer auth")
    p.add_argument("--daily-budget", type=float, default=1.0, metavar="USD",
                   help="refuse completions (429) once today's (UTC) spend reaches this; default 1.00")
    p.add_argument("--allow-frontier", action="store_true", help="enable the router:frontier selector")
    p.add_argument("--rate-limit", type=int, default=30, metavar="N", help="max POSTs per minute; default 30")
    a = p.parse_args(argv)
    token, generated = resolve_token(os.environ.get("AWR_SERVER_TOKEN") or None, a.no_auth)
    if a.no_auth:
        print("WARNING: --no-auth: anyone who can reach this port can spend your API budget",
              file=sys.stderr)
    elif generated:
        print(f"Generated bearer token (shown once; or set AWR_SERVER_TOKEN):\n  {token}\n"
              f"Use: OpenAI(base_url='http://{a.host}:{a.port}/v1', api_key='<token>')", file=sys.stderr)
    if a.host not in LOOPBACK:
        print(f"WARNING: binding to non-loopback {a.host}", file=sys.stderr)
    srv = make_server(a.host, a.port, a.roster, a.allow_mock, token, a.log_db,
                      a.daily_budget, a.allow_frontier, a.rate_limit)
    print(f"Serving on http://{a.host}:{srv.server_port}/v1 (roster={a.roster})", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

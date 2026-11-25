"""OpenAI-compatible HTTP endpoint (stdlib only) in front of ``Router``.

Point any OpenAI SDK's ``base_url`` at ``http://127.0.0.1:8787/v1``.

Multi-turn: routing (task type and difficulty) is decided on the last user turn
plus up to ``ROUTE_WINDOW_MESSAGES`` (2) earlier messages (system and developer ones are skipped); the model
still gets the whole conversation, flattened into one labelled prompt.
Streaming: ``stream=true`` is buffered. The default rule has two cheap models
write full answers and compares them before it chooses, so the answer exists in
full before the decision ends; the server sends it afterwards as SSE chunks.
Clients that need ``stream=true`` work, but the first token comes no sooner.
The ``model`` field selects the routing policy, not a vendor model:
``router``/``auto`` (default rule), ``router:cascade``, ``router:learned``,
``router:frontier`` and ``router:shadow``. The last two answer on the frontier
and need ``--allow-frontier``; ``router:shadow`` also returns ``x_router.shadow``,
what the router would have called and its estimated cost (docs/SHADOW_MODE.md).
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional, Union

from .api import (
    CascadePolicy, LearnedPolicy, FrontierOnlyPolicy, Policy, ProviderError, Router, default_policy,
)
from .config import ROSTERS
from .lookup import RefreshingLookup
from .shadow import ShadowComparePolicy
from .spend_guard import SpendGuard, spend_store_from_env

LogDb = Union[None, bool, str, Path]  # see Router(log_to=...)

MAX_BODY = 262_144  # 256 KB
ROUTE_WINDOW_MESSAGES = 2  # messages before the last user turn that routing also reads
ROUTE_SKIP_ROLES = ("system", "developer")  # instructions, not turns: never part of the routing text
STREAM_CHUNK_CHARS = 64  # max characters per SSE content chunk
FEEDBACK_RATE_LIMIT_PER_MIN = 10  # /v1/feedback has its own limiter, apart from completions
LOOPBACK = ("127.0.0.1", "localhost", "::1")
SELECTORS: dict[str, Callable[[], Policy]] = {
    "router": default_policy,
    "auto": default_policy,
    "router:cascade": CascadePolicy,
    "router:learned": lambda: LearnedPolicy(use_labels=False),
    "router:frontier": FrontierOnlyPolicy,
    "router:shadow": lambda: ShadowComparePolicy(default_policy()),
}


def _labelled(messages: Any) -> list[str]:
    """Validate the chat messages; each one -> '[role]\ncontent'. Raises ValueError."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("'messages' must be a non-empty array")
    parts = []
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("role"), str):
            raise ValueError("each message needs a 'role'")
        c = m.get("content")
        if isinstance(c, list):  # SDK content parts: [{"type":"text","text":...}]
            texts = [p.get("text", "") for p in c if isinstance(p, dict)]
            c = "".join(texts) if all(isinstance(t, str) for t in texts) else None
        if not isinstance(c, str):
            raise ValueError("message 'content' must be a string or text parts")
        parts.append(f"[{m['role']}]\n{c}")
    return parts


def flatten(messages: Any) -> str:
    """system + user/assistant turns -> one labelled prompt. Raises ValueError."""
    return "\n\n".join(_labelled(messages))


def routing_text(messages: Any, window: int = ROUTE_WINDOW_MESSAGES) -> tuple[str, int]:
    """-> (text to classify, messages used). Raises ValueError like ``flatten``.

    The last user message plus up to `window` messages right before it, skipping system
    and developer messages (they do not use up the window); later messages are ignored. With no user
    message, the whole flattened conversation.
    """
    parts = _labelled(messages)  # validates every message, so roles below are strings
    users = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if not users:
        return "\n\n".join(parts), len(parts)
    picked = [users[-1]]
    for i in range(users[-1] - 1, -1, -1):
        if len(picked) > window:
            break
        if messages[i]["role"] not in ROUTE_SKIP_ROLES:
            picked.append(i)
    return "\n\n".join(parts[i] for i in reversed(picked)), len(picked)


def _stream_error(req: dict[str, Any]) -> Optional[str]:
    """Type check of ``stream``/``stream_options``; None when they are valid or absent."""
    # null counts as absent, as in the OpenAI API
    if req.get("stream") is not None and not isinstance(req["stream"], bool):
        return "'stream' must be a boolean"
    so = req.get("stream_options")
    if so is not None:
        if not isinstance(so, dict):
            return "'stream_options' must be an object"
        if so.get("include_usage") is not None and not isinstance(so["include_usage"], bool):
            return "'stream_options.include_usage' must be a boolean"
    return None


def sse_body(resp: dict[str, Any], include_usage: bool) -> bytes:
    """A finished chat.completion -> the OpenAI streaming body (buffered: the text is complete)."""
    base = {"id": resp["id"], "object": "chat.completion.chunk",
            "created": resp["created"], "model": resp["model"]}

    def chunk(delta: dict[str, Any], finish: Optional[str], **extra: Any) -> dict[str, Any]:
        c = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
        if include_usage:
            c["usage"] = None  # OpenAI's convention: only the last chunk carries usage
        return c

    text = resp["choices"][0]["message"]["content"] or ""
    events = [chunk({"role": "assistant", "content": ""}, None)]
    events += [chunk({"content": text[i:i + STREAM_CHUNK_CHARS]}, None)
               for i in range(0, len(text), STREAM_CHUNK_CHARS)]
    events.append(chunk({}, "stop", x_router=resp["x_router"]))
    if include_usage:
        events.append({**base, "choices": [], "usage": resp["usage"]})
    return ("".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n").encode()


def make_handler(roster: str, allow_mock: bool, token: Optional[str], log_db: LogDb = None,
                 daily_budget_usd: float = 1.0, allow_frontier: bool = False,
                 rate_limit_per_min: int = 30, max_concurrent: int = 8,
                 host: str = "127.0.0.1", lookup_from_log: bool = False,
                 lookup_rebuild_every: int = 100) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class, closing over this server's limits and guard."""
    holder: Optional[RefreshingLookup] = None
    if lookup_from_log:
        if not log_db or log_db is True:
            raise ValueError("lookup_from_log needs a log_db path")
        holder = RefreshingLookup(log_db, every=lookup_rebuild_every)
        holder.rebuild()  # on start, so the operator sees what was learned before any request

    def _policy(sel: str) -> Policy:
        """Resolve the selector at request time; router/auto use the live lookup when it is on."""
        if holder is not None and sel in ("router", "auto"):
            return default_policy(lookup=holder.get())
        return SELECTORS[sel]()
    slots = _slots = threading.BoundedSemaphore(max_concurrent)
    guard = SpendGuard(daily_budget_usd, rate_limit_per_min, allow_frontier, store=spend_store_from_env())
    rate_ok, over_budget, add_spend = guard.rate_ok, guard.over_budget, guard.add_spend
    feedback_guard = SpendGuard(daily_budget_usd, FEEDBACK_RATE_LIMIT_PER_MIN, allow_frontier)

    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # socket timeout: slow/idle clients can't pin a thread
        server_version = "awr-router"
        protocol_version = "HTTP/1.1"
        server: ThreadingHTTPServer
        slots = _slots  # exposed so tests can hold the concurrency slots
        lookup_holder = holder  # the live-learning lookup, or None when --lookup-from-log is off

        def log_message(self, fmt: str, *args: Any) -> None:  # never log paths/bodies/headers
            pass

        def _send(self, code: int, obj: Any, headers: Optional[dict[str, str]] = None) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_sse(self, body: bytes, headers: dict[str, str]) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))  # body is complete: keep-alive stays correct
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code: int, msg: str, typ: str = "invalid_request_error") -> None:
            self._send(code, {"error": {"message": msg, "type": typ, "param": None, "code": None}})

        def _gate(self) -> bool:
            """Host allowlist (DNS rebinding), no Origin (browsers). Takes no slot and counts no rate."""
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
            return True

        def _take_slot(self) -> bool:
            """Concurrency cap; only called after auth and body validation."""
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

        def do_GET(self) -> None:
            if not self._gate():
                return
            path = self.path.split("?")[0]
            if path == "/healthz":  # unauthenticated: must not take (or be refused) a slot
                return self._send(200, {"status": "ok"})
            if not self._authed():
                return
            if not self._take_slot():
                return
            try:
                self._get(path)
            finally:
                slots.release()

        def do_POST(self) -> None:
            if not self._gate():
                return
            self._post()

        def _get(self, path: str) -> None:
            if path == "/v1/models":
                data = [{"id": s, "object": "model", "created": 0, "owned_by": "ai-workload-router"}
                        for s in SELECTORS]
                return self._send(200, {"object": "list", "data": data})
            self._err(404, "Not found")

        def _read_json(self) -> Optional[dict[str, Any]]:
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

        def _post(self) -> None:
            path = self.path.split("?")[0]
            if path not in ("/v1/chat/completions", "/v1/feedback"):
                self.close_connection = True  # body unread
                return self._err(404, "Not found")
            if not self._authed():
                self.close_connection = True  # body unread
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                self.close_connection = True  # body unread
                return self._err(415, "Content-Type must be application/json", "invalid_request_error")
            req = self._read_json()
            if req is None:
                return
            if path == "/v1/feedback":
                bad = self._feedback_invalid(req)
                if bad:  # malformed payloads never count toward the limit
                    return self._err(400, bad)
                if not feedback_guard.rate_ok():
                    return self._err(429, f"Rate limit exceeded ({FEEDBACK_RATE_LIMIT_PER_MIN}/min)",
                                     "rate_limit_error")
                if not self._take_slot():
                    return
                try:
                    return self._feedback(req)
                finally:
                    slots.release()
            bad = _stream_error(req)
            if bad:
                return self._err(400, bad)
            sel = req.get("model")
            if sel not in SELECTORS:
                return self._err(400, f"Unknown model; use one of: {', '.join(SELECTORS)}")
            if sel == "router:frontier" and not allow_frontier:
                return self._err(400, "router:frontier is disabled; start the server with --allow-frontier")
            if sel == "router:shadow" and not allow_frontier:
                return self._err(400, "router:shadow answers on the frontier and is disabled; "
                                      "start the server with --allow-frontier")
            try:
                prompt = flatten(req.get("messages"))
                route_on, routed_on = routing_text(req["messages"])
            except ValueError as e:
                return self._err(400, str(e))
            if not rate_ok():
                return self._err(429, f"Rate limit exceeded ({rate_limit_per_min}/min)", "rate_limit_error")
            if not self._take_slot():
                return
            try:
                self._complete(req, sel, prompt, route_on, routed_on)
            finally:
                slots.release()

        def _complete(self, req: dict[str, Any], sel: str, prompt: str,
                      route_on: str, routed_on: int) -> None:
            if over_budget():
                return self._err(429, f"Daily budget of ${daily_budget_usd:.2f} reached (resets 00:00 UTC)",
                                 "insufficient_quota")
            try:
                r = Router(_policy(sel), roster_name=roster,
                           allow_mock_fallback=allow_mock, log_to=log_db).complete(prompt, route_on=route_on)
            except ProviderError:
                return self._err(502, "Upstream providers failed", "api_error")
            except Exception:
                return self._err(500, "Internal error", "api_error")
            if not r.simulated:  # mock answers cost nothing and must not eat the shared budget
                add_spend(r.total_cost_usd)
            u = r.response
            pt, ct = (u.input_tokens, u.output_tokens) if u else (0, 0)
            x_router: dict[str, Any] = {
                "model_chosen": r.model, "policy": sel,
                "answer_cost_usd": r.answer_cost_usd,
                "routing_cost_usd": r.routing_cost_usd,
                "trace": r.trace, "simulated": r.simulated,
                "degraded": r.degraded,
                "request_id": r.request_id,
                "routed_on_messages": routed_on}
            if r.shadow is not None:  # router:shadow only; other selectors' bodies are unchanged
                x_router["shadow"] = r.shadow
            resp = {
                "id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion",
                "created": int(time.time()), "model": r.model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": r.text}}],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
                "x_router": x_router,
            }
            headers = {"X-Router-Model": r.model}
            if req.get("stream"):
                include_usage = bool((req.get("stream_options") or {}).get("include_usage"))
                return self._send_sse(sse_body(resp, include_usage), headers)
            self._send(200, resp, headers)

        @staticmethod
        def _feedback_invalid(req: dict[str, Any]) -> Optional[str]:
            """Payload check only (no DB access); mirrors Router.feedback's validation."""
            if not log_db:
                return "Feedback needs the server started with --log-db"
            score = req.get("score")
            if not isinstance(req.get("request_id"), str) or not req["request_id"]:
                return "request_id must be a non-empty string"
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0.0 <= score <= 1.0:
                return f"score must be a number in [0, 1], got {score!r}"
            return None

        def _feedback(self, req: dict[str, Any]) -> None:
            """{request_id, score in [0,1]} -> the learned router's live signal."""
            try:
                Router(FrontierOnlyPolicy(), log_to=log_db).feedback(
                    req.get("request_id"), req.get("score"),  # type: ignore[arg-type]  # checked inside
                    source="user")
            except KeyError:
                return self._err(404, "Unknown request_id")
            except (ValueError, TypeError) as e:
                return self._err(400, str(e))
            self._send(200, {"status": "recorded"})

    return Handler


def make_server(host: str = "127.0.0.1", port: int = 8787, roster: str = "cross_vendor",
                allow_mock: bool = False, token: Optional[str] = None, log_db: LogDb = None,
                daily_budget_usd: float = 1.0,
                allow_frontier: bool = False, rate_limit_per_min: int = 30,
                max_concurrent: int = 8, lookup_from_log: bool = False,
                lookup_rebuild_every: int = 100) -> ThreadingHTTPServer:
    """token=None means NO authentication (tests/embedding only); the CLI always sets one
    unless --no-auth. Defaults: $1/day cap, router:frontier and router:shadow off, 30 req/min, 8 in flight."""
    return ThreadingHTTPServer((host, port), make_handler(
        roster, allow_mock, token, log_db, daily_budget_usd, allow_frontier,
        rate_limit_per_min, max_concurrent, host,
        lookup_from_log=lookup_from_log, lookup_rebuild_every=lookup_rebuild_every))


def resolve_token(env_token: Optional[str], no_auth: bool) -> tuple[Optional[str], bool]:
    """-> (token or None, generated?). Auth is on unless --no-auth."""
    if no_auth:
        return None, False
    if env_token:
        return env_token, False
    return secrets.token_urlsafe(32), True


def main(argv: Optional[list[str]] = None) -> None:
    """CLI entry point: parse flags, resolve the bearer token, serve until interrupted."""
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
    p.add_argument("--allow-frontier", action="store_true", help="enable the router:frontier and router:shadow selectors "
                        "(both answer on the frontier)")
    p.add_argument("--rate-limit", type=int, default=30, metavar="N", help="max POSTs per minute; default 30")
    p.add_argument("--lookup-from-log", action="store_true",
                   help="rebuild the free lookup from --log-db rows that have a good outcome, on start "
                        "and every --lookup-rebuild-every requests; off by default")
    p.add_argument("--lookup-rebuild-every", type=int, default=100, metavar="N",
                   help="with --lookup-from-log: rebuild after every N router/auto requests; default 100")
    a = p.parse_args(argv)
    if a.lookup_from_log and not a.log_db:
        p.error("--lookup-from-log needs --log-db")
    if a.lookup_rebuild_every < 1:
        p.error("--lookup-rebuild-every must be >= 1")
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
                      a.daily_budget, a.allow_frontier, a.rate_limit,
                      lookup_from_log=a.lookup_from_log, lookup_rebuild_every=a.lookup_rebuild_every)
    holder = getattr(srv.RequestHandlerClass, "lookup_holder", None)
    if holder is not None and holder.lookup is not None:
        print(f"lookup: {holder.lookup.live_rows} live rows added to the public tasks "
              f"(min {holder.min_rows})", file=sys.stderr)
    print(f"Serving on http://{a.host}:{srv.server_port}/v1 (roster={a.roster})", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

"""Tests for router.server (mock mode, real socket on port 0)."""

import json
import sys
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import db  # noqa: E402
from router.server import MAX_BODY, SELECTORS, make_server, resolve_token  # noqa: E402


def start(token=None, **kw):
    kw.setdefault("rate_limit_per_min", 10_000)
    kw.setdefault("allow_frontier", True)
    srv = make_server(port=0, allow_mock=True, token=token, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def call(base, path, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json"} if data is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


MSG = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "What is 2+2?"}]


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # router:learned reads the runs db; use an empty temp one, not data/runs.db
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old = db.DB_PATH
        db.DB_PATH = Path(cls._tmp.name) / "runs.db"
        db.init_db()
        cls.srv, cls.base = start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        db.DB_PATH = cls._old
        cls._tmp.cleanup()

    def test_happy_path(self):
        code, j, h = call(self.base, "/v1/chat/completions", {"model": "router", "messages": MSG})
        self.assertEqual(code, 200)
        self.assertEqual(j["object"], "chat.completion")
        self.assertTrue(j["id"] and j["created"] and j["model"])
        self.assertTrue(j["choices"][0]["message"]["content"])
        u = j["usage"]
        self.assertEqual(u["total_tokens"], u["prompt_tokens"] + u["completion_tokens"])
        x = j["x_router"]
        self.assertEqual(x["model_chosen"], j["model"])
        self.assertTrue(x["simulated"])
        self.assertIn("trace", x)
        self.assertEqual(h["X-Router-Model"], j["model"])

    def test_each_selector(self):
        for s in SELECTORS:
            code, j, _ = call(self.base, "/v1/chat/completions", {"model": s, "messages": MSG})
            self.assertEqual(code, 200, s)
            self.assertEqual(j["x_router"]["policy"], s)

    def test_sdk_style_body(self):
        body = {"model": "auto", "stream": False, "temperature": 0.2, "max_tokens": 50,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}
        code, j, _ = call(self.base, "/v1/chat/completions", body,
                          {"Content-Type": "application/json", "Authorization": "Bearer sk-x",
                           "User-Agent": "OpenAI/Python 1.0"})
        self.assertEqual(code, 200)
        self.assertTrue(j["choices"][0]["message"]["content"])

    def test_errors(self):
        p = "/v1/chat/completions"
        for body in ({"model": "gpt-4", "messages": MSG},
                     {"model": "router", "messages": MSG, "stream": True},
                     {"model": "router", "messages": []}):
            code, j, _ = call(self.base, p, body)
            self.assertEqual(code, 400)
            self.assertIn("message", j["error"])
        self.assertEqual(call(self.base, p, raw=b"{nope")[0], 400)
        self.assertEqual(call(self.base, p, raw=b"x" * (MAX_BODY + 1))[0], 413)

    def test_models_and_health(self):
        code, j, _ = call(self.base, "/v1/models")
        self.assertEqual([m["id"] for m in j["data"]], list(SELECTORS))
        self.assertEqual(call(self.base, "/healthz")[1], {"status": "ok"})


class AuthTests(unittest.TestCase):
    def test_token(self):
        srv, base = start(token="s3cret")
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        body = {"model": "router", "messages": MSG}
        p = "/v1/chat/completions"
        self.assertEqual(call(base, p, body)[0], 401)
        self.assertEqual(call(base, p, body, {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(call(base, p, body, {"Authorization": "Bearer s3cret"})[0], 200)
        self.assertEqual(call(base, "/healthz")[0], 200)



class FeedbackLoopTests(unittest.TestCase):
    """Endpoint + learning hook together: answer -> request_id -> rating stored."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dbp = Path(self._tmp.name) / "live.db"
        self.srv = make_server(port=0, allow_mock=True, log_db=self.dbp)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._tmp.cleanup()

    def test_full_loop(self):
        _, j, _ = call(self.base, "/v1/chat/completions", {"model": "router", "messages": MSG})
        rid = j["x_router"]["request_id"]
        self.assertTrue(rid)
        self.assertEqual(call(self.base, "/v1/feedback", {"request_id": rid, "score": 0.9})[0], 200)
        import sqlite3
        row = sqlite3.connect(self.dbp).execute(
            "SELECT quality_score, quality_source FROM runs WHERE task_id=?", (rid,)).fetchone()
        self.assertEqual(row, (0.9, "user"))

    def test_feedback_errors(self):
        self.assertEqual(call(self.base, "/v1/feedback", {"request_id": "nope", "score": 1})[0], 404)
        _, j, _ = call(self.base, "/v1/chat/completions", {"model": "router", "messages": MSG})
        bad = {"request_id": j["x_router"]["request_id"], "score": 7}
        self.assertEqual(call(self.base, "/v1/feedback", bad)[0], 400)

    def test_feedback_off_without_log_db(self):
        srv, base = start()
        try:
            self.assertEqual(call(base, "/v1/feedback", {"request_id": "x", "score": 1})[0], 400)
        finally:
            srv.shutdown(); srv.server_close()


class HardeningTests(unittest.TestCase):
    P = "/v1/chat/completions"
    BODY = {"model": "router", "messages": MSG}

    def serve(self, **kw):
        srv, base = start(**kw)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, base

    def test_content_type_required(self):
        _, base = self.serve()
        code, j, _ = call(base, self.P, self.BODY, {"Content-Type": "text/plain"})
        self.assertEqual(code, 415)
        self.assertIn("message", j["error"])
        self.assertEqual(call(base, self.P, self.BODY, {"Content-Type": "application/json; charset=utf-8"})[0], 200)

    def test_origin_rejected(self):
        _, base = self.serve()
        self.assertEqual(call(base, self.P, self.BODY, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(call(base, "/healthz", headers={"Origin": "http://evil.example"})[0], 403)

    def test_host_allowlist(self):
        srv, base = self.serve()
        port = srv.server_port
        for bad in ("evil.example", f"evil.example:{port}", "127.0.0.1:1"):
            self.assertEqual(call(base, self.P, self.BODY, {"Host": bad})[0], 403, bad)
            self.assertEqual(call(base, "/healthz", headers={"Host": bad})[0], 403, bad)
        for good in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            self.assertEqual(call(base, self.P, self.BODY, {"Host": good})[0], 200, good)

    def test_token_required_when_set(self):
        _, base = self.serve(token="tok")
        self.assertEqual(call(base, "/v1/models")[0], 401)
        self.assertEqual(call(base, "/v1/models", headers={"Authorization": "Bearer tok"})[0], 200)

    def test_daily_budget(self):
        _, base = self.serve(daily_budget_usd=1e-9)
        self.assertEqual(call(base, self.P, self.BODY)[0], 200)  # spent 0 < budget
        code, j, _ = call(base, self.P, self.BODY)
        self.assertEqual(code, 429)
        self.assertIn("budget", j["error"]["message"])

    def test_frontier_gated(self):
        body = {"model": "router:frontier", "messages": MSG}
        _, base = self.serve(allow_frontier=False)
        code, j, _ = call(base, self.P, body)
        self.assertEqual(code, 400)
        self.assertIn("--allow-frontier", j["error"]["message"])
        _, base2 = self.serve(allow_frontier=True)
        self.assertEqual(call(base2, self.P, body)[0], 200)

    def test_rate_limit(self):
        _, base = self.serve(rate_limit_per_min=2)
        codes = [call(base, self.P, self.BODY)[0] for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_oversize(self):
        _, base = self.serve()
        self.assertEqual(call(base, self.P, raw=b"x" * (256 * 1024 + 1))[0], 413)

    def test_resolve_token(self):
        t, gen = resolve_token(None, False)
        self.assertTrue(gen and len(t) >= 32)
        self.assertEqual(resolve_token("abc", False), ("abc", False))
        self.assertEqual(resolve_token(None, True), (None, False))
        self.assertEqual(resolve_token("abc", True), (None, False))


if __name__ == "__main__":
    unittest.main()

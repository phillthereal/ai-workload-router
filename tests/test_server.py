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
from router.server import SELECTORS, make_server  # noqa: E402


def start(token=None):
    srv = make_server(port=0, allow_mock=True, token=token)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def call(base, path, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, headers=headers or {})
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
        self.assertEqual(call(self.base, p, raw=b"x" * 1_000_001)[0], 413)

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


if __name__ == "__main__":
    unittest.main()

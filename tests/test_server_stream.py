"""Tests for buffered SSE streaming and multi-turn routing in router.server (offline, mock adapter)."""

import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import db  # noqa: E402
from router.adapters.base import Adapter, Response  # noqa: E402
from router.adapters.mock import MockAdapter  # noqa: E402
from router.api import Router, default_policy  # noqa: E402
from router.server import (  # noqa: E402
    ROUTE_WINDOW_MESSAGES, STREAM_CHUNK_CHARS, flatten, make_server, routing_text,
)
from router.shadow import ShadowComparePolicy  # noqa: E402
from router.spend_guard import SpendGuard  # noqa: E402

P = "/v1/chat/completions"
WRITING = "Write a short poem about autumn leaves falling in a quiet park."
REASONING = ("If a train leaves at 3pm going 60 mph and another at 4pm going 80 mph, "
             "when does the second catch up? Show reasoning.")
PUZZLE = ("Solve: a bat and a ball cost $1.10 in total; the bat costs $1.00 more than the ball. "
          "How much is the ball? Think step by step.")
MSG = [{"role": "user", "content": WRITING}]  # the mock answers this with more than 64 characters


class StubAdapter(Adapter):
    """A real (non-simulated) model, so the answer is charged."""

    def __init__(self, model):
        self.model = model

    def complete(self, prompt, effort=None, max_tokens=None):
        return Response("4", 1000, 1000, 1.0, self.model, False)


class RecordingAdapter(MockAdapter):
    """The mock adapter, plus a record of every prompt a model received."""

    prompts: list = []

    def complete(self, prompt, *a, **k):
        RecordingAdapter.prompts.append(prompt)
        return super().complete(prompt, *a, **k)


def patch_adapters(test, factory):
    for target in ("router.api.get_adapter", "router.adapters.get_adapter"):
        p = mock.patch(target, factory)
        p.start()
        test.addCleanup(p.stop)


def start(test, **kw):
    kw.setdefault("rate_limit_per_min", 10_000)
    kw.setdefault("allow_frontier", True)
    srv = make_server(port=0, allow_mock=True, token=None, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    test.addCleanup(srv.server_close)
    test.addCleanup(srv.shutdown)
    return srv.server_port


def post(port, body, conn=None):
    """-> (status, headers dict (lower-case keys), raw body bytes)."""
    c = conn or http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body).encode()
    c.request("POST", P, body=data, headers={"Content-Type": "application/json"})
    r = c.getresponse()
    raw = r.read()
    headers = {k.lower(): v for k, v in r.getheaders()}
    if conn is None:
        c.close()
    return r.status, headers, raw


def sse_events(raw):
    """Parse an SSE body: list of decoded JSON chunks, with the literal '[DONE]' kept as a string."""
    text = raw.decode("utf-8")
    assert text.endswith("\n\n"), text[-20:]
    out = []
    for block in text.split("\n\n")[:-1]:
        assert block.startswith("data: ") and "\n" not in block, block
        payload = block[len("data: "):]
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


class StreamTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(db, "DB_PATH", Path(tmp.name) / "runs.db")
        p.start()
        self.addCleanup(p.stop)
        self.port = start(self)

    def test_stream_matches_non_stream(self):
        code, _, raw = post(self.port, {"model": "router", "messages": MSG})
        self.assertEqual(code, 200)
        plain = json.loads(raw)
        code, h, raw = post(self.port, {"model": "router", "messages": MSG, "stream": True})
        self.assertEqual(code, 200)
        self.assertEqual(h["content-type"], "text/event-stream; charset=utf-8")
        self.assertEqual(h["cache-control"], "no-cache")
        self.assertEqual(int(h["content-length"]), len(raw))
        self.assertEqual(h["x-router-model"], plain["model"])
        ev = sse_events(raw)
        self.assertEqual(ev[-1], "[DONE]")
        chunks = ev[:-1]
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant", "content": ""})
        self.assertIsNone(chunks[0]["choices"][0]["finish_reason"])
        content = chunks[1:-1]
        answer = plain["choices"][0]["message"]["content"]
        self.assertGreater(len(answer), STREAM_CHUNK_CHARS)
        self.assertGreater(len(content), 1)
        for c in content:
            self.assertEqual(set(c["choices"][0]["delta"]), {"content"})
            self.assertLessEqual(len(c["choices"][0]["delta"]["content"]), STREAM_CHUNK_CHARS)
            self.assertIsNone(c["choices"][0]["finish_reason"])
        self.assertEqual("".join(c["choices"][0]["delta"]["content"] for c in content), answer)
        last = chunks[-1]
        self.assertEqual(last["choices"][0]["delta"], {})
        self.assertEqual(last["choices"][0]["finish_reason"], "stop")
        self.assertEqual(set(last["x_router"]), set(plain["x_router"]))
        self.assertEqual(last["x_router"]["routed_on_messages"], 1)
        ids = {c["id"] for c in chunks}
        self.assertEqual(len(ids), 1)
        self.assertTrue(ids.pop().startswith("chatcmpl-"))
        for c in chunks:
            self.assertEqual(c["object"], "chat.completion.chunk")
            self.assertEqual(c["model"], plain["model"])
            self.assertEqual(c["created"], chunks[0]["created"])
            self.assertNotIn("usage", c)  # without include_usage no chunk carries a usage key

    def test_include_usage(self):
        body = {"model": "router", "messages": MSG, "stream": True, "stream_options": {"include_usage": True}}
        code, _, raw = post(self.port, body)
        self.assertEqual(code, 200)
        ev = sse_events(raw)
        self.assertEqual(ev[-1], "[DONE]")
        usage = ev[-2]
        self.assertEqual(usage["choices"], [])
        self.assertEqual(set(usage["usage"]), {"prompt_tokens", "completion_tokens", "total_tokens"})
        self.assertEqual(ev[-3]["choices"][0]["finish_reason"], "stop")
        for c in ev[:-2]:
            self.assertIn("usage", c)
            self.assertIsNone(c["usage"])
        _, _, raw = post(self.port, {"model": "router", "messages": MSG})
        self.assertEqual(usage["usage"], json.loads(raw)["usage"])

    def test_stream_false_is_plain_json(self):
        code, h, raw = post(self.port, {"model": "router", "messages": MSG, "stream": False})
        self.assertEqual(code, 200)
        self.assertEqual(h["content-type"], "application/json")
        self.assertEqual(json.loads(raw)["object"], "chat.completion")

    def test_bad_stream_fields_are_json_400(self):
        for extra in ({"stream": "yes"}, {"stream": 1}, {"stream": True, "stream_options": []},
                      {"stream": True, "stream_options": {"include_usage": "true"}}):
            code, h, raw = post(self.port, {"model": "router", "messages": MSG, **extra})
            self.assertEqual(code, 400, extra)
            self.assertEqual(h["content-type"], "application/json")
            self.assertIn("message", json.loads(raw)["error"])

    def test_errors_stay_json_when_streaming(self):
        for body in ({"model": "gpt-4", "messages": MSG, "stream": True},
                     {"model": "router", "messages": [], "stream": True}):
            code, h, raw = post(self.port, body)
            self.assertEqual(code, 400)
            self.assertEqual(h["content-type"], "application/json")
            self.assertIn("message", json.loads(raw)["error"])

    def test_connection_reusable_after_stream(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        code, _, raw = post(self.port, {"model": "router", "messages": MSG, "stream": True}, conn)
        self.assertEqual(code, 200)
        self.assertEqual(sse_events(raw)[-1], "[DONE]")
        code, _, raw = post(self.port, {"model": "router", "messages": MSG}, conn)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["object"], "chat.completion")

    def test_shadow_stream_carries_shadow(self):
        body = {"model": "router:shadow", "messages": MSG, "stream": True}
        code, _, raw = post(self.port, body)
        self.assertEqual(code, 200)
        last = sse_events(raw)[-2]
        self.assertEqual(last["choices"][0]["finish_reason"], "stop")
        self.assertTrue(last["x_router"]["shadow"]["would_call"])


class StreamSpendTests(unittest.TestCase):
    def spent(self, body, stub):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "spend.db"
            with mock.patch.dict(os.environ, {"AWR_SPEND_STORE": str(store)}), \
                    mock.patch.object(db, "DB_PATH", Path(tmp) / "runs.db"):
                if stub:
                    patch_adapters(self, lambda m, *a, **k: StubAdapter(m))
                port = start(self, daily_budget_usd=100.0)
                code, _, _ = post(port, body)
                self.assertEqual(code, 200)
            return SpendGuard(store=store).spent_today()

    def test_simulated_stream_adds_no_spend(self):
        self.assertEqual(self.spent({"model": "router", "messages": MSG, "stream": True}, stub=False), 0.0)

    def test_charged_stream_adds_same_spend_as_non_stream(self):
        plain = self.spent({"model": "router", "messages": MSG}, stub=True)
        streamed = self.spent({"model": "router", "messages": MSG, "stream": True}, stub=True)
        self.assertGreater(plain, 0.0)
        self.assertEqual(streamed, plain)


def conversation():
    """Ten messages: a reasoning puzzle, a short thanks, then a last user turn that asks for writing."""
    msgs = [{"role": "system", "content": "You are a helpful assistant."}]
    for _ in range(3):
        msgs.append({"role": "user", "content": PUZZLE})
        msgs.append({"role": "assistant", "content": "Let x be the ball price; then x + (x+1) = 1.10, so x = 0.05."})
    msgs.append({"role": "user", "content": "Thanks, that is clear."})
    msgs.append({"role": "assistant", "content": "Glad to help."})
    msgs.append({"role": "user", "content": WRITING})
    return msgs


class RoutingTextTests(unittest.TestCase):
    def test_window_of_two(self):
        self.assertEqual(ROUTE_WINDOW_MESSAGES, 2)
        m = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
             {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"},
             {"role": "user", "content": "e"}]
        self.assertEqual(routing_text(m), ("[user]\nc\n\n[assistant]\nd\n\n[user]\ne", 3))
        self.assertEqual(routing_text(m, window=0), ("[user]\ne", 1))

    def test_system_skipped_and_not_counted(self):
        m = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
             {"role": "system", "content": "S"}, {"role": "user", "content": "c"}]
        self.assertEqual(routing_text(m), ("[user]\na\n\n[assistant]\nb\n\n[user]\nc", 3))

    def test_no_user_message_routes_on_everything(self):
        m = [{"role": "system", "content": "S"}, {"role": "assistant", "content": "b"}]
        self.assertEqual(routing_text(m), (flatten(m), 2))

    def test_trailing_assistant_ignored(self):
        m = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        self.assertEqual(routing_text(m), ("[user]\na", 1))

    def test_content_parts(self):
        m = [{"role": "user", "content": [{"type": "text", "text": "he"}, {"type": "text", "text": "llo"}]}]
        self.assertEqual(routing_text(m), ("[user]\nhello", 1))

    def test_single_user_message_equals_flatten(self):
        m = [{"role": "user", "content": "What is 2+2?"}]
        self.assertEqual(routing_text(m), (flatten(m), 1))

    def test_same_validation_errors(self):
        for bad in ([], None, [{"content": "x"}], [{"role": "user", "content": 3}]):
            with self.assertRaises(ValueError) as a:
                flatten(bad)
            with self.assertRaises(ValueError) as b:
                routing_text(bad)
            self.assertEqual(str(a.exception), str(b.exception))


class RouteOnTests(unittest.TestCase):
    def setUp(self):
        RecordingAdapter.prompts = []
        patch_adapters(self, lambda m, *a, **k: RecordingAdapter(m))

    @staticmethod
    def same(r):
        return (r.text, r.model, r.trace, r.answer_cost_usd, r.routing_cost_usd, r.simulated,
                r.degraded, r.task_type, r.difficulty, r.shadow)

    def test_classifies_on_route_on_and_sends_prompt(self):
        ref = Router(default_policy(), allow_mock_fallback=True).complete(WRITING)
        RecordingAdapter.prompts = []
        r = Router(default_policy(), allow_mock_fallback=True).complete(REASONING, route_on=WRITING)
        self.assertEqual(r.trace[0], ref.trace[0])
        self.assertEqual((r.task_type, r.difficulty), (ref.task_type, ref.difficulty))
        self.assertEqual(RecordingAdapter.prompts, [REASONING])
        alone = Router(default_policy(), allow_mock_fallback=True).complete(REASONING)
        self.assertNotEqual(alone.trace[0], r.trace[0])  # the test would prove nothing otherwise

    def test_route_on_none_or_equal_is_unchanged(self):
        for p in (WRITING, REASONING):
            base = self.same(Router(default_policy(), allow_mock_fallback=True).complete(p))
            for ro in (None, p):
                got = self.same(Router(default_policy(), allow_mock_fallback=True).complete(p, route_on=ro))
                self.assertEqual(got, base, (p, ro))

    def test_shadow_dry_run_uses_route_on(self):
        ref = Router(ShadowComparePolicy(default_policy()), allow_mock_fallback=True).complete(WRITING)
        RecordingAdapter.prompts = []
        r = Router(ShadowComparePolicy(default_policy()), allow_mock_fallback=True).complete(
            REASONING, route_on=WRITING)
        self.assertEqual((r.shadow["task_type"], r.shadow["difficulty"]),
                         (ref.shadow["task_type"], ref.shadow["difficulty"]))
        self.assertEqual(r.shadow["would_call"], ref.shadow["would_call"])
        self.assertEqual(RecordingAdapter.prompts, [REASONING])
        for p in (WRITING, REASONING):
            base = self.same(Router(ShadowComparePolicy(default_policy()), allow_mock_fallback=True).complete(p))
            got = self.same(Router(ShadowComparePolicy(default_policy()),
                                   allow_mock_fallback=True).complete(p, route_on=None))
            self.assertEqual(got, base)

    def test_log_stores_full_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbp = Path(tmp) / "runs.db"
            Router(default_policy(), allow_mock_fallback=True, log_to=dbp).complete(REASONING, route_on=WRITING)
            with closing(sqlite3.connect(dbp)) as con:
                rows = con.execute("SELECT prompt_text, task_type FROM runs").fetchall()
        ref = Router(default_policy(), allow_mock_fallback=True).complete(WRITING)
        self.assertEqual(rows, [(REASONING, ref.task_type)])


class MultiTurnServerTests(unittest.TestCase):
    def setUp(self):
        RecordingAdapter.prompts = []
        patch_adapters(self, lambda m, *a, **k: RecordingAdapter(m))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(db, "DB_PATH", Path(tmp.name) / "runs.db")
        p.start()
        self.addCleanup(p.stop)
        self.port = start(self)

    def test_ten_turns_route_like_the_last_turn(self):
        msgs = conversation()
        self.assertEqual(len(msgs), 10)
        text, used = routing_text(msgs)
        self.assertEqual(used, 3)
        for stream in (False, True):
            RecordingAdapter.prompts = []
            code, _, raw = post(self.port, {"model": "router", "messages": msgs, "stream": stream})
            self.assertEqual(code, 200)
            x = sse_events(raw)[-2]["x_router"] if stream else json.loads(raw)["x_router"]
            self.assertEqual(x["routed_on_messages"], 3)
            self.assertEqual(RecordingAdapter.prompts, [flatten(msgs)])  # the model got the whole thing
            single = Router(default_policy(), allow_mock_fallback=True).complete(text)
            self.assertEqual(x["model_chosen"], single.model)
            self.assertEqual(x["trace"][0], single.trace[0])
            # and the whole conversation alone would have been classified otherwise
            whole = Router(default_policy(), allow_mock_fallback=True).complete(flatten(msgs))
            self.assertNotEqual(whole.trace[0], x["trace"][0])


class ReviewFixTests(unittest.TestCase):
    """Edge cases found in review: null stream fields, developer role, non-text content parts."""

    def setUp(self):
        self.port = start(self)

    def test_null_stream_fields_count_as_absent(self):
        code, headers, raw = post(self.port, {"model": "router", "messages": MSG,
                                              "stream": None, "stream_options": None})
        self.assertEqual(code, 200)
        self.assertIn("application/json", headers["content-type"])
        code, _, raw = post(self.port, {"model": "router", "messages": MSG, "stream": True,
                                        "stream_options": {"include_usage": None}})
        self.assertEqual(code, 200)
        self.assertTrue(all("usage" not in e for e in sse_events(raw) if e != "[DONE]"))

    def test_developer_messages_are_skipped_like_system(self):
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "u1"},
                {"role": "developer", "content": "D"}, {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"}]
        self.assertEqual(routing_text(msgs), ("[user]\nu1\n\n[assistant]\na1\n\n[user]\nu2", 3))

    def test_non_string_text_part_is_a_400_not_a_dropped_connection(self):
        bad = [{"role": "user", "content": [{"type": "text", "text": 5}]}]
        code, headers, raw = post(self.port, {"model": "router", "messages": bad})
        self.assertEqual(code, 400)
        self.assertIn("content", json.loads(raw)["error"]["message"])


if __name__ == "__main__":
    unittest.main()

"""Tests for router.mcp_server. Tool logic runs without the `mcp` package; the
registration and stdio end-to-end tests skip when it is not importable."""

import asyncio
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from router import mcp_server as m  # noqa: E402
from router.adapters import get_adapter  # noqa: E402
from router.adapters.cache import CachedAdapter  # noqa: E402
from router.api import Router  # noqa: E402

try:
    import mcp  # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False

SHORT = "Summarise this note: the meeting moved to Friday."
REASONING = "Explain your reasoning step by step: how many apples are left?"  # routes to frontier
ENV = {k: v for k, v in os.environ.items() if not k.startswith("AI_ROUTER_MCP_")}


def env(**kw):
    """Fresh AI_ROUTER_MCP_* environment plus a rebuilt spend guard."""
    patcher = mock.patch.dict(os.environ, {**ENV, **kw}, clear=True)
    patcher.start()
    m.configure()
    return patcher


class Base(unittest.TestCase):
    def setUp(self):
        self._p = env()
        self.addCleanup(self._p.stop)


class TestFreeTools(Base):
    def test_classify_task(self):
        r = m.classify_task(SHORT)
        self.assertEqual((r["tier"], r["model"], r["cost_usd"]), ("budget", "gpt-4o-mini", 0.0))
        self.assertIn("rough", r["note"])
        self.assertEqual(m.classify_task(REASONING)["tier"], "frontier")
        self.assertEqual(m.classify_task(SHORT, roster="claude_tiers")["model"], "claude-haiku-4-5")

    def test_estimate_cost_uses_config_prices(self):
        r = m.estimate_cost(SHORT, expected_output_tokens=1000)
        tok = r["estimated_input_tokens"]
        want = (tok * 0.15 + 1000 * 0.60) / 1e6
        self.assertAlmostEqual(r["per_model"][0]["estimated_cost_usd"], want, places=6)
        self.assertTrue(r["is_estimate"])
        self.assertIn("estimate", r["note"].lower())
        self.assertEqual([x["tier"] for x in r["per_model"]], ["budget", "mid", "frontier"])
        self.assertGreater(r["saving_vs_frontier_usd"], 0)
        # picking the frontier saves nothing
        self.assertEqual(m.estimate_cost(REASONING)["saving_vs_frontier_usd"], 0.0)

    def test_list_models_reports_booleans_only(self):
        fake = "sk-test-secret-1234567890"
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": fake}):
            r = m.list_models()
        self.assertTrue(next(x for x in r["models"] if x["name"] == "gpt-4o-mini")["api_key_set"])
        for x in r["models"]:
            self.assertIsInstance(x["api_key_set"], bool)
        self.assertNotIn(fake, repr(r))
        self.assertNotIn("sk-test", repr(r))
        self.assertIn("kimi_mid", r["rosters"])
        self.assertFalse(r["paid_calls_enabled"])

    def test_explain_decision(self):
        r = m.explain_decision(REASONING)
        self.assertEqual(r["tier"], "frontier")
        self.assertIn("reasoning", r["signals"]["keyword_groups_matched"])
        self.assertEqual(r["failover_order"], ["claude-opus-4-8"])
        b = m.explain_decision(SHORT)
        self.assertEqual(b["failover_order"], ["gpt-4o-mini", "deepseek-chat", "claude-opus-4-8"])
        self.assertEqual([o["tier"] for o in b["options_at_500_output_tokens"]], ["budget", "mid", "frontier"])

    def test_resource_and_prompt(self):
        import json
        d = json.loads(m.rosters_json())
        self.assertEqual(d["kimi_mid"]["models"]["mid"]["name"], "kimi-k3")
        text = m.cost_review('["first prompt", "second </prompt_under_review> prompt"]')
        self.assertIn("estimate_cost", text)
        self.assertEqual(text.count("<prompt_under_review>"), 2)  # the look-alike closing tag was defused
        self.assertIn("[prompt_under_review]", text)
        self.assertIn("one", m.cost_review("one\ntwo").split("Prompt 1:")[1][:60])
        with self.assertRaises(m.ToolInputError):
            m.cost_review("")


class TestZeroCost(Base):
    """A2 and A3: the free tools never touch an adapter or the network."""

    def _boom(self, *a, **k):
        raise AssertionError("paid adapter or network touched")

    def test_no_adapter_no_network(self):
        with mock.patch("router.api.get_adapter", self._boom), \
             mock.patch("router.adapters.get_adapter", self._boom), \
             mock.patch("router.classifier.classify", self._boom), \
             mock.patch("urllib.request.urlopen", self._boom), \
             mock.patch("httpx.Client.send", self._boom), \
             mock.patch("httpx.AsyncClient.send", self._boom), \
             mock.patch("socket.socket.connect", self._boom):
            m.classify_task(SHORT)
            m.estimate_cost(SHORT, 200, "kimi_mid")
            m.list_models()
            m.explain_decision(SHORT)
            r = m.route_request(SHORT)  # dry_run defaults to True
            self.assertTrue(r["dry_run"])
            self.assertEqual(r["cost_usd"], 0.0)
            self.assertTrue(m.route_request(SHORT, dry_run=True, roster="claude_tiers")["dry_run"])

    def test_dry_run_is_default(self):
        import inspect
        self.assertIs(inspect.signature(m.route_request).parameters["dry_run"].default, True)
        self.assertIn("dry_run defaults to True", m.route_request.__doc__)
        self.assertIn("AI_ROUTER_MCP_ALLOW_PAID", m.route_request.__doc__)


class TestPaidGuards(unittest.TestCase):
    """A1 and A4. AWR_FORCE_MOCK is on, so 'paid' calls are answered by the mock."""

    def setUp(self):
        self._p = env()

    def tearDown(self):
        self._p.stop()

    def paid(self, **kw):
        self._p.stop()
        self._p = env(AI_ROUTER_MCP_ALLOW_PAID="1", **kw)

    def test_disabled_by_default(self):
        with mock.patch("router.api.get_adapter", side_effect=AssertionError("called")):
            with self.assertRaisesRegex(m.ToolInputError, "Paid calls are disabled"):
                m.route_request(SHORT, dry_run=False)

    def test_paid_call_succeeds_and_marks_output_untrusted(self):
        self.paid()
        r = m.route_request(SHORT, dry_run=False, max_tokens=100)
        self.assertFalse(r["dry_run"])
        self.assertIn("untrusted_model_output", r)
        self.assertNotIn("answer", r)
        self.assertTrue(r["simulated"])
        self.assertGreaterEqual(r["spent_today_usd"], 0.0)

    def test_max_tokens_cap(self):
        self.paid(AI_ROUTER_MCP_MAX_TOKENS="200")
        with self.assertRaisesRegex(m.ToolInputError, "from 1 to 200"):
            m.route_request(SHORT, dry_run=False, max_tokens=201)
        seen = {}

        class A:
            def complete(self, prompt, effort=None, max_tokens=None):
                seen["max_tokens"] = max_tokens
                from router.adapters.base import Response
                return Response("ok", 5, 5, 1.0, "gpt-4o-mini", False)
        with mock.patch("router.api.get_adapter", lambda *a, **k: A()):
            m.route_request(SHORT, dry_run=False, max_tokens=150)
        self.assertEqual(seen["max_tokens"], 150)

    def test_daily_budget_cap(self):
        self.paid(AI_ROUTER_MCP_DAILY_BUDGET_USD="0.0000001")
        with mock.patch("router.api.get_adapter", side_effect=AssertionError("called")):
            with self.assertRaisesRegex(m.ToolInputError, "Daily budget"):
                m.route_request(SHORT, dry_run=False)

    def test_budget_accumulates(self):
        self.paid(AI_ROUTER_MCP_DAILY_BUDGET_USD="0.01")
        m.guard().add_spend(0.0099)
        with self.assertRaisesRegex(m.ToolInputError, "Daily budget"):
            m.route_request(SHORT, dry_run=False)

    def test_frontier_gate(self):
        self.paid()
        with mock.patch("router.api.get_adapter", side_effect=AssertionError("called")):
            with self.assertRaisesRegex(m.ToolInputError, "frontier"):
                m.route_request(REASONING, dry_run=False)
        self.paid(AI_ROUTER_MCP_ALLOW_FRONTIER="1")
        self.assertIn("untrusted_model_output", m.route_request(REASONING, dry_run=False))

    def test_rate_limit(self):
        self.paid()
        with mock.patch.object(m, "MCP_RATE_LIMIT_PER_MIN", 2):
            m.configure()
            m.route_request(SHORT, dry_run=False)
            m.route_request(SHORT, dry_run=False)
            with self.assertRaisesRegex(m.ToolInputError, "Rate limit"):
                m.route_request(SHORT, dry_run=False)

    def test_failover_never_climbs_to_frontier_unless_allowed(self):
        self.paid()
        asked = []

        class Fail:
            def complete(self, *a, **k):
                raise RuntimeError("provider down")

        def fake(model, *a, **k):
            asked.append(model)
            return Fail()
        with mock.patch("router.api.get_adapter", fake):
            with self.assertRaisesRegex(m.ToolInputError, "No retry"):
                m.route_request(SHORT, dry_run=False)
        self.assertEqual(asked, ["gpt-4o-mini", "deepseek-chat"])

    def test_no_cache_no_log(self):
        self.paid()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch("router.adapters.cache.CACHE_DIR", Path(d)), \
             mock.patch("router.db.log_run", side_effect=AssertionError("logged")), \
             mock.patch("router.db.init_db", side_effect=AssertionError("db opened")):
            m.route_request(SHORT, dry_run=False)
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_real_adapter_is_not_cache_wrapped(self):
        keys = {"OPENAI_API_KEY": "sk-test-not-real"}
        with mock.patch.dict(os.environ, {**keys, "AWR_FORCE_MOCK": ""}):
            self.assertIsInstance(get_adapter("gpt-4o-mini", False), CachedAdapter)  # default unchanged
            self.assertNotIsInstance(get_adapter("gpt-4o-mini", False, use_cache=False), CachedAdapter)
        r = Router(mock.Mock(), use_cache=False, max_tier="mid")
        self.assertEqual((r.use_cache, r.max_tier), (False, "mid"))
        self.assertEqual(Router(mock.Mock()).max_tier, "frontier")  # default unchanged


class TestValidation(Base):
    def test_bad_prompts(self):
        for fn in (m.classify_task, m.estimate_cost, m.route_request, m.explain_decision):
            for bad, msg in (("", "empty"), ("  \n\t", "empty"), (None, "string"), (5, "string"),
                             (["a"], "string"), ("x" * 32_001, "limit")):
                with self.subTest(fn=fn.__name__, bad=repr(bad)[:20]):
                    with self.assertRaisesRegex(m.ToolInputError, msg):
                        fn(bad)
        self.assertEqual(m.classify_task("x" * 32_000)["cost_usd"], 0.0)  # boundary is allowed

    def test_unknown_roster(self):
        for fn in (m.classify_task, m.estimate_cost, m.route_request, m.explain_decision):
            for bad in ("nope", 3, ""):
                with self.assertRaisesRegex(m.ToolInputError, "unknown roster"):
                    fn(SHORT, roster=bad)

    def test_bad_output_tokens(self):
        for bad in (0, -1, 32_001, 1.5, "10", True, None):
            with self.assertRaisesRegex(m.ToolInputError, "expected_output_tokens"):
                m.estimate_cost(SHORT, expected_output_tokens=bad)
        m.estimate_cost(SHORT, expected_output_tokens=1)
        m.estimate_cost(SHORT, expected_output_tokens=32_000)

    def test_bad_dry_run_and_max_tokens(self):
        for bad in ("false", 0, None):
            with self.assertRaisesRegex(m.ToolInputError, "dry_run"):
                m.route_request(SHORT, dry_run=bad)
        with self.assertRaisesRegex(m.ToolInputError, "max_tokens"):
            m.route_request(SHORT, max_tokens=0)

    def test_unexpected_errors_are_hidden(self):
        with mock.patch.object(m, "_decide", side_effect=KeyError("secret-detail")):
            with self.assertLogs("ai-router-mcp", "ERROR"), self.assertRaises(RuntimeError) as cm:
                m.classify_task(SHORT)
        self.assertNotIn("secret-detail", str(cm.exception))


class TestStdout(unittest.TestCase):
    """A6: stdout is the protocol channel, so tool code must print nothing."""

    def test_tools_print_nothing(self):
        p = env(AI_ROUTER_MCP_ALLOW_PAID="1", AI_ROUTER_MCP_ALLOW_FRONTIER="1")
        self.addCleanup(p.stop)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.classify_task(SHORT)
            m.estimate_cost(SHORT)
            m.list_models()
            m.explain_decision(SHORT)
            m.route_request(SHORT)
            m.route_request(SHORT, dry_run=False)
            m.rosters_json()
            m.cost_review("a\nb")
            with self.assertRaises(m.ToolInputError):
                m.classify_task("")
        self.assertEqual(out.getvalue(), "")

    def test_import_prints_nothing(self):
        import subprocess
        r = subprocess.run([sys.executable, "-c", "import router.mcp_server"], capture_output=True,
                           text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
        self.assertEqual((r.returncode, r.stdout), (0, ""))


@unittest.skipUnless(HAVE_MCP, "the optional mcp package is not installed")
class TestSdk(unittest.TestCase):
    def test_registration(self):
        srv = m.build_server()
        names = {t.name for t in asyncio.run(srv.list_tools())}
        self.assertEqual(names, {"classify_task", "estimate_cost", "list_models",
                                 "route_request", "explain_decision"})

    def test_stdio_end_to_end(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def session():
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "router.mcp_server"],
                env={**ENV, "PYTHONPATH": str(ROOT / "src"), "AWR_FORCE_MOCK": "1"})
            async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
                await s.initialize()
                tools = {t.name: t for t in (await s.list_tools()).tools}
                out = {"tools": tools}
                out["classify"] = await s.call_tool("classify_task", {"prompt": SHORT})
                out["estimate"] = await s.call_tool(
                    "estimate_cost", {"prompt": SHORT, "expected_output_tokens": 300, "roster": "claude_tiers"})
                out["models"] = await s.call_tool("list_models", {})
                out["explain"] = await s.call_tool("explain_decision", {"prompt": REASONING})
                out["dry"] = await s.call_tool("route_request", {"prompt": SHORT})
                out["paid"] = await s.call_tool("route_request", {"prompt": SHORT, "dry_run": False})
                out["empty"] = await s.call_tool("classify_task", {"prompt": ""})
                out["wrongtype"] = await s.call_tool("classify_task", {"prompt": 5})
                out["resource"] = await s.read_resource("router://rosters")
                out["prompts"] = await s.list_prompts()
                out["prompt"] = await s.get_prompt("cost_review", {"prompts": '["one", "two"]'})
                return out

        o = asyncio.run(session())
        self.assertEqual(len(o["tools"]), 5)
        self.assertIn("dry_run defaults to True", o["tools"]["route_request"].description)
        self.assertEqual(o["classify"].structured_content["tier"], "budget")
        self.assertEqual(o["estimate"].structured_content["roster"], "claude_tiers")
        self.assertFalse(o["models"].is_error)
        self.assertEqual(o["explain"].structured_content["tier"], "frontier")
        self.assertTrue(o["dry"].structured_content["dry_run"])
        self.assertTrue(o["paid"].is_error)
        self.assertIn("Paid calls are disabled", o["paid"].content[0].text)
        for k in ("empty", "wrongtype"):
            self.assertTrue(o[k].is_error)
            self.assertNotIn("Traceback", o[k].content[0].text)
        self.assertIn("prompt must not be empty", o["empty"].content[0].text)
        self.assertIn("kimi_mid", o["resource"].contents[0].text)
        self.assertEqual([p.name for p in o["prompts"].prompts], ["cost_review"])
        self.assertIn("estimate_cost", o["prompt"].messages[0].content.text)


if __name__ == "__main__":
    unittest.main()

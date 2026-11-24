"""`awr bench` (bring your own workload). Offline: mock mode, or every provider stubbed.

The live path is covered by these stubbed tests only; no paid call is made.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import analyze  # noqa: E402
import run_benchmark  # noqa: E402
from router import adapters, cli, scoring  # noqa: E402
from router.adapters.base import Adapter, Response  # noqa: E402
from router.api import FINAL_LINE  # noqa: E402
from router.benchmark import byo, guards, stats  # noqa: E402
from router.config import ROSTERS, get_model  # noqa: E402
from router.scoring import JudgeError  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "byo_mixed.jsonl"
SENTINEL = "SENTINEL-7f3a-do-not-print"


def response(text="Answer: 12", simulated=False, success=True, model="claude-opus-4-8"):
    return Response(text=text, input_tokens=10, output_tokens=10, latency_ms=1.0,
                    model=model, simulated=simulated, success=success)


class Stub(Adapter):
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def complete(self, prompt, effort=None, max_tokens=None):
        self.calls += 1
        return self.reply


class Never:
    """A get_adapter stand-in that fails the test if it is ever called."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        raise AssertionError("an adapter was requested")


def write_jsonl(folder, lines, name="w.jsonl"):
    path = Path(folder) / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def live_env():
    env = {k: v for k, v in os.environ.items() if k != "AWR_FORCE_MOCK"}
    return mock.patch.dict(os.environ, env, clear=True)


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class Loader(unittest.TestCase):
    def refused(self, lines, expect_line, key_hint=""):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(byo.BYOError) as ctx:
                byo.load_rows(write_jsonl(tmp, lines), "cross_vendor")
        msg = str(ctx.exception)
        if expect_line is not None:
            self.assertIn(f"line {expect_line}:", msg)
        self.assertIn(key_hint, msg)
        self.assertNotIn(SENTINEL, msg)
        return msg

    def ok(self, extra=None):
        return json.dumps({"prompt": f"ok {SENTINEL}", **(extra or {})})

    def test_every_refusal_names_its_line_and_never_a_value(self):
        p = f"Question {SENTINEL}"
        cases = [
            ([self.ok(), f"not json {SENTINEL}"], 2, ""),
            ([self.ok(), json.dumps([p])], 2, "JSON object"),
            ([self.ok(), json.dumps({"prompt": p, "refernce": SENTINEL})], 2, "'refernce'"),
            ([json.dumps({"prompt": p, "x" * 100: 1})], 1, "'" + "x" * 40 + "'"),
            ([json.dumps({"reference": SENTINEL})], 1, "'prompt'"),
            ([json.dumps({"prompt": "   "})], 1, "'prompt'"),
            ([json.dumps({"prompt": [{"role": "user", "content": SENTINEL}]})], 1, "multi-turn"),
            ([json.dumps({"prompt": {"text": SENTINEL}})], 1, "multi-turn"),
            ([json.dumps({"prompt": 7})], 1, "'prompt'"),
            ([json.dumps({"prompt": p, "id": f"bad id {SENTINEL}"})], 1, "'id'"),
            ([json.dumps({"prompt": p, "id": "a"}), "", json.dumps({"prompt": p, "id": "a"})], 3, "line 1"),
            ([json.dumps({"prompt": p, "reference": " "})], 1, "'reference'"),
            ([json.dumps({"prompt": p, "scoring": "exact_match"})], 1, "'scoring'"),
            ([json.dumps({"prompt": p, "reference": SENTINEL, "scoring": "fuzzy"})], 1, "'scoring'"),
            ([json.dumps({"prompt": p, "task_type": f"Support {SENTINEL}"})], 1, "'task_type'"),
            ([json.dumps({"prompt": p, "difficulty": "x" * 33})], 1, "'difficulty'"),
            ([self.ok(), json.dumps({"prompt": SENTINEL + "x" * 2_000_000})], 2, "too long"),
        ]
        for lines, line, hint in cases:
            with self.subTest(hint=hint, line=line):
                self.refused(lines, line, hint)

    def test_file_limits(self):
        self.refused([self.ok()] * (byo.MAX_ROWS + 1), byo.MAX_ROWS + 1, "rows")
        self.refused([""], None, "no rows")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.jsonl"
            path.write_bytes(b" " * (byo.MAX_FILE_BYTES + 1))
            with self.assertRaises(byo.BYOError) as ctx:
                byo.load_rows(path, "cross_vendor")
            self.assertIn("MB", str(ctx.exception))
            bad = Path(tmp) / "latin1.jsonl"
            bad.write_bytes(b'{"prompt": "caf\xe9"}\n')
            with self.assertRaises(byo.BYOError):
                byo.load_rows(bad, "cross_vendor")

    def test_the_fixture_loads_with_ids_suffixes_labels_and_planned_sources(self):
        rows = byo.load_rows(FIXTURE, "cross_vendor")
        self.assertEqual(len(rows), 5)
        final, exact, judged, plain, labelled = rows
        self.assertTrue(final["prompt"].endswith(FINAL_LINE))
        self.assertEqual(final["prompt"].count("Answer:"), 1)
        self.assertEqual(final["scoring"], "final_answer")
        self.assertFalse(exact["prompt"].endswith(FINAL_LINE))
        self.assertEqual(exact["scoring"], "exact_match")
        self.assertEqual((judged["scoring"], judged["user_rubric"]), ("rubric_judge", True))
        self.assertEqual((plain["scoring"], plain["reference"]), ("rubric_judge", byo.NO_REFERENCE_RUBRIC))
        self.assertFalse(plain["user_rubric"])
        self.assertEqual([r["source"] for r in rows], ["reference", "reference", "judge", "judge", "judge"])
        self.assertEqual((labelled["task_type"], labelled["difficulty"]), ("support_email", "easy"))
        self.assertTrue(plain["task_type"].endswith(" (predicted)"))

    def test_ids_are_generated_by_line_order_and_a_prompt_with_an_answer_line_keeps_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = byo.load_rows(write_jsonl(tmp, [
                json.dumps({"prompt": "a"}), "", json.dumps({"prompt": "b", "id": "byo-0003"}),
                json.dumps({"prompt": "c Answer: x", "reference": "x"}),
            ]), "cross_vendor")
        self.assertEqual([r["id"] for r in rows], ["byo-0001", "byo-0003", "byo-0004"])
        self.assertEqual(rows[2]["prompt"], "c Answer: x")


class Estimator(unittest.TestCase):
    def test_matches_a_hand_worked_row(self):
        prompt = "x" * 35  # 35 / 3.5 + 1 = 11 tokens as sent
        task = {"id": "t", "prompt": prompt, "reference": "r", "scoring": "exact_match",
                "source": "reference", "user_rubric": False}
        roster = ROSTERS["cross_vendor"]

        def usd(model, tokens):
            m = get_model(model)
            return (tokens * m.cost_per_1m_input_tokens + 1024 * m.cost_per_1m_output_tokens) / 1e6

        tokens = lambda text: int(len(text) / 3.5) + 1
        c = prompt + FINAL_LINE
        e = c + "\nReply with only that final line, with no explanation."
        classification = usd(roster.budget, 11) + usd(roster.mid, 11) + usd(roster.frontier, 11)
        reasoning = (usd("claude-haiku-5-5", tokens(c)) + usd(roster.mid, tokens(c))
                     + usd(roster.budget, tokens(c)) + usd("claude-sonnet-5-5", tokens(c)))
        extraction = usd("claude-haiku-5-5", tokens(e)) + usd(roster.mid, tokens(e)) + usd("claude-sonnet-5-5", tokens(e))
        router, frontier, judge = byo.row_worst_case(task, "cross_vendor")
        self.assertAlmostEqual(router, max(classification, reasoning, extraction, usd(roster.frontier, 11)))
        self.assertAlmostEqual(frontier, usd(roster.frontier, 11))
        self.assertEqual(judge, 0.0)

        judged = {**task, "source": "judge", "scoring": "rubric_judge"}
        judge_in = scoring._JUDGE_PROMPT_TEMPLATE.format(task_prompt=prompt, reference="r", candidate="")
        self.assertAlmostEqual(byo.row_worst_case(judged, "cross_vendor")[2],
                               2 * usd("claude-opus-4-8", tokens(judge_in) + 1024))

    def test_the_fixture_estimate_plans_two_reference_and_three_judge_rows(self):
        est = byo.estimate(byo.load_rows(FIXTURE, "cross_vendor"), "cross_vendor")
        self.assertEqual(est.planned, {"reference": 2, "judge": 3})
        self.assertEqual(est.user_rubrics, 1)
        self.assertGreater(est.reserve_usd, est.max_row_usd)
        self.assertAlmostEqual(est.total_usd, est.router_usd + est.frontier_usd + est.judge_usd)


class ScoringSeam(unittest.TestCase):
    """The reference scorers row by row (a mock run scores every row with the mock judge)."""

    def setUp(self):
        self.rows = byo.load_rows(FIXTURE, "cross_vendor")

    def test_reference_rows_are_checked_by_code(self):
        final, exact = self.rows[0], self.rows[1]
        self.assertEqual(byo.score_row(final, response("Working: 36 - 12.\nAnswer: 12"), strict=True), 1.0)
        self.assertEqual(byo.score_row(final, response("12 is close.\nAnswer: 24"), strict=True), 0.0)
        self.assertEqual(byo.score_row(exact, response("The label is shipping."), strict=True), 1.0)
        self.assertEqual(byo.row_source(final, response()), "reference")

    def test_judged_rows_use_the_real_judge_without_the_disk_cache(self):
        calls = []

        def factory(model, *a, **k):
            calls.append((model, k))
            return Stub(response("0.8"))

        with mock.patch.object(adapters, "get_adapter", factory), \
                mock.patch.object(scoring, "_real_judge_available", return_value=True):
            self.assertEqual(byo.score_row(self.rows[3], response("An answer."), strict=True), 0.8)
        self.assertEqual(calls, [("claude-opus-4-8", {"use_cache": False})])
        self.assertEqual(byo.row_source(self.rows[3], response()), "judge")

    def test_a_simulated_answer_gets_the_mock_judge_on_predicted_labels(self):
        row = self.rows[4]  # support_email is not a known type: the mock judge uses the prediction
        sim = response("x", simulated=True)
        self.assertEqual(byo.score_row(row, sim), scoring.rubric_judge(
            {"id": row["id"], "task_type": row["pred_type"], "difficulty": row["pred_difficulty"]}, sim))
        self.assertEqual(byo.row_source(row, sim), "simulated")

    def test_score_keeps_its_cache_default(self):
        seen = []
        with mock.patch.object(adapters, "get_adapter", lambda m, *a, **k: seen.append(k) or Stub(response("0.9"))), \
                mock.patch.object(scoring, "_real_judge_available", return_value=True):
            scoring.score({"prompt": "p", "reference": "r", "scoring": "rubric_judge"}, response())
        self.assertEqual(seen, [{}])


class Verdict(unittest.TestCase):
    def test_rules(self):
        good, bad, mid = (60.0, 45.0, 70.0), (20.0, 10.0, 30.0), (50.0, 30.0, 60.0)
        q_good, q_mid = (99.0, 96.0, 100.0), (96.0, 90.0, 99.0)
        self.assertEqual(byo.verdict(40, False, good, q_good), "meets")
        self.assertEqual(byo.verdict(40, False, bad, q_good), "misses")
        self.assertEqual(byo.verdict(40, False, mid, q_mid), "not settled")
        self.assertEqual(byo.verdict(40, False, good, None), "not settled")
        self.assertIn("too few rows for a verdict", byo.verdict(29, False, good, q_good))
        self.assertIn("simulated", byo.verdict(40, True, good, q_good))

    def test_a_zero_frontier_mean_prints_undefined_instead_of_crashing(self):
        pairs = [(1.0, 0.5, 0.0, 0.5)] * 3
        self.assertIsNone(byo._ranged(pairs, stats.retention))
        self.assertIn("undefined", byo._fmt(None))


class SharedPieces(unittest.TestCase):
    def test_analyze_and_run_benchmark_reexport_the_moved_names(self):
        for name in ("bootstrap", "net_reduction", "retention", "N_BOOT", "SEED"):
            self.assertIs(getattr(analyze, name), getattr(stats, name))
        for name in ("_TASK_RESERVE_USD", "_spend_cap", "RunStopped", "_check_real"):
            self.assertIs(getattr(run_benchmark, name), getattr(guards, name))

    def test_preflight_extra_models(self):
        seen = []
        factory = lambda m, *a, **k: seen.append(m) or Stub(response("OK"))
        guards._preflight("cross_vendor", extra_models=("claude-haiku-5-5",), adapter_factory=factory)
        r = ROSTERS["cross_vendor"]
        self.assertEqual(seen, [r.budget, r.mid, r.frontier, "claude-haiku-5-5"])


class Paths(unittest.TestCase):
    def test_out_is_refused_inside_data_or_when_it_exists_or_is_not_markdown(self):
        for bad in (ROOT / "data" / "benchmark_report.md", ROOT / "data" / "x" / "new.md"):
            with self.assertRaises(byo.BYOError):
                byo.check_out_path(str(bad))
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "r.md"
            existing.write_text("keep")
            with self.assertRaises(byo.BYOError):
                byo.check_out_path(str(existing))
            with self.assertRaises(byo.BYOError):
                byo.check_out_path(str(Path(tmp) / "r.txt"))
            self.assertEqual(byo.check_out_path(str(Path(tmp) / "new.md")), (Path(tmp) / "new.md").resolve())
        self.assertTrue(byo.check_out_path(str(ROOT / "data" / "byo" / "mine-unused-name.md")))

    def test_no_byo_name_equals_any_benchmark_report_path(self):
        published = set()
        for roster in ROSTERS:
            for real in (True, False):
                for tags in ([], ["classified"], ["agree", "pinned", "consensus", "lookup"], ["learned"]):
                    for strategy in ("router", "cascade", "jury"):
                        for tasks in (None, "data/tasks_hard.json", "byo_mixed.jsonl"):
                            for effort in (None, {"mid": "low"}):
                                published.add(run_benchmark._report_path(
                                    roster, real, tags, effort, strategy, tasks).resolve())
        for sim in (True, False):
            path = byo.default_report_path(byo.report_stem(FIXTURE), "byo-20261124T000000-abcd1234", sim)
            self.assertNotIn(path.resolve(), published)
            self.assertEqual(path.parent, byo.BYO_DIR)
            self.assertEqual("_byo_simulated_" in path.name, sim)
        self.assertEqual(byo.report_stem("my file (v2)!.jsonl"), "myfilev2")
        self.assertEqual(len(byo.report_stem("a" * 99 + ".jsonl")), 40)


class Isolated(unittest.TestCase):
    """Run log, report folder and disk cache all point at temp folders."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.byo_dir = self.tmp / "byo"
        self.cache = self.tmp / "cache"
        self.cache.mkdir()
        for patcher in (mock.patch.object(byo, "BYO_DIR", self.byo_dir),
                        mock.patch("router.adapters.cache.CACHE_DIR", self.cache),
                        mock.patch.object(adapters.SpendMeter, "usd", 0.0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        runs = ROOT / "data" / "runs.db"
        self.runs_before = runs.stat().st_mtime_ns if runs.exists() else None

    def assert_untouched(self):
        runs = ROOT / "data" / "runs.db"
        self.assertEqual(runs.stat().st_mtime_ns if runs.exists() else None, self.runs_before)
        self.assertEqual(list(self.cache.iterdir()), [])

    def reports(self):
        return sorted(self.byo_dir.glob("*.md")) if self.byo_dir.exists() else []


class MockRun(Isolated):
    def test_end_to_end_on_the_fixture(self):
        with mock.patch.dict(os.environ, {"AWR_FORCE_MOCK": "1"}):
            code, out, err = run_cli(["bench", str(FIXTURE)])
        self.assertEqual(code, 0, err)
        dry, _, report = out.partition("# Bring your own workload")
        for text in ("Dry run: 5 rows", "router arm (default rule)", "frontier_only arm",
                     "judge (claude-opus-4-8", "2 reference, 3 judge (1 with your rubric)"):
            self.assertIn(text, dry)
        for text in ("**SIMULATED**", "**Cost cut** (net of routing cost):", "**Quality kept**",
                     "99% range", "| simulated | 5 of 5 |", "| reference | 0 of 5 |",
                     "| support_email |", "(predicted)", "too few rows for a verdict", byo.CLOSING_LINE):
            self.assertIn(text, report)
        self.assertNotIn("Hypothesis", report)
        written = self.reports()
        self.assertEqual(len(written), 1)
        self.assertIn("_byo_simulated_byo_mixed_byo-", written[0].name)
        self.assertTrue((self.byo_dir / byo.RUNS_DB_NAME).exists())
        saved = written[0].read_text()
        for line in FIXTURE.read_text().splitlines():
            for field in ("prompt", "reference"):
                value = json.loads(line).get(field)
                if value and len(value) > 20:  # short keys such as "12" match by chance
                    self.assertNotIn(value, out + saved)
        self.assert_untouched()

    def test_out_path_is_used_and_never_overwritten(self):
        target = self.tmp / "mine.md"
        with mock.patch.dict(os.environ, {"AWR_FORCE_MOCK": "1"}):
            self.assertEqual(run_cli(["bench", str(FIXTURE), "--out", str(target)])[0], 0)
            self.assertTrue(target.exists())
            self.assertEqual(run_cli(["bench", str(FIXTURE), "--out", str(target)])[0], 2)
        self.assert_untouched()


class LivePath(Isolated):
    def test_max_spend_is_refused_in_mock_mode(self):
        with mock.patch.dict(os.environ, {"AWR_FORCE_MOCK": "1"}), \
                mock.patch.object(byo, "run") as run:
            code, out, err = run_cli(["bench", str(FIXTURE), "--max-spend", "1.00"])
        self.assertEqual(code, 2)
        self.assertIn("--max-spend is for live runs", err)
        run.assert_not_called()

    def test_without_a_mode_the_dry_run_prints_and_the_command_fails(self):
        never = Never()
        with live_env(), mock.patch("router.api.get_adapter", never), \
                mock.patch.object(guards, "get_adapter", never), mock.patch.object(byo, "run") as run:
            code, out, err = run_cli(["bench", str(FIXTURE)])
        self.assertNotEqual(code, 0)
        self.assertIn("Dry run: 5 rows", out)
        self.assertIn("AWR_FORCE_MOCK=1", err)
        self.assertIn("--max-spend", err)
        run.assert_not_called()
        self.assertEqual(never.calls, 0)

    def test_dry_run_exits_zero_with_no_run(self):
        for env in (live_env(), mock.patch.dict(os.environ, {"AWR_FORCE_MOCK": "1"})):
            with env, mock.patch.object(byo, "run") as run:
                code, out, _ = run_cli(["bench", str(FIXTURE), "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("Dry run: 5 rows", out)
            run.assert_not_called()
        self.assertEqual(self.reports(), [])

    def test_a_worst_case_above_the_cap_is_refused_before_any_adapter_call(self):
        never = Never()
        with live_env(), mock.patch("router.api.get_adapter", never), \
                mock.patch.object(adapters, "get_adapter", never), \
                mock.patch.object(guards, "get_adapter", never):
            code, out, err = run_cli(["bench", str(FIXTURE), "--max-spend", "0.11"])
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", err)
        self.assertEqual(never.calls, 0)
        self.assertEqual(self.reports(), [])
        self.assert_untouched()

    def stopped(self, answer_factory, judge_factory=None):
        preflight_models = []

        def preflight(model, *a, **k):
            preflight_models.append((model, k))
            return Stub(response("OK"))

        with live_env(), mock.patch.object(guards, "get_adapter", preflight), \
                mock.patch("router.api.get_adapter", answer_factory), \
                mock.patch.object(adapters, "get_adapter", judge_factory or Never()), \
                mock.patch.object(scoring, "_real_judge_available", return_value=True):
            code, out, err = run_cli(["bench", str(FIXTURE), "--max-spend", "5"])
        self.assertIn("STOPPED:", str(code))
        self.assertIn("No report written.", str(code))
        self.assertEqual(self.reports(), [])
        self.assert_untouched()
        return str(code), preflight_models

    def test_preflight_covers_haiku_and_sonnet_5_5_and_a_simulated_answer_stops_the_run(self):
        calls = []

        def answer(model, *a, **k):
            calls.append(k)
            return Stub(response("Answer: 12", simulated=True, model=model))

        msg, models = self.stopped(answer)
        self.assertIn("an answer was simulated", msg)
        r = ROSTERS["cross_vendor"]
        self.assertEqual([m for m, _ in models],
                         [r.budget, r.mid, r.frontier, "claude-haiku-5-5", "claude-sonnet-5-5"])
        self.assertTrue(all(k == {"use_cache": False} for _, k in models))
        self.assertTrue(calls and all(k == {"use_cache": False} for k in calls))

    def test_a_prose_judge_reply_stops_the_run(self):
        answer = lambda model, *a, **k: Stub(response("The answer.\nAnswer: 12", model=model))
        judge = lambda model, *a, **k: Stub(response("A fine answer, about 0.8 out of 1."))
        msg, _ = self.stopped(answer, judge)
        self.assertIn("no real judge score", msg)

    def test_judge_error_and_run_stopped_are_the_shared_types(self):
        self.assertIs(cli.RunStopped, run_benchmark.RunStopped)
        self.assertIs(cli.JudgeError, JudgeError)


if __name__ == "__main__":
    unittest.main()

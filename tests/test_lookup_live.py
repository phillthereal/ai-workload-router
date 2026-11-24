"""Tests for the opt-in live-learning lookup (router.lookup.from_log, --lookup-from-log)."""

import contextlib
import io
import itertools
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import uuid
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from router import db  # noqa: E402
from router.lookup import RefreshingLookup, from_log, public_lookup  # noqa: E402
from router.scoring import DIFFICULTIES, TASK_TYPES  # noqa: E402
from router.server import main, make_server  # noqa: E402

# 60 prompts in a vocabulary the public tasks lack (beekeeping logs), all carrying one label.
THINGS = ["varroa mites", "brood frames", "queen excluder", "nectar flow", "honey supers", "drone comb",
          "propolis traps", "swarm cells", "smoker fuel", "apiary windbreaks"]
VERBS = ["Log the hive inspection note about", "Record the apiary note about",
         "From the beekeeper's logbook, note", "Write down the hive tally for",
         "Note the colony count of", "Tally the apiary entry for"]
BEE_PROMPTS = [f"{v} {t} in hive {i}." for i, (v, t) in enumerate(itertools.product(VERBS, THINGS))]
LIVE_LABEL = ("short_generation", "easy")
# Held out: not among BEE_PROMPTS. The public lookup calls it extraction (checked below).
HELD_OUT = "Tally the beekeeper's logbook entry for capped brood and varroa mites in hive 99."

# The runs table as an old db has it: every column up to quality_source, no prompt_text yet.
OLD_SCHEMA = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_group TEXT NOT NULL, strategy TEXT NOT NULL, task_id TEXT NOT NULL,
    task_type TEXT NOT NULL, difficulty TEXT NOT NULL, model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL, latency_ms REAL NOT NULL, quality_score REAL NOT NULL,
    success INTEGER NOT NULL, simulated INTEGER NOT NULL DEFAULT 0,
    response_text TEXT NOT NULL DEFAULT '', effort TEXT,
    roster TEXT NOT NULL DEFAULT 'cross_vendor', routing_cost_usd REAL NOT NULL DEFAULT 0.0,
    classifier_agreed INTEGER, verifier_scores TEXT, created_at TEXT,
    learned_evidence TEXT, quality_source TEXT
)
"""


def log_live(path, prompt, label=LIVE_LABEL, **over):
    kw = dict(run_group="live", strategy="live:router", task_id=str(uuid.uuid4()),
              task_type=label[0], difficulty=label[1], model="m", input_tokens=1, output_tokens=1,
              cost_usd=0.0, latency_ms=1.0, quality_score=0.9, success=True, simulated=False,
              quality_source="user", prompt_text=prompt, db_path=path)
    kw.update(over)
    db.init_db(path)  # idempotent; log_run expects the table to exist
    return db.log_run(**kw)


def rows(path):
    return db.live_labelled_rows(path, task_types=TASK_TYPES, difficulties=DIFFICULTIES)


class FromLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "runs.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_learns_a_label_the_public_set_gets_wrong(self):
        for p in BEE_PROMPTS:
            log_live(self.path, p)
        q = {"prompt": HELD_OUT}
        public = public_lookup().classify(q)
        self.assertEqual(public.task_type, "extraction")  # the public lookup's actual mislabel
        self.assertNotEqual(public.task_type, LIVE_LABEL[0])
        live = from_log(self.path)
        self.assertGreaterEqual(live.live_rows, 60)
        got = live.classify(q)
        self.assertEqual((got.task_type, got.difficulty), LIVE_LABEL)

    def test_below_min_rows_is_public_only(self):
        for p in BEE_PROMPTS[:10]:
            log_live(self.path, p)
        live = from_log(self.path)
        self.assertEqual(live.live_rows, 0)
        q = {"prompt": HELD_OUT}
        self.assertEqual(live.classify(q), public_lookup().classify(q))

    def test_exclusions(self):
        log_live(self.path, "good user row")
        log_live(self.path, "good verifier row", quality_source="verifier", quality_score=0.7)
        log_live(self.path, "pending row", quality_source=db.SOURCE_PENDING, quality_score=0.0)
        log_live(self.path, "no source row", quality_source=None)
        log_live(self.path, "simulated row", simulated=True)
        log_live(self.path, "low score row", quality_score=0.69)
        log_live(self.path, "", quality_score=0.9)
        log_live(self.path, None)
        log_live(self.path, "unknown label row", label=("unknown", "easy"))
        log_live(self.path, "bad difficulty row", label=("reasoning", "unknown"))
        log_live(self.path, "benchmark row", strategy="router")
        got = rows(self.path)
        self.assertEqual([r["prompt"] for r in got], ["good user row", "good verifier row"])
        self.assertEqual(set(got[0]), {"prompt", "task_type", "difficulty", "quality_score",
                                       "quality_source", "created_at"})

    def test_dedup_keeps_latest_label(self):
        log_live(self.path, "same prompt", label=("classification", "medium"))
        log_live(self.path, "same prompt", label=("reasoning", "hard"))
        for p in BEE_PROMPTS[:50]:
            log_live(self.path, p)
        self.assertEqual(len(rows(self.path)), 52)
        live = from_log(self.path)
        self.assertEqual(live.live_rows, 51)
        i = len(live.labels) - live.live_rows  # live tasks follow the public ones
        live_labels = dict(zip(["same prompt"] + BEE_PROMPTS[:50], live.labels[i:]))
        self.assertEqual(live_labels["same prompt"], ("reasoning", "hard"))

    def test_missing_db_is_not_created(self):
        self.assertEqual(rows(self.path), [])
        self.assertEqual(from_log(self.path).live_rows, 0)
        self.assertFalse(self.path.exists())

    def test_old_schema_without_prompt_text(self):
        conn = sqlite3.connect(self.path)
        conn.execute(OLD_SCHEMA)
        conn.execute("INSERT INTO runs (run_group, strategy, task_id, task_type, difficulty, model, "
                     "input_tokens, output_tokens, cost_usd, latency_ms, quality_score, success, "
                     "quality_source) VALUES ('live', 'live:router', 'x', 'reasoning', 'hard', 'm', "
                     "1, 1, 0, 1, 0.9, 1, 'user')")
        conn.commit()
        conn.close()
        self.assertEqual(rows(self.path), [])
        live = from_log(self.path)
        self.assertEqual(live.live_rows, 0)
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
        self.assertNotIn("prompt_text", cols)  # read-only: no migration ran


class RefreshingLookupTests(unittest.TestCase):
    def test_rebuilds_every_n_calls(self):
        calls = []

        def build(path, min_rows):
            calls.append((path, min_rows))
            return public_lookup()

        holder = RefreshingLookup("unused.db", every=100, min_rows=5, build=build)
        for _ in range(100):
            holder.get()
        self.assertEqual(holder.builds, 1)  # call 1 builds, calls 2-100 reuse it
        holder.get()  # call 101: 100 calls served since the build
        self.assertEqual(holder.builds, 2)
        self.assertEqual(calls[0], ("unused.db", 5))
        holder.rebuild()
        self.assertEqual(holder.builds, 3)

    def test_failed_build_keeps_previous_or_falls_back(self):
        state = {"fail": True}
        good = public_lookup()

        def build(path, min_rows):
            if state["fail"]:
                raise RuntimeError("boom")
            return good

        holder = RefreshingLookup("unused.db", every=1, build=build)
        first = holder.get()  # no previous lookup: public fallback
        self.assertEqual(first.live_rows, 0)
        state["fail"] = False
        self.assertIs(holder.get(), good)
        state["fail"] = True
        self.assertIs(holder.get(), good)  # the failed rebuild keeps the previous one


class ServerFlagTests(unittest.TestCase):
    def test_flag_needs_log_db(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--lookup-from-log"])

    def test_router_request_with_lookup_from_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live.db"
            for p in BEE_PROMPTS:
                log_live(path, p)
            srv = make_server(port=0, allow_mock=True, log_db=path, lookup_from_log=True,
                              rate_limit_per_min=10_000)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                holder = srv.RequestHandlerClass.lookup_holder  # type: ignore[attr-defined]
                self.assertEqual(holder.builds, 1)  # built on start
                self.assertEqual(holder.lookup.live_rows, 60)
                body = json.dumps({"model": "router",
                                   "messages": [{"role": "user", "content": HELD_OUT}]}).encode()
                req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/v1/chat/completions",
                                             data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(r.status, 200)
                    j = json.loads(r.read())
                self.assertEqual(j["x_router"]["policy"], "router")
                self.assertEqual(holder.builds, 1)  # one request is far from the 100-call rebuild
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()

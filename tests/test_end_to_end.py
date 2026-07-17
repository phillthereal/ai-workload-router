"""End-to-end test: the full benchmark pipeline populates the db and produces
a sane cost delta between the router and frontier_only strategies."""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

# Force the offline mock path BEFORE importing any router module. Real API
# keys may be present in .env, but this test suite must never make a real
# network call — `python3 -m unittest discover -s tests` imports this file
# as a bare top-level module (tests/__init__.py does NOT run first in that
# invocation, since start_dir == top_level_dir), so the flag has to be set
# here rather than relied on from the package __init__. See
# router.secrets.force_mock / router.adapters.get_adapter.
os.environ.setdefault("AWR_FORCE_MOCK", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from router import db  # noqa: E402
from router.adapters import get_adapter  # noqa: E402
from router.benchmark.tasks import load_tasks  # noqa: E402
from router.config import FRONTIER_MODEL, get_model  # noqa: E402
from router.report import build_report  # noqa: E402
from router.router import route_task  # noqa: E402
from router.scoring import score  # noqa: E402


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        self.db_path = Path(fd.name)
        db.init_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_benchmark_populates_db_and_produces_sane_cost_delta(self):
        tasks = load_tasks()
        self.assertGreater(len(tasks), 0)

        run_group = f"test-{uuid.uuid4().hex[:8]}"
        for strategy in ("router", "frontier_only"):
            for task in tasks:
                model_name = (
                    route_task(task).chosen_model if strategy == "router" else FRONTIER_MODEL
                )
                adapter = get_adapter(model_name)
                response = adapter.complete(task["prompt"])
                cost = get_model(model_name).cost_for_tokens(
                    response.input_tokens, response.output_tokens
                )
                quality = score(task, response)

                db.log_run(
                    run_group=run_group,
                    strategy=strategy,
                    task_id=task["id"],
                    task_type=task["task_type"],
                    difficulty=task["difficulty"],
                    model=model_name,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cost_usd=cost,
                    latency_ms=response.latency_ms,
                    quality_score=quality,
                    success=True,
                    db_path=self.db_path,
                )

        rows = db.fetch_runs(run_group, db_path=self.db_path)
        self.assertEqual(len(rows), 2 * len(tasks))

        report = build_report(run_group, db_path=self.db_path)

        # The router must actually be cheaper than the frontier-only
        # baseline, but not implausibly so.
        self.assertGreater(report.cost_reduction_pct, 0)
        self.assertLess(report.cost_reduction_pct, 90)

        # Quality shouldn't collapse just because some tasks got routed to
        # cheaper models.
        self.assertGreater(report.quality_retention_pct, 50)


if __name__ == "__main__":
    unittest.main()

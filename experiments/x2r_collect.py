"""X2R collector: gather the data for the X2 replication (docs/X2R_REPLICATION.md).

For every task in data/tasks_x2r.json it collects
  * one real answer from each tier of roster `claude_tiers` (haiku, sonnet, opus), scored and logged with db.log_run
    (strategy `x2r_tier`);
  * the Haiku LLM classifier's labels, the tier route_task picks from them, and the classifier cost
    (experiments/x2r_classifier_cache.json);
  * one Jev tier decision with the repo's TIER_RULEBOOK, exactly as experiments/x2_jev_tier_picker.py does
    (experiments/x2r_jev_cache.json).

Usage
  python3 experiments/x2r_collect.py                 # offline: forces mock mode, writes *_simulated files only
  python3 experiments/x2r_collect.py --live          # REAL, PAID calls (needs the owner's go-ahead; see the doc)

Everything is resume-safe: anything already collected is skipped, so a re-run after an interruption costs nothing extra.
Mock data can never mix with real data: without --live the db and both caches carry a `_simulated` name.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from router import adapters, db  # noqa: E402
from router.adapters.typesafe import JevClient  # noqa: E402
from router.api import Result, Router  # noqa: E402
from router.benchmark.tasks import load_tasks  # noqa: E402
from router.classifier import classify_task  # noqa: E402
from router.config import FRONTIER_MODEL, MODEL_TIER, get_model, get_roster  # noqa: E402
from router.jev import TIER_RULEBOOK  # noqa: E402
from router.router import route_task  # noqa: E402
from router.scoring import score  # noqa: E402
from router.secrets import force_mock, get_api_key  # noqa: E402

ROSTER = "claude_tiers"
STRATEGY = "x2r_tier"
TIERS = ["budget", "mid", "frontier"]
DEFAULT_TASKS = ROOT / "data" / "tasks_x2r.json"
DEFAULT_DB = ROOT / "data" / "x2r_runs.db"
DEFAULT_SIM_DB = ROOT / "data" / "x2r_runs_simulated.db"
CACHE_DIR = ROOT / "experiments"
DEFAULT_CAP = 2.00

# Mean real cost per answer from past logged runs (USD), plus the classifier and Jev per-task figures.
MEAN_ANSWER_COST = {"budget": 0.00037, "mid": 0.00308, "frontier": 0.00401}
CLASSIFIER_COST = 0.0006
JEV_COST = 0.00002
# Rough rubric-judge call: ~450 input tokens (task, rubric, answer, instructions) and ~5 output tokens.
JUDGE_INPUT_TOKENS, JUDGE_OUTPUT_TOKENS = 450, 5


class CapHit(RuntimeError):
    pass


class Abort(RuntimeError):
    pass


class Spend:
    """Running total of money spent (answers, judge, classifier, Jev, probe). Raises CapHit once it passes the cap."""

    def __init__(self, cap: float, already: float = 0.0) -> None:
        self.cap, self.total = cap, already

    def add(self, usd: float) -> None:
        self.total += usd
        if self.total > self.cap:
            raise CapHit(f"spend ${self.total:.4f} passed the cap ${self.cap:.2f}")


class _Recorder:
    """Wraps an adapter: counts the cost of every call and, in live mode, aborts on a simulated response."""

    def __init__(self, inner, spend: Spend, live: bool) -> None:
        self.inner, self.spend, self.live = inner, spend, live

    def complete(self, *a, **k):
        r = self.inner.complete(*a, **k)
        if self.live and (r.simulated or not r.success):
            raise Abort("a judge/classifier call came back simulated or failed (missing key or provider error); aborting")
        self.spend.add(get_model(r.model).cost_for_tokens(r.input_tokens, r.output_tokens))
        return r


class _Watch:
    """Context manager: every adapter the repo's judge and classifier ask for goes through a _Recorder."""

    def __init__(self, spend: Spend, live: bool) -> None:
        self.spend, self.live = spend, live

    def __enter__(self):
        self._orig = adapters.get_adapter
        adapters.get_adapter = lambda *a, **k: _Recorder(self._orig(*a, **k), self.spend, self.live)
        return self

    def __exit__(self, *exc):
        adapters.get_adapter = self._orig
        return False


class _FixedModel:
    """Policy that answers with exactly one model (no routing, no failover to another tier)."""
    name = STRATEGY

    def __init__(self, model: str) -> None:
        self.model = model

    def report_tags(self) -> list[str]:
        return []

    def before_run_group(self, run_group: str) -> None:
        pass

    def run(self, prompt, task, ctx) -> Result:
        r = ctx.call(self.model, prompt)
        return Result(text=r.text, model=r.model, answer_cost_usd=ctx.cost(r), routing_cost_usd=0.0,
                      simulated=r.simulated, response=r, effort=r.effort)


def answer(task: dict, tier: str) -> Result:
    """One answer from `tier`'s model through the repo's own call path (Router/Ctx.call). Never falls back to a mock
    answer or to another tier: a failure raises (max_tier=tier stops the failover ladder; allow_mock_fallback=False)."""
    model = get_roster(ROSTER).model_for_tier(tier)
    router = Router(_FixedModel(model), roster_name=ROSTER, allow_mock_fallback=False, max_tier=tier)
    return router.complete(task["prompt"], task=task)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=1, sort_keys=True))


def paths(live: bool, db_arg: str | None, cache_dir: Path) -> dict:
    sfx = "" if live else "_simulated"
    dbp = Path(db_arg) if db_arg else (DEFAULT_DB if live else DEFAULT_SIM_DB)
    if live and "simulated" in dbp.name:
        raise SystemExit("refusing: --live must not write to a *_simulated* database")
    if not live and "simulated" not in dbp.name:
        raise SystemExit("refusing: without --live the database name must contain 'simulated' (mock data must never "
                         "mix with real data); drop --db or use data/x2r_runs_simulated.db")
    return dict(db=dbp, classifier=cache_dir / f"x2r_classifier_cache{sfx}.json", jev=cache_dir / f"x2r_jev_cache{sfx}.json",
                dropped=cache_dir / f"x2r_dropped{sfx}.json")


def done_pairs(dbp: Path, live: bool) -> set:
    if not dbp.exists():
        return set()
    c = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    try:
        rows = c.execute("SELECT task_id, model FROM runs WHERE strategy=? AND roster=? AND simulated=?",
                         (STRATEGY, ROSTER, 0 if live else 1)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        c.close()
    return {(t, MODEL_TIER[m]) for t, m in rows if m in MODEL_TIER}


def prior_spend(dbp: Path, live: bool, classifier: dict, jev: dict) -> float:
    total = sum(v.get("cost", 0.0) for v in classifier.values()) + sum(v.get("cost", 0.0) for v in jev.values())
    if dbp.exists():
        c = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        try:
            total += c.execute("SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE strategy=? AND roster=? AND simulated=?",
                               (STRATEGY, ROSTER, 0 if live else 1)).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        finally:
            c.close()
    return total


def estimate(tasks: list, done: set, classifier: dict, jev: dict) -> dict:
    """Cost of the work still to do, by part (USD)."""
    judge_model = get_model(FRONTIER_MODEL)
    judge_call = judge_model.cost_for_tokens(JUDGE_INPUT_TOKENS, JUDGE_OUTPUT_TOKENS)
    answers = {t: sum(MEAN_ANSWER_COST[t] for k in tasks if (k["id"], t) not in done) for t in TIERS}
    judge = sum(judge_call for k in tasks for t in TIERS if k["scoring"] == "rubric_judge" and (k["id"], t) not in done)
    cls = CLASSIFIER_COST * sum(1 for k in tasks if k["id"] not in classifier)
    jv = JEV_COST * sum(1 for k in tasks if k["id"] not in jev)
    parts = dict(answers_budget=answers["budget"], answers_mid=answers["mid"], answers_frontier=answers["frontier"],
                 judge=judge, classifier=cls, jev=jv)
    parts["total"] = sum(parts.values())
    return parts


def probe(spend: Spend) -> None:
    """ONE budget-model call before anything else; abort if it comes back simulated or fails."""
    try:
        r = answer({"id": "x2r-probe", "prompt": "Reply with the single word: ready"}, "budget")
    except Exception as exc:  # noqa: BLE001
        raise Abort(f"probe call failed ({type(exc).__name__}); not starting") from None
    if r.simulated or (r.response and r.response.simulated):
        raise Abort("probe response is marked simulated (missing key or fallback to the mock); not starting")
    spend.add(r.answer_cost_usd)
    print(f"probe ok: real response from {r.model}, cost ${r.answer_cost_usd:.6f}")


def _retry(fn):
    """Run fn; on any exception try once more. Returns (value, None) or (None, error type name)."""
    err = None
    for _ in range(2):
        try:
            return fn(), None
        except (CapHit, Abort):
            raise
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
    return None, err


def collect_task(task: dict, live: bool, p: dict, spend: Spend, done: set, cls_cache: dict, jev_cache: dict,
                 dropped: dict, run_group: str) -> None:
    tid = task["id"]
    if tid not in jev_cache:
        def _jev():
            d = JevClient().decide(task["prompt"], TIER_RULEBOOK.render_instructions(), TIER_RULEBOOK.criteria)
            if live and d.simulated:
                raise Abort("Jev decision is marked simulated; aborting")
            if not d.success:
                raise RuntimeError(d.error or "jev_failed")
            return d
        d, err = _retry(_jev)
        if d is None:
            dropped.setdefault(tid, {})["jev"] = err
        else:
            spend.add(d.cost_usd)
            jev_cache[tid] = dict(choice=d.choice, probs=d.probabilities, tokens=d.input_tokens, cost=d.cost_usd,
                                  simulated=d.simulated)
            _save_json(p["jev"], jev_cache)
    if tid not in cls_cache:
        def _cls():
            with _Watch(spend, live):  # the classifier call (and its cost) goes through the recorder
                c = classify_task(task, roster_name=ROSTER, use_labels=False)
            tier = MODEL_TIER[route_task(task, roster_name=ROSTER, task_type=c.task_type, difficulty=c.difficulty).chosen_model]
            return c, tier
        out, err = _retry(_cls)
        if out is None:
            dropped.setdefault(tid, {})["classifier"] = err
        else:
            c, tier = out
            cls_cache[tid] = dict(task_type=c.task_type, difficulty=c.difficulty, tier=tier, cost=c.cost_usd,
                                  simulated=c.simulated)
            _save_json(p["classifier"], cls_cache)
    for tier in TIERS:
        if (tid, tier) in done:
            continue
        res, err = _retry(lambda: answer(task, tier))
        if res is None:
            dropped.setdefault(tid, {})[tier] = err
            continue
        if live and (res.simulated or (res.response and res.response.simulated)):
            raise Abort(f"{tier} answer for {tid} is marked simulated; aborting")
        spend.add(res.answer_cost_usd)
        with _Watch(spend, live):  # a rubric judge call goes through the recorder too
            q = score(task, res.response)
        db.log_run(
            run_group=run_group, strategy=STRATEGY, task_id=tid, task_type=task["task_type"], difficulty=task["difficulty"],
            model=res.model, input_tokens=res.response.input_tokens, output_tokens=res.response.output_tokens,
            cost_usd=res.answer_cost_usd, latency_ms=res.latency_ms or res.response.latency_ms, quality_score=q,
            success=res.response.success, simulated=res.response.simulated, response_text=res.response.text,
            effort=res.response.effort, roster=ROSTER, db_path=p["db"],
        )
        done.add((tid, tier))
        dropped.get(tid, {}).pop(tier, None)
    if not dropped.get(tid):
        dropped.pop(tid, None)
    _save_json(p["dropped"], dropped)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", default=str(DEFAULT_TASKS))
    ap.add_argument("--db", default=None, help="default data/x2r_runs.db (live) or data/x2r_runs_simulated.db (mock)")
    ap.add_argument("--cap", type=float, default=DEFAULT_CAP, help="hard total USD cap across everything (default 2.00)")
    ap.add_argument("--live", action="store_true", help="make REAL, paid calls (required for real data)")
    ap.add_argument("--cache-dir", default=str(CACHE_DIR), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if a.live:
        if force_mock():
            raise SystemExit("refusing: AWR_FORCE_MOCK is set; unset it for a --live run")
    else:
        os.environ["AWR_FORCE_MOCK"] = "1"
    p = paths(a.live, a.db, Path(a.cache_dir))
    tasks = load_tasks(a.tasks)
    db.init_db(p["db"])
    cls_cache, jev_cache, dropped = _load_json(p["classifier"]), _load_json(p["jev"]), _load_json(p["dropped"])
    done = done_pairs(p["db"], a.live)
    est = estimate(tasks, done, cls_cache, jev_cache)
    mode = "LIVE (real, paid calls)" if a.live else "MOCK (offline, simulated data only)"
    print(f"mode: {mode}\ntasks: {len(tasks)}; db: {p['db']}")
    print(f"rubric judge model: {FRONTIER_MODEL} (judges each rubric_judge answer; one call per answer)")
    print("cost estimate for the work still to do (USD):  " + ", ".join(f"{k} {v:.4f}" for k, v in est.items()))
    spend = Spend(a.cap, prior_spend(p["db"], a.live, cls_cache, jev_cache))
    if a.live:
        if spend.total + est["total"] > a.cap:
            raise SystemExit(f"refusing: estimate ${est['total']:.4f} + already spent ${spend.total:.4f} exceeds the cap ${a.cap:.2f}")
        for name in ("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"):
            if not get_api_key(name):
                raise SystemExit(f"refusing: {name} is not set (names only are ever printed)")
        try:
            probe(spend)
        except Abort as exc:
            raise SystemExit(f"ABORT: {exc}") from None
    run_group = f"x2r-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    try:
        for n, task in enumerate(tasks, 1):
            collect_task(task, a.live, p, spend, done, cls_cache, jev_cache, dropped, run_group)
            if n % 10 == 0:
                print(f"  {n}/{len(tasks)} tasks, spend so far ${spend.total:.4f} (cap ${a.cap:.2f})")
    except (CapHit, Abort) as exc:
        _save_json(p["dropped"], dropped)
        print(f"STOPPED: {exc}. Spend ${spend.total:.4f}. Everything collected so far is saved; re-run to resume.")
        return 2
    miss = {t: v for t, v in dropped.items() if v}
    print(f"done. tasks: {len(tasks)}, answers logged: {len(done)}/{len(tasks) * 3}, total spend ${spend.total:.4f}")
    if miss:
        print(f"WARNING: {len(miss)} task(s) have missing pieces after one retry (listed in {p['dropped']}): {sorted(miss)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

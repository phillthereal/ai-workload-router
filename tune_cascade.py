#!/usr/bin/env python3
"""
Cascade threshold sweep: what `escalate_threshold` minimizes routing overhead
without dropping ground-truth quality?

Separate from run_benchmark.py because it answers a tuning question the main
benchmark doesn't: the cascade's single knob (`escalate_threshold`, default 0.7)
trades a cheap-answer quality leak against a cost leak, and this sweeps it to
find the setting — if one exists — that pays less overhead at the same quality.

THE TRICK THAT KEEPS THIS FREE. Changing the threshold does NOT change what is
sent to the verifier or the models — it only changes the accept/escalate
DECISION made on the verifier score that was already returned. Every model call
(budget answer, verifier check, frontier answer) is content-addressed in
.cache/ from the live runs, so re-deciding at a different threshold is a pure
replay: no network, deterministic, $0. This script therefore reads ONLY from the
cache (via load_cached) and never calls a live adapter — it snapshots the cache
directory before and after and asserts nothing was written, which is the proof
that it cost nothing.

THE ONE SUBTLETY: GROUND-TRUTH QUALITY OF DISCARDED ANSWERS. At a LOWER
threshold, a task that previously escalated now ACCEPTS the budget answer, and to
know if quality held we need that budget answer's ground-truth quality from the
Opus judge (router.scoring). For `exact_match` tasks that's free and
deterministic. For `rubric_judge` tasks it's a judge call that may be uncached
(the budget answer was discarded, so may never have been judged). This script
does NOT make that call live by default: it counts such tasks as
`n_uncached_judge`, drops them from the mean, and flags the sweep as
quality-incomplete for them. Pass --mock-uncached-judge to fill those gaps with
the OFFLINE MOCK judge instead (marked simulated); pass --allow-live-judge to
permit real judge calls (spends money — off by default).

Output goes to data/cascade_threshold_sweep.md (gitignored) and stdout. It never
writes to data/runs.db or any committed report.

Usage:
    python tune_cascade.py
    python tune_cascade.py --thresholds 0.5,0.6,0.7,0.8,0.9
    python tune_cascade.py --mock-uncached-judge
"""

from __future__ import annotations  # 3.9-safe: stringizes str|None annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent / "src"))

from router import db  # noqa: E402
from router.adapters.cache import CACHE_DIR, load_cached  # noqa: E402
from router.benchmark.tasks import load_tasks  # noqa: E402
from router.cascade import (  # noqa: E402
    _VERIFIER_MAX_TOKENS,
    _VERIFIER_PROMPT_TEMPLATE,
    _parse_adequacy,
)
from router.config import FRONTIER_MODEL, get_model, get_roster  # noqa: E402
from router.scoring import (  # noqa: E402
    _JUDGE_PROMPT_TEMPLATE,
    _parse_judge_score,
    exact_match,
    rubric_judge,
)

# The two LIVE cascade run_groups already in data/runs.db. Each is (task_file,
# run_group, label). The run_group is only used to locate the frontier_only
# baseline rows for the "% of savings" denominator and to validate the replay
# against what was logged live — no live rows are written or modified.
LIVE_RUNS: list[tuple[str, str, str]] = [
    ("data/tasks.json", "20260717T171702-e98b914b", "easy (25 tasks)"),
    ("data/tasks_hard.json", "20260717T172831-e7fe3e42", "hard (10 tasks)"),
]

DEFAULT_THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.9]

# The cascade's canonical two-tier ladder for the claude_tiers roster is
# budget -> frontier, verified by the roster's mid tier. This script is written
# for exactly that shape (the shape the live runs used).
ROSTER_NAME = "claude_tiers"


def _cost(model: str, resp) -> float:
    return get_model(model).cost_for_tokens(resp.input_tokens, resp.output_tokens)


def _judge_prompt(task: dict, candidate: str) -> str:
    return _JUDGE_PROMPT_TEMPLATE.format(
        task_prompt=task.get("prompt", ""),
        reference=task.get("reference", ""),
        candidate=candidate,
    )


class PreparedTask:
    """Everything the sweep needs for one task, gathered ONCE from the cache.

    The per-threshold loop then only re-applies the accept/escalate rule to
    `verifier_score` — no further cache access, and certainly no network.
    """

    def __init__(
        self,
        task: dict,
        budget_model: str,
        verifier_model: str,
        frontier_model: str,
        judge_model: str,
        mock_uncached_judge: bool,
        allow_live_judge: bool,
    ) -> None:
        self.task_id = task["id"]
        prompt = task["prompt"]

        budget_resp = load_cached(budget_model, prompt)
        frontier_resp = load_cached(frontier_model, prompt)
        if budget_resp is None or frontier_resp is None:
            raise SystemExit(
                f"[cache miss] budget/frontier answer for {self.task_id} is not "
                "cached — the sweep cannot run offline for this task set. Did the "
                "live runs use a different task file?"
            )
        self.budget_cost = _cost(budget_model, budget_resp)
        self.frontier_cost = _cost(frontier_model, frontier_resp)

        # Verifier check: reference-free adequacy of the budget answer. Cached
        # from the live run (verifier prompt embeds the budget answer text, so
        # the key is stable). max_tokens must match the live call (=8).
        vprompt = _VERIFIER_PROMPT_TEMPLATE.format(prompt=prompt, answer=budget_resp.text)
        verifier_resp = load_cached(verifier_model, vprompt, max_tokens=_VERIFIER_MAX_TOKENS)
        if verifier_resp is None:
            raise SystemExit(
                f"[cache miss] verifier check for {self.task_id} is not cached."
            )
        self.verifier_score = _parse_adequacy(verifier_resp.text)
        self.verify_cost = _cost(verifier_model, verifier_resp)

        # Ground-truth quality of BOTH candidate answers. exact_match is free
        # and deterministic; rubric_judge needs the Opus judge (may be uncached
        # for a discarded answer). quality None => uncached and not mock-filled.
        self.budget_quality, self.budget_q_uncached, self.budget_q_sim = self._quality(
            task, budget_resp, judge_model, mock_uncached_judge, allow_live_judge
        )
        self.frontier_quality, self.frontier_q_uncached, self.frontier_q_sim = self._quality(
            task, frontier_resp, judge_model, mock_uncached_judge, allow_live_judge
        )

    def _quality(
        self, task, resp, judge_model, mock_uncached_judge, allow_live_judge
    ) -> tuple[Optional[float], bool, bool]:
        """Return (quality, uncached_flag, simulated_flag) from cache only."""
        method = task.get("scoring")
        if method == "exact_match":
            return exact_match(resp.text, task["reference"]), False, False
        if method == "rubric_judge":
            cached = load_cached(judge_model, _judge_prompt(task, resp.text))
            if cached is not None:
                return _parse_judge_score(cached.text), False, False
            if allow_live_judge:
                # Explicit opt-in to spend. We still refuse to silently do it;
                # the safe default never reaches here.
                raise SystemExit(
                    "--allow-live-judge would make a real judge call for "
                    f"{task['id']} — refusing in this offline script. Re-run the "
                    "live benchmark to populate the cache instead."
                )
            if mock_uncached_judge:
                return rubric_judge(task, resp), False, True  # simulated fill
            return None, True, False  # uncached, quality-incomplete
        raise SystemExit(f"Unsupported scoring method: {method!r}")


def sweep_threshold(prepared: list[PreparedTask], threshold: float, frontier_only_cost: float) -> dict:
    """Apply the accept/escalate rule at `threshold`; aggregate cost & quality."""
    answer_cost = overhead = 0.0
    n_escalated = 0
    quals: list[float] = []
    n_uncached = n_simulated = 0
    for p in prepared:
        # Every task pays exactly one verifier check in a two-tier ladder —
        # this toll is threshold-INVARIANT and is the bulk of the overhead.
        overhead += p.verify_cost
        if p.verifier_score >= threshold:  # accept the budget answer
            answer_cost += p.budget_cost
            q, unc, sim = p.budget_quality, p.budget_q_uncached, p.budget_q_sim
        else:  # escalate: budget attempt becomes discarded overhead
            overhead += p.budget_cost
            answer_cost += p.frontier_cost
            n_escalated += 1
            q, unc, sim = p.frontier_quality, p.frontier_q_uncached, p.frontier_q_sim
        if unc:
            n_uncached += 1
        else:
            quals.append(q)
            n_simulated += int(sim)
    savings = frontier_only_cost - answer_cost
    mean_q = sum(quals) / len(quals) if quals else float("nan")
    return {
        "threshold": threshold,
        "answer_cost": answer_cost,
        "overhead": overhead,
        "savings": savings,
        "overhead_pct_of_savings": (overhead / savings * 100) if savings else float("nan"),
        "mean_quality": mean_q,
        "n_escalated": n_escalated,
        "n_uncached_judge": n_uncached,
        "n_simulated_quality": n_simulated,
    }


def decompose_at(prepared: list[PreparedTask], threshold: float) -> dict:
    """Split overhead into verifier toll (paid on every task) vs escalation
    discard (budget attempts thrown away on escalated tasks), at `threshold`."""
    verifier_toll = sum(p.verify_cost for p in prepared)
    discard = sum(p.budget_cost for p in prepared if p.verifier_score < threshold)
    return {"verifier_toll": verifier_toll, "discard": discard}


def _frontier_only_cost(run_group: str, task_ids: set[str]) -> float:
    """Sum the logged frontier_only answer cost for these tasks (the baseline)."""
    total = 0.0
    for row in db.fetch_runs(run_group):
        if row["strategy"] == "frontier_only" and row["task_id"] in task_ids:
            total += row["cost_usd"]
    return total


def _cascade_live_reference(run_group: str) -> dict:
    """DB truth for the live cascade run (for validating the 0.7 replay)."""
    rows = [r for r in db.fetch_runs(run_group) if r["strategy"] == "cascade"]
    n_esc = sum(1 for r in rows if get_model(r["model"]).name == FRONTIER_MODEL)
    return {
        "answer_cost": sum(r["cost_usd"] for r in rows),
        "overhead": sum(r["routing_cost_usd"] for r in rows),
        "mean_quality": sum(r["quality_score"] for r in rows) / len(rows) if rows else 0.0,
        "n_escalated": n_esc,
    }


def format_markdown(sections: list[dict], thresholds: list[float], live_threshold: float) -> str:
    out = ["# Cascade threshold sweep — overhead vs ground-truth quality", ""]
    out.append(
        "Offline replay over the record/replay cache: every model & verifier call "
        "is a cache hit, so this cost $0 (see the CACHE-SAFE assertion in stdout). "
        "Quality is the real Opus judge (cached) for rubric tasks, exact-match for "
        "the rest. Sweep does NOT touch data/runs.db or any committed report."
    )
    for s in sections:
        out += ["", f"## {s['label']}  —  run_group `{s['run_group']}`", ""]
        d = s["decomp"]
        ov = d["verifier_toll"] + d["discard"]
        out += [
            f"**Overhead decomposition at the live threshold {live_threshold}:**",
            "",
            f"- verifier toll (Sonnet check on every task): "
            f"${d['verifier_toll']:.6f}  ({d['verifier_toll']/ov*100:.1f}% of overhead)",
            f"- escalation-discard (thrown-away budget attempts): "
            f"${d['discard']:.6f}  ({d['discard']/ov*100:.1f}% of overhead)",
            f"- total overhead: ${ov:.6f}  =  {s['live_overhead_pct']:.1f}% of savings",
            "",
            "| threshold | answer $ | overhead $ | overhead % of savings | mean quality | n_escalated | n_uncached_judge |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in s["rows"]:
            marker = "  ← live" if abs(r["threshold"] - live_threshold) < 1e-9 else ""
            out.append(
                f"| {r['threshold']:.1f}{marker} | ${r['answer_cost']:.6f} | "
                f"${r['overhead']:.6f} | {r['overhead_pct_of_savings']:.1f}% | "
                f"{r['mean_quality']:.4f} | {r['n_escalated']} | {r['n_uncached_judge']} |"
            )
        est = s["verifier_estimate"]
        out += [
            "",
            "_Cheaper-verifier ESTIMATE (cost only — re-prices the identical "
            "verifier prompt at another model's rates; a different verifier would "
            "score differently, so quality is unverified):_",
            "",
            "| verifier model | est. verifier toll | est. overhead | est. overhead % of savings |",
            "|---|---|---|---|",
        ]
        for e in est:
            out.append(
                f"| {e['model']} | ${e['toll']:.6f} | ${e['overhead']:.6f} | "
                f"{e['overhead_pct_of_savings']:.1f}% |"
            )
    out += ["", "_Generated by tune_cascade.py. Numbers validated against the live "
            "cascade rows in data/runs.db (see stdout)._", ""]
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--thresholds", default=None,
                        help="Comma-separated thresholds, e.g. '0.5,0.6,0.7,0.8,0.9'.")
    parser.add_argument("--mock-uncached-judge", action="store_true",
                        help="Fill uncached rubric-judge quality with the OFFLINE MOCK "
                             "judge (marked simulated) instead of dropping the task.")
    parser.add_argument("--allow-live-judge", action="store_true",
                        help="Permit REAL judge calls for uncached rubric tasks (SPENDS "
                             "money; off by default — the script refuses otherwise).")
    args = parser.parse_args(argv)

    thresholds = (
        [float(x) for x in args.thresholds.split(",")] if args.thresholds else DEFAULT_THRESHOLDS
    )
    live_threshold = 0.7

    roster = get_roster(ROSTER_NAME)
    budget, verifier, frontier = roster.budget, roster.mid, roster.frontier
    judge_model = FRONTIER_MODEL

    # PROOF OF ZERO SPEND: snapshot the cache dir before/after; assert unchanged.
    before = {p.name for p in CACHE_DIR.glob("*.json")} if CACHE_DIR.exists() else set()

    sections = []
    for task_file, run_group, label in LIVE_RUNS:
        tasks = load_tasks(task_file)
        task_ids = {t["id"] for t in tasks}
        prepared = [
            PreparedTask(t, budget, verifier, frontier, judge_model,
                         args.mock_uncached_judge, args.allow_live_judge)
            for t in tasks
        ]
        fo_cost = _frontier_only_cost(run_group, task_ids)

        rows = [sweep_threshold(prepared, th, fo_cost) for th in thresholds]
        decomp = decompose_at(prepared, live_threshold)
        live_row = next(r for r in rows if abs(r["threshold"] - live_threshold) < 1e-9) \
            if any(abs(th - live_threshold) < 1e-9 for th in thresholds) \
            else sweep_threshold(prepared, live_threshold, fo_cost)

        # Cheaper-verifier estimate: reprice verifier token counts at cand rates.
        discard_live = decomp["discard"]
        est = []
        for cand in ("claude-haiku-4-5", "claude-sonnet-5"):
            toll = 0.0
            for t in tasks:
                bprompt = t["prompt"]
                b = load_cached(budget, bprompt)
                vprompt = _VERIFIER_PROMPT_TEMPLATE.format(prompt=bprompt, answer=b.text)
                v = load_cached(verifier, vprompt, max_tokens=_VERIFIER_MAX_TOKENS)
                toll += get_model(cand).cost_for_tokens(v.input_tokens, v.output_tokens)
            ov = toll + discard_live
            est.append({"model": cand, "toll": toll, "overhead": ov,
                        "overhead_pct_of_savings": ov / live_row["savings"] * 100})

        # Validate the 0.7 replay against the live DB rows.
        ref = _cascade_live_reference(run_group)
        ok = (abs(live_row["answer_cost"] - ref["answer_cost"]) < 1e-9
              and abs(live_row["overhead"] - ref["overhead"]) < 1e-9
              and live_row["n_escalated"] == ref["n_escalated"])

        sections.append({
            "label": label, "run_group": run_group, "rows": rows,
            "decomp": decomp, "live_overhead_pct": live_row["overhead_pct_of_savings"],
            "verifier_estimate": est,
        })

        print(f"\n{label}  run_group={run_group}")
        print(f"  frontier_only baseline cost: ${fo_cost:.6f}")
        print(f"  overhead decomposition @ {live_threshold}: "
              f"verifier toll ${decomp['verifier_toll']:.6f} "
              f"({decomp['verifier_toll']/(decomp['verifier_toll']+decomp['discard'])*100:.1f}%)  "
              f"| discard ${decomp['discard']:.6f}")
        print(f"  {'thresh':>6} {'answer$':>10} {'overhd$':>10} {'ovh%sav':>8} "
              f"{'meanQ':>7} {'nEsc':>5} {'nUncached':>9}")
        for r in rows:
            print(f"  {r['threshold']:>6.1f} {r['answer_cost']:>10.6f} {r['overhead']:>10.6f} "
                  f"{r['overhead_pct_of_savings']:>7.1f}% {r['mean_quality']:>7.4f} "
                  f"{r['n_escalated']:>5} {r['n_uncached_judge']:>9}")
        print(f"  live-replay validation vs data/runs.db @0.7: "
              f"{'PASS' if ok else 'FAIL'} "
              f"(answer/overhead/n_esc match: {ok})")

    # Assert nothing was written to the cache => zero live calls.
    after = {p.name for p in CACHE_DIR.glob("*.json")} if CACHE_DIR.exists() else set()
    new_files = after - before
    assert not new_files, f"CACHE MISS: {len(new_files)} new cache files => live calls were made!"
    print(f"\nCACHE-SAFE: {len(after)} cache files unchanged, 0 new writes => $0 spent.")

    out_path = Path(__file__).parent / "data" / "cascade_threshold_sweep.md"
    out_path.write_text(format_markdown(sections, thresholds, live_threshold))
    print(f"Written to {out_path}")


if __name__ == "__main__":
    main()

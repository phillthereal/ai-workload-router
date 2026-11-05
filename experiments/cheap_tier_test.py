#!/usr/bin/env python3
"""
Can cheaper models take over reasoning and extraction without losing quality?

Asks every model each task, saves the answers, then works out each routing idea
from them, so one paid pass covers every idea. Reruns replay from .cache for free.

    python3 experiments/cheap_tier_test.py --max-spend 0.70
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from analyze import bootstrap, net_reduction, retention  # noqa: E402
from router.adapters import SpendMeter, get_adapter  # noqa: E402
from router.config import EFFORT_OFF, get_model  # noqa: E402
from router.scoring import committed_answer, final_answer  # noqa: E402

BENCH = ROOT / "data" / "public" / "benchmark"
OUT = ROOT / "data" / "cheap_tier_test.json"
SETS = {"new tasks": "tasks_public_fresh3.json", "hard set": "tasks_public_hard.json"}
TYPES = ("reasoning", "extraction")
OPUS, SONNET, HAIKU = "claude-opus-4-8", "claude-sonnet-5-5", "claude-haiku-5-5"
MINI, DEEPSEEK = "gpt-4o-mini", "deepseek-chat"
EFFORT = {SONNET: EFFORT_OFF, HAIKU: EFFORT_OFF}  # thinking off: it is billed as output
MODELS = (OPUS, SONNET, HAIKU, MINI, DEEPSEEK)
CLASSIFIER_USD = 0.000105  # mean per task in run group 20261105T175936-c27f3a54
WORKERS = 4


def ask(task, model):
    r = get_adapter(model, False).complete(task["prompt"], EFFORT.get(model))
    if r.simulated:
        raise RuntimeError(f"{model} was simulated on {task['id']}")
    return {"text": r.text, "answered": r.success, "in": r.input_tokens, "out": r.output_tokens, "cut_off": r.truncated,
            "usd": get_model(model).cost_for_tokens(r.input_tokens, r.output_tokens),
            "score": final_answer(r.text, task["reference"])}


def collect(max_spend):
    rows = []
    with ThreadPoolExecutor(WORKERS) as pool:
        for name, file in SETS.items():
            tasks = json.loads((BENCH / file).read_text())["tasks"]
            for task in tasks:
                if task["task_type"] not in TYPES or task["scoring"] != "final_answer":
                    continue
                if SpendMeter.usd >= max_spend:
                    print(f"Stopped at the ${max_spend:.2f} cap", file=sys.stderr)
                    return rows
                answers = dict(zip(MODELS, pool.map(lambda m: ask(task, m), MODELS)))
                if not answers[OPUS]["answered"]:  # no baseline to compare with, so the task is left out
                    print(f"Skipped {task['id']}: Opus gave no answer", file=sys.stderr)
                    continue
                rows.append({"set": name, "id": task["id"], "type": task["task_type"], "answers": answers})
    return rows


def agree(a, b, fallback):
    """Keep b's answer when a and b commit to the same one, as RulesPolicy._agree_or_frontier does."""
    def arm(row):
        x, y, z = (row["answers"][m] for m in (a, b, fallback))
        same = final_answer(x["text"], committed_answer(y["text"])) == 1.0
        return (y["score"] if same else z["score"]), x["usd"] + y["usd"] + (0 if same else z["usd"]), same
    return arm


def alone(model):
    return lambda row: (row["answers"][model]["score"], row["answers"][model]["usd"], True)


ARMS = {
    "Sonnet 5.5 only": alone(SONNET),
    "Haiku 5.5 only": alone(HAIKU),
    "DeepSeek only": alone(DEEPSEEK),
    "mini + DeepSeek agree, else Opus": agree(MINI, DEEPSEEK, OPUS),
    "mini + DeepSeek agree, else Sonnet": agree(MINI, DEEPSEEK, SONNET),
    "Haiku + DeepSeek agree, else Opus": agree(HAIKU, DEEPSEEK, OPUS),
    "Haiku + DeepSeek agree, else Sonnet": agree(HAIKU, DEEPSEEK, SONNET),
}


def table(rows, label):
    print(f"\n{label}: {len(rows)} tasks, Opus mean {sum(r['answers'][OPUS]['score'] for r in rows) / len(rows):.3f}")
    print("| Idea | Saved % [range] | Quality kept % [range] | Cheap answer kept |\n|---|---|---|---|")
    for name, arm in ARMS.items():
        res = [arm(r) for r in rows]
        pairs = [(r["answers"][OPUS]["usd"], u + CLASSIFIER_USD, r["answers"][OPUS]["score"], s)
                 for r, (s, u, _) in zip(rows, res)]
        c, q = bootstrap(pairs, net_reduction), bootstrap(pairs, retention)
        print(f"| {name} | {c[0]:.1f} [{c[1]:.1f}, {c[2]:.1f}] | {q[0]:.1f} [{q[1]:.1f}, {q[2]:.1f}] "
              f"| {sum(k for *_, k in res)} of {len(res)} |")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--max-spend", type=float, required=True)
    rows = collect(p.parse_args().max_spend)
    OUT.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
    for name in SETS:
        part = [r for r in rows if r["set"] == name]
        if part:
            table(part, name)
        for kind in TYPES:
            sub = [r for r in part if r["type"] == kind]
            if sub and name == "new tasks":
                table(sub, f"{name}, {kind}")
    cut = {m: sum(r["answers"][m]["cut_off"] for r in rows) for m in MODELS}
    none = {m: sum(not r["answers"][m]["answered"] for r in rows) for m in MODELS}
    print(f"\nNo answer given (scored 0): {none}")
    print(f"Cut off at the length limit: {cut}\nReal spend ${SpendMeter.usd:.4f}")


if __name__ == "__main__":
    main()

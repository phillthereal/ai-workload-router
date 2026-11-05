#!/usr/bin/env python3
"""Build the public benchmark task sets. Free: downloads public data, calls no model.

    python build_public_tasks.py [out_dir]

Writes tasks_public.json (300 mixed tasks, July's task-type shares) and
tasks_public_hard.json (the 10 hard tasks plus 30 MMLU-Pro) to out_dir, default
data/public/benchmark. The same seed and pinned dataset versions give the same
files. See data/public/benchmark/README.md for sources and licences.
"""

import csv
import json
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from router.scoring import REFERENCE_SEP  # noqa: E402

SEED = 2026
PAGE = 100  # the most rows the rows API returns per request
ANSWER_LINE = "End your reply with one final line in exactly this form:\nAnswer: <your answer>"
HEADERS = {"User-Agent": "ai-workload-router-task-builder"}

# dataset -> (commit the rows must come from, config, split)
PINS = {
    "rajpurkar/squad_v2": ("3ffb306f725f7d2ce8394bc1873b24868140c412", "squad_v2", "validation"),
    "google/IFEval": ("966cd89545d6b6acfd7638bc708b98261ca58e84", "default", "train"),
    "openai/gsm8k": ("740312add88f781978c0658806c59bc2815b9866", "main", "test"),
    "TIGER-Lab/MMLU-Pro": ("b189ec765aa7ed75c8acfea42df31fdae71f97be", "default", "test"),
}

Task = dict[str, Any]
Row = dict[str, Any]


def _get(url: str) -> Any:
    request = urllib.request.Request(url, headers=HEADERS)
    time.sleep(1)  # the rows API rate-limits bursts
    for attempt in range(7):
        try:
            with urllib.request.urlopen(request, timeout=30) as reply:
                return json.load(reply)
        except OSError:
            if attempt == 6:
                raise
            time.sleep(2 ** attempt)


def _rows_url(dataset: str, offset: int, length: int) -> str:
    _, config, split = PINS[dataset]
    query = urllib.parse.urlencode(
        {"dataset": dataset, "config": config, "split": split, "offset": offset, "length": length})
    return f"https://datasets-server.huggingface.co/rows?{query}"


def check_pin(dataset: str) -> None:
    """Refuse to build from a dataset that changed since it was pinned."""
    sha = _get(f"https://huggingface.co/api/datasets/{dataset}")["sha"]
    if sha != PINS[dataset][0]:
        raise SystemExit(f"{dataset} changed: expected {PINS[dataset][0]}, found {sha}")


def sample(dataset: str, n: int, convert: Callable[[Row, int], Optional[Task]]) -> list[Task]:
    """First n rows, in seeded random order, that `convert` accepts."""
    check_pin(dataset)
    total = _get(_rows_url(dataset, 0, 1))["num_rows_total"]
    order = list(range(total))
    random.Random(f"{SEED}-{dataset}").shuffle(order)
    tasks: list[Task] = []
    pages: dict[int, list[Any]] = {}  # same rows as one request per row, far fewer requests
    for index in order:
        if len(tasks) == n:
            return tasks
        page = index // PAGE
        if page not in pages:
            pages[page] = _get(_rows_url(dataset, page * PAGE, PAGE))["rows"]
        task = convert(pages[page][index % PAGE]["row"], index)
        if task:
            tasks.append(task)
    raise SystemExit(f"{dataset} has too few usable rows")


def task(id_: str, task_type: str, difficulty: str, prompt: str, reference: str,
         scoring: str, source: str) -> Task:
    """One task in the shape router.benchmark.tasks expects, plus where it came from."""
    return {"id": id_, "task_type": task_type, "difficulty": difficulty, "prompt": prompt,
            "reference": reference, "scoring": scoring, "source": source}


def banking(text: str, category: str, categories: list[str], index: int) -> Task:
    """Banking77: pick one of the 77 intents."""
    prompt = (f"Classify this bank customer message into exactly one of these categories:\n"
              f"{', '.join(categories)}\n\nMessage: {text}\n\n{ANSWER_LINE}")
    return task(f"b77-{index}", "classification", "medium", prompt, category, "final_answer", "banking77")


def squad(row: Row, index: int) -> Optional[Task]:
    """SQuAD v2: copy the answer out of a passage. Unanswerable questions are skipped."""
    answers = sorted(set(row["answers"]["text"]))
    if not answers:
        return None
    prompt = (f"Using only this passage, answer the question with the shortest exact phrase "
              f"from the passage.\n\nPassage: {row['context']}\n\nQuestion: {row['question']}\n\n{ANSWER_LINE}")
    return task(f"squad-{index}", "extraction", "easy", prompt, REFERENCE_SEP.join(answers),
                "final_answer", "squad_v2")


def ifeval(row: Row, index: int) -> Task:
    """IFEval: write to explicit constraints, graded by the judge against those constraints."""
    rubric = "Score how fully the answer obeys every explicit instruction and constraint in the task."
    return task(f"ifeval-{index}", "short_generation", "medium", row["prompt"], rubric,
                "rubric_judge", "ifeval")


def gsm8k(row: Row, index: int) -> Task:
    """GSM8K: a word problem whose number follows #### in the solution."""
    answer = row["answer"].split("####")[-1].strip()
    prompt = f"{row['question']}\n\nGive the answer as a number only.\n{ANSWER_LINE}"
    return task(f"gsm-{index}", "reasoning", "medium", prompt, answer, "final_answer", "gsm8k")


def mmlu_pro(row: Row, index: int) -> Task:
    """MMLU-Pro: hard multiple choice with up to ten options."""
    options = "\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(row["options"]))
    prompt = f"{row['question']}\n\n{options}\n\nGive the letter of the correct option.\n{ANSWER_LINE}"
    return task(f"mmlu-{row['question_id']}", "reasoning", "hard", prompt, row["answer"],
                "final_answer", "mmlu_pro")


def hard_set() -> list[Task]:
    """The repo's own hard set, moved off the too-generous exact_match scorer."""
    data = json.loads((ROOT / "data" / "tasks_hard.json").read_text())
    tasks = data if isinstance(data, list) else data["tasks"]
    for t in tasks:
        t["source"] = "repo_hard_set"
        if t["scoring"] == "exact_match":
            t["scoring"] = "final_answer"
            t["prompt"] = f"{t['prompt']}\n\n{ANSWER_LINE}"
    return tasks


def build() -> tuple[list[Task], list[Task]]:
    """Return (mixed set, hard set)."""
    with (ROOT / "data" / "public" / "banking77" / "test.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    categories = sorted({r["category"] for r in rows})
    order = list(range(len(rows)))
    random.Random(f"{SEED}-banking77").shuffle(order)
    mixed = [banking(rows[i]["text"], rows[i]["category"], categories, i) for i in order[:96]]
    mixed += sample("rajpurkar/squad_v2", 84, squad)
    mixed += sample("google/IFEval", 72, ifeval)
    mixed += sample("openai/gsm8k", 24, gsm8k)
    mmlu = sample("TIGER-Lab/MMLU-Pro", 54, mmlu_pro)  # disjoint: 24 mixed, 30 hard
    return mixed + mmlu[:24], hard_set() + mmlu[24:]


def build_fresh() -> list[Task]:
    """The next 300 tasks in each dataset's seeded order, unused by build(), same type shares."""
    with (ROOT / "data" / "public" / "banking77" / "test.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    categories = sorted({r["category"] for r in rows})
    order = list(range(len(rows)))
    random.Random(f"{SEED}-banking77").shuffle(order)
    fresh = [banking(rows[i]["text"], rows[i]["category"], categories, i) for i in order[96:192]]
    fresh += sample("rajpurkar/squad_v2", 168, squad)[84:]
    fresh += sample("google/IFEval", 144, ifeval)[72:]
    fresh += sample("openai/gsm8k", 48, gsm8k)[24:]
    return fresh + sample("TIGER-Lab/MMLU-Pro", 78, mmlu_pro)[54:]


def main() -> None:
    """Write both task files, or with --fresh only tasks_public_fresh.json."""
    fresh = "--fresh" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--fresh"]
    out = Path(args[0]) if args else ROOT / "data" / "public" / "benchmark"
    out.mkdir(parents=True, exist_ok=True)
    if fresh:
        data = {"version": 2, "tasks": build_fresh()}
        path = out / "tasks_public_fresh.json"
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        print(f"{path}: {len(data['tasks'])} tasks")
        return
    mixed, hard = build()
    for name, tasks in (("tasks_public.json", mixed), ("tasks_public_hard.json", hard)):
        data = {"version": 2, "tasks": tasks}
        (out / name).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        print(f"{out / name}: {len(tasks)} tasks")


if __name__ == "__main__":
    main()

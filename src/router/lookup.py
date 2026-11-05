"""Free task-type lookup: the most similar labelled tasks vote, with no model call.

Replaces the paid classifier when `RulesPolicy(lookup=...)` is set. Word overlap is
weighted by how rare each word is (TF-IDF cosine), standard library only.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .classifier import Classification, heuristic_classify

BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "data" / "public" / "benchmark"
LABELLED_FILES = ("tasks_public.json", "tasks_public_fresh.json",
                  "tasks_public_fresh2.json", "tasks_public_fresh3.json")


def _bag(text: str) -> Counter:
    return Counter(re.findall(r"[a-z]+", text.lower()))


class Lookup:
    """Votes (task_type, difficulty) from the k nearest labelled tasks."""

    def __init__(self, tasks: Iterable[dict[str, Any]], k: int = 7) -> None:
        tasks = list(tasks)
        bags = [_bag(t["prompt"]) for t in tasks]
        counts = Counter(w for bag in bags for w in bag)
        self.k = k
        self.idf = {w: math.log(len(bags) / n) for w, n in counts.items()}
        self.labels = [(t["task_type"], t["difficulty"]) for t in tasks]
        self.index: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for i, bag in enumerate(bags):
            for word, weight in self._vector(bag).items():
                self.index[word].append((i, weight))

    def _vector(self, bag: Counter) -> dict[str, float]:
        v = {w: (1 + math.log(n)) * self.idf.get(w, 0.0) for w, n in bag.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {w: x / norm for w, x in v.items()}

    def classify(self, task: dict[str, Any]) -> Classification:
        """Predict from the neighbours; a prompt sharing no words gets the free heuristic."""
        scores: Counter = Counter()
        for word, weight in self._vector(_bag(task["prompt"])).items():
            for i, other in self.index.get(word, ()):
                scores[i] += weight * other
        near = [self.labels[i] for i, _ in scores.most_common(self.k)]
        if not near:
            return heuristic_classify(task["prompt"])
        task_type = Counter(label[0] for label in near).most_common(1)[0][0]
        difficulty = Counter(label[1] for label in near).most_common(1)[0][0]
        labelled = task.get("task_type") and task.get("difficulty")
        return Classification(
            task_type=task_type, difficulty=difficulty, cost_usd=0.0, model=None, simulated=False,
            agreed_with_label=((task_type, difficulty) == (task["task_type"], task["difficulty"])
                               if labelled else None),
        )


def public_lookup(exclude_prompts: Iterable[str] = ()) -> Lookup:
    """A Lookup over the public labelled tasks, minus any prompts being tested."""
    skip = set(exclude_prompts)
    return Lookup(t for name in LABELLED_FILES
                  for t in json.loads((BENCHMARK_DIR / name).read_text())["tasks"]
                  if t["prompt"] not in skip)

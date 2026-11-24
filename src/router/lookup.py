"""Free task-type lookup: the most similar labelled tasks vote, with no model call.

Replaces the paid classifier when `RulesPolicy(lookup=...)` is set. Word overlap is
weighted by how rare each word is (TF-IDF cosine), standard library only.
lookup_tasks.json holds the prompts and labels of the four public task sets, inside the
package so it also ships in the Docker image.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter, defaultdict
from functools import cache
from heapq import nlargest
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union

from . import db
from .classifier import Classification, heuristic_classify
from .scoring import DIFFICULTIES, TASK_TYPES

LABELLED_TASKS = Path(__file__).with_name("lookup_tasks.json")


def _bag(text: str) -> Counter:
    return Counter(re.findall(r"[a-z]+", text.lower()))


class Lookup:
    """Votes (task_type, difficulty) from the k nearest labelled tasks."""

    def __init__(self, tasks: Iterable[dict[str, Any]], k: int = 7, live_rows: int = 0) -> None:
        tasks = list(tasks)
        self.live_rows = live_rows  # how many tasks came from the run log (0 for public lookups)
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
        scores: dict[int, float] = defaultdict(float)
        for word, weight in self._vector(_bag(task["prompt"])).items():
            for i, other in self.index.get(word, ()):
                scores[i] += weight * other
        near = [self.labels[i] for i in nlargest(self.k, scores, key=scores.__getitem__)]
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
    return Lookup(t for t in json.loads(LABELLED_TASKS.read_text()) if t["prompt"] not in skip)


@cache
def shared_lookup() -> Lookup:
    """One Lookup over every labelled task, built once per process for live requests."""
    return public_lookup()


def from_log(db_path: Union[str, Path], min_rows: int = 50, min_score: float = 0.7,
             k: int = 7) -> Lookup:
    """A Lookup over every public labelled task plus live rows from the run log with a good outcome.

    A live row's label is the label the router ACTED ON for that request (the lookup's own
    prediction at the time; live traffic has no ground truth). It is kept only because the
    outcome was good (quality_score >= min_score, from user feedback, the cascade verifier
    or a judge). Rows with a low score are dropped, not relabelled: a low score says the
    route failed, not what the right label was.

    The same prompt logged more than once counts once, with its latest row's label. If
    fewer than `min_rows` live rows qualify after that, the result is public-only
    (`live_rows == 0`). The db is read-only here and never created. The benchmark does not
    use this: its lookup (`public_lookup(exclude_prompts)`) never sees live rows.
    """
    public = json.loads(LABELLED_TASKS.read_text())
    rows = db.live_labelled_rows(db_path, min_score, task_types=TASK_TYPES, difficulties=DIFFICULTIES)
    latest = {r["prompt"]: r for r in rows}  # rows come oldest first, so the latest wins
    if len(latest) < min_rows:
        return Lookup(public, k=k)
    live = [{"prompt": p, "task_type": r["task_type"], "difficulty": r["difficulty"]}
            for p, r in latest.items()]
    return Lookup(public + live, k=k, live_rows=len(live))


class RefreshingLookup:
    """Holds a from_log Lookup and rebuilds it every `every` calls to get().

    The first get() builds; after a build, `every` more get() calls are served before the
    next one rebuilds (so with every=100, calls 1 and 101 build). The rebuild runs
    synchronously in the calling request: building over ~1,300 prompts takes milliseconds,
    so a background thread is not worth its complexity. A failed build keeps the previous
    Lookup, or falls back to public_lookup() when there is none yet.
    """

    def __init__(self, db_path: Union[str, Path], every: int = 100, min_rows: int = 50,
                 build: Callable[..., Lookup] = from_log) -> None:
        self.db_path = db_path
        self.every = max(1, every)
        self.min_rows = min_rows
        self.builds = 0
        self.lookup: Optional[Lookup] = None
        self._build = build
        self._since = 0
        self._lock = threading.Lock()

    def _rebuild_locked(self) -> Lookup:
        try:
            self.lookup = self._build(self.db_path, min_rows=self.min_rows)
        except Exception:
            if self.lookup is None:
                self.lookup = public_lookup()
        self.builds += 1
        self._since = 0
        return self.lookup

    def rebuild(self) -> Lookup:
        """Build now, whatever the call count."""
        with self._lock:
            return self._rebuild_locked()

    def get(self) -> Lookup:
        """The current Lookup, rebuilt first on the first call and every `every` calls."""
        with self._lock:
            if self.lookup is None or self._since >= self.every:
                self._rebuild_locked()
            self._since += 1
            assert self.lookup is not None
            return self.lookup

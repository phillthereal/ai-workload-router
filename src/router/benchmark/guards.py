"""Guard rails for paid benchmark runs, shared by run_benchmark.py and `awr bench`.

Moved from run_benchmark.py (a root script the installed package cannot import);
run_benchmark.py imports these back under the same names. The defaults keep the
benchmark's behaviour unchanged.
"""
from __future__ import annotations

import argparse
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Iterator, Optional

from ..adapters import SpendMeter, get_adapter
from ..api import Result
from ..config import ROSTERS
from ..scoring import score

# ponytail: fixed reserve, enough for one worst-case task (a 1,024-token Opus answer
# plus a judge call). The cap is per process: replays are free, but after a stopped
# run pass only what is left of the budget.
_TASK_RESERVE_USD = 0.10


def _spend_cap(raw: str) -> float:
    """argparse type for --max-spend: a finite cap that can cover at least one task."""
    cap = float(raw)
    if not (math.isfinite(cap) and cap > _TASK_RESERVE_USD):
        raise argparse.ArgumentTypeError(f"must be a finite amount above ${_TASK_RESERVE_USD:.2f}")
    return cap


class RunStopped(RuntimeError):
    """A --max-spend run hit its cap or something that would make its result untrue."""


def _check_real(result: Result) -> None:
    """Stop a --max-spend run on anything that would quietly weaken its result."""
    if result.simulated:
        raise RunStopped("an answer was simulated")
    if result.classifier_fell_back:
        raise RunStopped("the classifier failed and the free heuristic picked the route")
    if any("provider error" in line for line in result.trace):
        raise RunStopped("a provider call failed")


def _run_tasks(router: Any, tasks: list[dict], workers: int, max_spend: Optional[float],
               reserve: float = _TASK_RESERVE_USD,
               scorer: Optional[Callable[..., float]] = None,
               checker: Optional[Callable[[Any], None]] = None) -> Iterator[tuple[dict, Any, float]]:
    """Yield (task, result, quality) in task order, answering `workers` tasks at a time.

    The spend check runs before each batch and reserves room for the whole
    batch, so a parallel run stops under the cap just as a serial one does.
    `reserve` is the room kept per task; `scorer(task, response, strict=...)`
    defaults to router.scoring.score and `checker(result)` to _check_real.
    """
    capped = max_spend is not None
    score_fn = scorer or score
    check = checker or _check_real

    def answer(task: dict) -> tuple[dict, Any, float]:
        result = router.complete(task["prompt"], task=task)
        if capped:
            check(result)
        return task, result, score_fn(task, result.response, strict=capped)

    with ThreadPoolExecutor(workers) as pool:
        for start in range(0, len(tasks), workers):
            if max_spend is not None and SpendMeter.usd + reserve * workers >= max_spend:
                raise RunStopped(f"spend cap of ${max_spend:.2f} reached")
            yield from pool.map(answer, tasks[start:start + workers])


def _preflight(roster_name: str, extra_models: Iterable[str] = (),
               adapter_factory: Optional[Callable[..., Any]] = None) -> None:
    """One tiny real call per roster model (plus `extra_models`), so a bad key or empty account stops the run first."""
    factory = adapter_factory or get_adapter
    roster = ROSTERS[roster_name]
    for model in (roster.budget, roster.mid, roster.frontier, *extra_models):
        try:
            ok = factory(model, False, use_cache=False).complete("Reply with OK.", max_tokens=16).success
        except Exception as exc:  # noqa: BLE001 - name only: provider text can hold account details
            raise RunStopped(f"preflight failed on {model} ({type(exc).__name__})") from None
        if not ok:
            raise RunStopped(f"preflight failed on {model}")

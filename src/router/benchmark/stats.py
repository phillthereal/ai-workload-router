"""Paired bootstrap pieces shared by analyze.py, the experiments and `awr bench`.

Moved here unchanged from analyze.py so the installed package can use them;
analyze.py re-imports every name, so its output is byte-identical.
"""
from __future__ import annotations

import random
from typing import Any, Callable, Sequence

N_BOOT, SEED = 10_000, 20261020


def net_reduction(p: Sequence[Sequence[float]]) -> float:  # p: list of (base_cost, exp_net_cost, base_q, exp_q)
    """Net cost reduction in percent: (baseline cost - experimental net cost) / baseline cost."""
    base = sum(x[0] for x in p)
    return (base - sum(x[1] for x in p)) / base * 100


def retention(p: Sequence[Sequence[float]]) -> float:
    """Quality retention in percent: experimental mean quality / baseline mean quality."""
    return (sum(x[3] for x in p) / len(p)) / (sum(x[2] for x in p) / len(p)) * 100


def bootstrap(pairs: Sequence[Any], stat: Callable[[Any], float], n: int = N_BOOT,
              seed: int = SEED, alpha: float = 0.05) -> tuple[float, float, float]:
    """Resample whole tasks (both arms together) with replacement.
    Returns (point, lo, hi) with a percentile CI."""
    rng = random.Random(seed)
    k = len(pairs)
    vals = sorted(stat([pairs[rng.randrange(k)] for _ in range(k)]) for _ in range(n))
    return stat(pairs), vals[int(n * alpha / 2)], vals[int(n * (1 - alpha / 2)) - 1]

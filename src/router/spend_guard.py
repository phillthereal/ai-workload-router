"""Daily budget, per-minute rate limit and frontier gate, shared by the HTTP
server (server.py) and the MCP server (mcp_server.py).

In-process and thread-safe; the day total resets at 00:00 UTC and on restart.
ponytail: per-process only, so two servers sharing one API key each get their
own allowance. A shared store would be the upgrade if that matters.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone


class SpendGuard:
    def __init__(self, daily_budget_usd: float = 1.0, rate_limit_per_min: int = 30,
                 allow_frontier: bool = False) -> None:
        self.daily_budget_usd = daily_budget_usd
        self.rate_limit_per_min = rate_limit_per_min
        self.allow_frontier = allow_frontier
        self._lock = threading.Lock()
        self._hits: deque = deque()  # request times (monotonic) in the last minute
        self._spend = {"day": "", "usd": 0.0}

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll(self) -> None:  # caller holds the lock
        if self._spend["day"] != self._today():
            self._spend.update(day=self._today(), usd=0.0)

    def rate_ok(self) -> bool:
        """Count this request; False if it would exceed the per-minute limit."""
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] > 60:
                self._hits.popleft()
            if len(self._hits) >= self.rate_limit_per_min:
                return False
            self._hits.append(now)
            return True

    def over_budget(self, extra_usd: float = 0.0) -> bool:
        """True once today's spend (plus an optional worst-case `extra_usd`) reaches the cap."""
        with self._lock:
            self._roll()
            return self._spend["usd"] + extra_usd >= self.daily_budget_usd

    def add_spend(self, usd: float) -> None:
        with self._lock:
            self._roll()
            self._spend["usd"] += usd

    def spent_today(self) -> float:
        with self._lock:
            self._roll()
            return self._spend["usd"]

"""Daily budget, per-minute rate limit and frontier gate, shared by the web app
(web.py), the HTTP server (server.py) and the MCP server (mcp_server.py).

Thread-safe. The day total resets at 00:00 UTC. With a `store` path it lives in
a small SQLite file (by default data/spend.db, see `spend_store_from_env`), so it
survives restarts and is shared by every server on this machine that opens the
same file. With `store=None` it is held in memory for this process only. The
per-minute rate limit is always in memory, per process.

If the store cannot be read or written (locked past the timeout, read-only, disk
full), spend is never dropped and never raises: it is kept as a pending amount in
memory, counted in today's total and flushed on the next successful write, and
`over_budget` fails closed (returns True) while the store cannot be read.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SPEND_STORE = Path(__file__).resolve().parents[2] / "data" / "spend.db"

_SCHEMA = "CREATE TABLE IF NOT EXISTS spend (day TEXT PRIMARY KEY, usd REAL NOT NULL)"
_UPSERT = ("INSERT INTO spend(day, usd) VALUES (?, ?) "
           "ON CONFLICT(day) DO UPDATE SET usd = usd + excluded.usd RETURNING usd")
_TIMEOUT_S = 2.0  # wait this long for another process's lock, then fall back

_log = logging.getLogger(__name__)


def spend_store_from_env() -> Path | None:
    """Spend store path from AWR_SPEND_STORE: unset = data/spend.db, empty = in memory."""
    value = os.environ.get("AWR_SPEND_STORE")
    if value is None:
        return DEFAULT_SPEND_STORE
    if value == "":
        return None
    return Path(value)


class SpendGuard:
    """Limits on daily spend and request rate, plus the frontier switch."""

    def __init__(self, daily_budget_usd: float = 1.0, rate_limit_per_min: int = 30,
                 allow_frontier: bool = False, store: str | Path | None = None) -> None:
        self.daily_budget_usd = daily_budget_usd
        self.rate_limit_per_min = rate_limit_per_min
        self.allow_frontier = allow_frontier
        self._lock = threading.Lock()  # spend and the store
        self._rate_lock = threading.Lock()  # rate limiter only, never waits on the store
        self._hits: deque[float] = deque()  # request times (monotonic) in the last minute
        # In memory: today's total when store is None, else spend not yet written to the store.
        self._spend: dict[str, Any] = {"day": "", "usd": 0.0}
        self._store = Path(store) if store else None  # "" means in memory, like AWR_SPEND_STORE=""
        if self._store is not None:
            self._init_store(self._store)

    def _init_store(self, path: Path) -> None:
        try:
            if path.is_dir():
                raise IsADirectoryError(str(path))
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            try:
                conn.execute(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as exc:
            raise ValueError(f"Cannot use spend store {path} ({type(exc).__name__}). Set AWR_SPEND_STORE "
                             "to a writable file path, or to an empty string to keep spend in memory.") from exc

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _connect(self) -> sqlite3.Connection:
        assert self._store is not None
        return sqlite3.connect(self._store, timeout=_TIMEOUT_S)

    def _roll(self) -> None:  # caller holds the lock
        if self._spend["day"] != self._today():
            self._spend.update(day=self._today(), usd=0.0)

    def _read_today(self) -> float:  # caller holds the lock; raises sqlite3.Error if the store fails
        self._roll()
        pending = float(self._spend["usd"])
        if self._store is None:
            return pending
        conn = self._connect()
        try:
            row = conn.execute("SELECT usd FROM spend WHERE day = ?", (self._today(),)).fetchone()
        finally:
            conn.close()
        return (float(row[0]) if row else 0.0) + pending

    def _warn(self, action: str, exc: Exception) -> None:
        _log.warning("spend store %s could not %s (%s); keeping $%.6f pending in memory",
                     self._store, action, type(exc).__name__, self._spend["usd"])

    def rate_ok(self) -> bool:
        """Count this request; False if it would exceed the per-minute limit."""
        now = time.monotonic()
        with self._rate_lock:
            while self._hits and now - self._hits[0] > 60:
                self._hits.popleft()
            if len(self._hits) >= self.rate_limit_per_min:
                return False
            self._hits.append(now)
            return True

    def over_budget(self, extra_usd: float = 0.0) -> bool:
        """True once today's spend (plus an optional worst-case `extra_usd`) reaches the cap."""
        with self._lock:
            try:
                return self._read_today() + extra_usd >= self.daily_budget_usd
            except sqlite3.Error as exc:
                self._warn("be read, refusing until it can", exc)
                return True  # fail closed

    def add_spend(self, usd: float) -> None:
        """Add `usd` to today's total. Never raises: a failed store write stays pending in memory."""
        with self._lock:
            self._roll()
            self._spend["usd"] += usd
            if self._store is None:
                return
            try:
                conn = self._connect()
                try:
                    with conn:  # one transaction: pending + this amount, or nothing
                        conn.execute(_UPSERT, (self._today(), self._spend["usd"])).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._warn("be written", exc)
                return
            self._spend["usd"] = 0.0

    def spent_today(self) -> float:
        """Today's spend so far, in USD."""
        with self._lock:
            try:
                return self._read_today()
            except sqlite3.Error as exc:
                self._warn("be read", exc)
                return float(self._spend["usd"])

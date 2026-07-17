"""
Performance log (P0-5).

SQLite-backed run log (stdlib `sqlite3`, no external dependency). Every run
of the benchmark — one task, routed to one model, under one strategy —
persists task type, difficulty, model, tokens in/out, cost, latency, quality
score, and success, tagged with a `run_group` so a single benchmark
invocation's rows can be queried together. `summary_by_strategy()` is the
query the report (P0-6) is built on.

Default db file: data/runs.db (gitignored). Every function accepts an
optional `db_path` override for tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional, Union

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "runs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_group TEXT NOT NULL,
    strategy TEXT NOT NULL,
    task_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    latency_ms REAL NOT NULL,
    quality_score REAL NOT NULL,
    success INTEGER NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 0,
    response_text TEXT NOT NULL DEFAULT ''
)
"""

# Older data/runs.db files predate the `simulated` / `response_text`
# columns; add them in place rather than forcing a manual `rm data/runs.db`
# on every existing checkout. `response_text` stores the model's raw answer
# text so router.judge_validation can re-score it with a second judge later
# without re-calling the original model — only rows logged after this
# migration landed will have it populated (see run_benchmark.py).
_MIGRATIONS: list[str] = [
    "ALTER TABLE runs ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN response_text TEXT NOT NULL DEFAULT ''",
]


def _resolve_path(db_path: Optional[Union[str, Path]]) -> Path:
    return Path(db_path) if db_path else DB_PATH


def init_db(db_path: Optional[Union[str, Path]] = None) -> Path:
    """
    Initialize the database schema, creating the `runs` table if needed.

    Args:
        db_path: Path to the SQLite file. Defaults to data/runs.db.

    Returns:
        The resolved db path.
    """
    path = _resolve_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(_SCHEMA)
        for migration in _MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists — migration already applied
        conn.commit()
    finally:
        conn.close()
    return path


def log_run(
    *,
    run_group: str,
    strategy: str,
    task_id: str,
    task_type: str,
    difficulty: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: float,
    quality_score: float,
    success: bool,
    simulated: bool = False,
    response_text: str = "",
    db_path: Optional[Union[str, Path]] = None,
) -> int:
    """
    Log a single run to the database.

    Args:
        simulated: True if this run's Response came from MockAdapter (no
            real provider key configured, or the real call failed and
            degraded to the mock) rather than a real provider call. Defaults
            to False for backward compatibility with callers that predate
            real adapters.
        response_text: The model's raw answer text for this run. Optional
            (defaults to "") for backward compatibility with callers that
            predate this column; used by router.judge_validation to re-score
            rubric_judge answers with a second judge without re-calling the
            original model.

    Returns:
        The auto-generated row id.
    """
    path = _resolve_path(db_path)
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            """
            INSERT INTO runs (
                run_group, strategy, task_id, task_type, difficulty, model,
                input_tokens, output_tokens, cost_usd, latency_ms,
                quality_score, success, simulated, response_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_group, strategy, task_id, task_type, difficulty, model,
                input_tokens, output_tokens, cost_usd, latency_ms,
                quality_score, int(success), int(simulated), response_text,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def summary_by_strategy(
    run_group: str, db_path: Optional[Union[str, Path]] = None
) -> dict[str, dict[str, Any]]:
    """
    Aggregate cost/quality/count grouped by strategy for one run_group.

    Args:
        run_group: The run_group to summarize.
        db_path: Optional db path override.

    Returns:
        {strategy: {"total_cost": float, "mean_quality": float, "n": int}}
    """
    path = _resolve_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT strategy,
                   SUM(cost_usd) AS total_cost,
                   AVG(quality_score) AS mean_quality,
                   COUNT(*) AS n
            FROM runs
            WHERE run_group = ?
            GROUP BY strategy
            """,
            (run_group,),
        ).fetchall()
    finally:
        conn.close()
    return {
        row["strategy"]: {
            "total_cost": row["total_cost"] or 0.0,
            "mean_quality": row["mean_quality"] or 0.0,
            "n": row["n"],
        }
        for row in rows
    }


def latency_by_strategy(
    run_group: str, db_path: Optional[Union[str, Path]] = None
) -> dict[str, list[float]]:
    """
    Fetch every logged latency_ms value for a run_group, grouped by strategy.

    Raw per-run values (not pre-aggregated) so the caller can compute
    mean, median, or any other statistic — see router.report, which uses
    this for the benchmark report's Latency section.

    Args:
        run_group: The run_group to query.
        db_path: Optional db path override.

    Returns:
        {strategy: [latency_ms, ...]}
    """
    path = _resolve_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT strategy, latency_ms FROM runs WHERE run_group = ?",
            (run_group,),
        ).fetchall()
    finally:
        conn.close()
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row["strategy"], []).append(row["latency_ms"])
    return grouped


def latest_run_group(db_path: Optional[Union[str, Path]] = None) -> Optional[str]:
    """
    Return the most recently logged run_group (by insertion order).

    Used by router.judge_validation so its runnable entry can default to
    "validate whatever benchmark run I just ran" without the caller having
    to know/pass the run_group string.

    Args:
        db_path: Optional db path override.

    Returns:
        The latest run_group, or None if the runs table is empty (or
        doesn't exist yet).
    """
    path = _resolve_path(db_path)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT run_group FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def fetch_runs(
    run_group: str, db_path: Optional[Union[str, Path]] = None
) -> list[dict[str, Any]]:
    """
    Fetch all logged rows for a run_group as plain dicts (for report breakdowns).

    Args:
        run_group: The run_group to fetch.
        db_path: Optional db path override.

    Returns:
        List of row dicts (one per logged run).
    """
    path = _resolve_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM runs WHERE run_group = ?", (run_group,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]

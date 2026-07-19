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

import json
import sqlite3
from datetime import datetime, timezone
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
    response_text TEXT NOT NULL DEFAULT '',
    effort TEXT,
    roster TEXT NOT NULL DEFAULT 'cross_vendor',
    routing_cost_usd REAL NOT NULL DEFAULT 0.0,
    classifier_agreed INTEGER,
    verifier_scores TEXT,
    created_at TEXT,
    learned_evidence TEXT
)
"""

# Older data/runs.db files predate columns added in later phases; add them in
# place rather than forcing a manual `rm data/runs.db` on every existing
# checkout. `response_text` stores the model's raw answer text so
# router.judge_validation can re-score it with a second judge later without
# re-calling the original model.
#
# The v2 columns:
#   effort            — the effort level this run was produced at (NULL = no
#                       effort/thinking config sent, i.e. the v1 shape). Cost is
#                       unattributable without it, because effort changes output
#                       token count without changing the model or the price.
#   roster            — which ladder this run routed across.
#   routing_cost_usd  — what the classifier call for this task cost. Logged per
#                       row so the report can present savings NET of routing
#                       overhead instead of quietly excluding it.
#   classifier_agreed — whether the predicted label matched the hand label
#                       (NULL when unlabelled or when labels were used directly).
#   verifier_scores   — JSON list of the cascade verifier's per-tier adequacy
#                       scores for this task, e.g. "[0.85]" (NULL for non-cascade
#                       strategies, which have no verifier). Persisting it makes
#                       the escalate_threshold tunable directly from the run log
#                       instead of reconstructing scores by cache replay; see
#                       tune_cascade.py for why that reconstruction was needed
#                       before this column existed.
#
# The v3 columns:
#   created_at        — ISO-8601 UTC timestamp, populated by log_run (via
#                       datetime.now(timezone.utc)) for every row logged from
#                       this point forward. Rows logged before this column
#                       existed stay NULL FOREVER (SQLite ALTER TABLE ADD
#                       COLUMN cannot backfill a real timestamp for history
#                       that was never recorded — there is no honest value to
#                       put there). router.learned's evidence-decay weighting
#                       treats NULL as maximally stale rather than crashing or
#                       treating it as "now" — see router.learned.decay_weight.
#   learned_evidence  — JSON summary of the learned router's decision for this
#                       row (classifier tier, chosen tier, direction, and the
#                       per-tier evidence it considered) — NULL unless this run
#                       was routed with `--learned`. Mirrors verifier_scores:
#                       additive, JSON-encoded, NULL when not applicable.
_MIGRATIONS: list[str] = [
    "ALTER TABLE runs ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN response_text TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE runs ADD COLUMN effort TEXT",
    "ALTER TABLE runs ADD COLUMN roster TEXT NOT NULL DEFAULT 'cross_vendor'",
    "ALTER TABLE runs ADD COLUMN routing_cost_usd REAL NOT NULL DEFAULT 0.0",
    "ALTER TABLE runs ADD COLUMN classifier_agreed INTEGER",
    "ALTER TABLE runs ADD COLUMN verifier_scores TEXT",
    "ALTER TABLE runs ADD COLUMN created_at TEXT",
    "ALTER TABLE runs ADD COLUMN learned_evidence TEXT",
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
    effort: Optional[str] = None,
    roster: str = "cross_vendor",
    routing_cost_usd: float = 0.0,
    classifier_agreed: Optional[bool] = None,
    verifier_scores: Optional[list[float]] = None,
    created_at: Optional[str] = None,
    learned_evidence: Optional[dict[str, Any]] = None,
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
        effort: Effort level this run was produced at, or None if no
            effort/thinking config was sent (the v1 shape).
        roster: Which model ladder this run routed across.
        routing_cost_usd: Cost of the classifier call that produced this run's
            routing decision. Zero when labels were used directly.
        classifier_agreed: Whether the classifier's prediction matched the
            task's hand label, or None when there was nothing to compare.
        verifier_scores: The cascade verifier's per-tier adequacy scores for
            this task (JSON-encoded on write). None for non-cascade strategies,
            which have no verifier. Persisting it lets the escalate_threshold be
            re-tuned directly from the log instead of by cache replay.
        created_at: ISO-8601 timestamp for this row. Defaults to None, which
            means "now" — `log_run` stamps `datetime.now(timezone.utc)`
            itself so every caller gets a real timestamp without having to
            pass one. Tests that need a specific, deterministic timestamp
            (e.g. to exercise evidence decay) can still pass one explicitly.
        learned_evidence: The learned router's decision context for this row
            (JSON-encoded on write) — None unless this run was routed with
            `--learned`. See router.learned.LearnedDecision.

    Returns:
        The auto-generated row id.
    """
    path = _resolve_path(db_path)
    resolved_created_at = created_at or datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            """
            INSERT INTO runs (
                run_group, strategy, task_id, task_type, difficulty, model,
                input_tokens, output_tokens, cost_usd, latency_ms,
                quality_score, success, simulated, response_text,
                effort, roster, routing_cost_usd, classifier_agreed,
                verifier_scores, created_at, learned_evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_group, strategy, task_id, task_type, difficulty, model,
                input_tokens, output_tokens, cost_usd, latency_ms,
                quality_score, int(success), int(simulated), response_text,
                effort, roster, routing_cost_usd,
                None if classifier_agreed is None else int(classifier_agreed),
                None if verifier_scores is None else json.dumps(verifier_scores),
                resolved_created_at,
                None if learned_evidence is None else json.dumps(learned_evidence),
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


def outcomes_for_bucket(
    task_type: str,
    difficulty: str,
    before_run_group: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> list[dict[str, Any]]:
    """
    Fetch logged, non-simulated outcomes matching (task_type, difficulty) —
    the evidence pool router.learned draws on for one feature bucket.

    Only `task_type`/`difficulty` are filterable in SQL because that is all
    this table stores about the task shape; the finer-grained bucket
    (prompt-length band, keyword signal) requires the original prompt text,
    which `runs` does not persist (only `response_text`, the model's
    OUTPUT). router.learned resolves each returned row's prompt from the
    known task registries (by `task_id`) and refines the match itself.

    Args:
        task_type: Task type to match (the classifier's prediction, not
            necessarily the hand label — see run_benchmark.py's note that
            `task_type`/`difficulty` are logged from the task's TRUE label,
            which is what makes this an honest evidence pool: it is grouped
            by ground truth, not by what a possibly-wrong classifier guessed
            on that historical call).
        difficulty: Difficulty to match.
        before_run_group: If given, only rows whose `run_group` sorts
            strictly earlier (plain string comparison) are returned. Every
            `run_group` this codebase produces starts with a
            `YYYYMMDDTHHMMSS` timestamp (see run_benchmark.py), so lexical
            order IS chronological order. This is what makes a chronological
            held-out eval possible without depending on `created_at` (which
            is NULL for every pre-v3 row) — the split is enforced by
            run_group, decay weighting is a separate, second-order concern
            layered on top of whatever the split already allows in.
        db_path: Optional db path override.

    Returns:
        List of row dicts with at least task_id, model, quality_score,
        success, created_at, run_group, simulated (simulated is always 0 in
        the returned rows — simulated/mock outcomes are never evidence).
    """
    path = _resolve_path(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        if before_run_group is not None:
            rows = conn.execute(
                """
                SELECT task_id, model, quality_score, success, created_at,
                       run_group, simulated
                FROM runs
                WHERE task_type = ? AND difficulty = ? AND simulated = 0
                      AND run_group < ?
                """,
                (task_type, difficulty, before_run_group),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT task_id, model, quality_score, success, created_at,
                       run_group, simulated
                FROM runs
                WHERE task_type = ? AND difficulty = ? AND simulated = 0
                """,
                (task_type, difficulty),
            ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def frontier_reference_quality(
    task_type: str,
    difficulty: str,
    frontier_model: str,
    before_run_group: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> Optional[float]:
    """
    Mean logged, non-simulated quality of `frontier_model` on this
    (task_type, difficulty) — the reference the learned router's evidence
    threshold measures "95% retention" against, so the bar is the SAME
    95%-of-frontier framing the rest of the benchmark uses, not a new
    absolute number.

    Args:
        task_type: Task type to match.
        difficulty: Difficulty to match.
        frontier_model: The roster's frontier model name.
        before_run_group: Same chronological cutoff as outcomes_for_bucket.
        db_path: Optional db path override.

    Returns:
        Mean quality_score, or None if no matching frontier evidence exists
        yet (the caller should treat that as "assume the frontier would
        score at the ceiling" — i.e. the strictest possible bar — rather
        than skip the check).
    """
    path = _resolve_path(db_path)
    conn = sqlite3.connect(path)
    try:
        if before_run_group is not None:
            row = conn.execute(
                """
                SELECT AVG(quality_score) FROM runs
                WHERE task_type = ? AND difficulty = ? AND model = ?
                      AND simulated = 0 AND run_group < ?
                """,
                (task_type, difficulty, frontier_model, before_run_group),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT AVG(quality_score) FROM runs
                WHERE task_type = ? AND difficulty = ? AND model = ?
                      AND simulated = 0
                """,
                (task_type, difficulty, frontier_model),
            ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] is not None else None


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

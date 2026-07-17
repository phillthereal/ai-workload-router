"""
Judge validation harness (P0-4 follow-up).

The benchmark report's `rubric_judge` scores come from a single LLM-as-judge
(claude-opus-4-8, the primary/frontier model — see router.scoring). That's a
known-imperfect instrument (see the PRD's non-goals), so this module
cross-checks it two ways:

1. **Inter-judge agreement.** Re-scores each rubric_judge candidate answer
   in a run_group with a SECOND, INDEPENDENT judge model (gpt-4o-mini, a
   different vendor from the primary judge) via the same
   `router.scoring.judge_score_with_model` prompt/parse logic and the same
   record/replay cache the rest of the benchmark uses. Agreement is reported
   as mean absolute difference and % of tasks where the two judges agree
   within a small tolerance.
2. **Human labels (optional).** `export_human_label_sheet()` writes a CSV a
   human can fill in (task, prompt, model answer, rubric, judge score, blank
   human score column); `score_human_agreement()` reads it back once filled
   in and computes the same agreement stats against the primary judge's
   scores.

Re-scoring an old run needs the candidate model's answer text, which is
stored in `runs.response_text` (see router.db — added alongside the
pre-existing `simulated` column via the same ALTER TABLE migration
pattern). Only rows logged by a run_benchmark.py that populates
`response_text` have it; validating an older run_group (or one with no
rubric_judge tasks) raises `JudgeValidationError` with a message telling the
caller to re-run the benchmark first, rather than silently no-op'ing.

Gated behind API-key presence exactly like router.scoring's primary judge:
`router.adapters.get_adapter(SECOND_JUDGE_MODEL)` returns the offline
MockAdapter under AWR_FORCE_MOCK or when OPENAI_API_KEY isn't configured, so
everything in this module is fully exercisable offline — see
validate_judge.py at the repo root for the runnable entry point (it WILL
make real gpt-4o-mini calls when run for real, outside AWR_FORCE_MOCK, with
a key configured).
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path
from typing import Any, Optional, Union

from . import db
from .adapters.base import Response
from .benchmark.tasks import load_tasks
from .config import FRONTIER_MODEL
from .scoring import judge_score_with_model

SECOND_JUDGE_MODEL = "gpt-4o-mini"
AGREEMENT_THRESHOLD = 0.15

DEFAULT_HUMAN_SHEET_PATH = Path(__file__).resolve().parents[2] / "data" / "human_label_sheet.csv"

_HUMAN_SHEET_FIELDS = (
    "task_id", "task_type", "prompt", "model_answer", "rubric",
    "judge_score", "human_score",
)


class JudgeValidationError(RuntimeError):
    """Raised when judge validation can't proceed — e.g. no rubric_judge
    runs in the run_group, or those runs predate the response_text column."""


def _truncate(text: Optional[str], n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _rubric_judge_task_ids(tasks: list[dict[str, Any]]) -> set[str]:
    return {t["id"] for t in tasks if t.get("scoring") == "rubric_judge"}


def collect_rubric_rows(
    run_group: str,
    db_path: Optional[Union[str, Path]] = None,
    tasks: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """
    Fetch the rubric_judge rows logged for `run_group`, joined with each
    task's definition (prompt, reference) from data/tasks.json.

    Args:
        run_group: The run_group to validate.
        db_path: Optional db path override (for tests).
        tasks: Optional pre-loaded task list (defaults to load_tasks()).

    Returns:
        List of row dicts (the logged run fields plus `prompt`/`reference`
        merged in from the task definition), one per rubric_judge task run
        that has non-empty `response_text`.

    Raises:
        JudgeValidationError: If run_group has no logged runs, no
            rubric_judge task runs, or its rubric_judge runs all predate the
            response_text column (nothing to re-score).
    """
    db.init_db(db_path=db_path)
    tasks = tasks if tasks is not None else load_tasks()
    task_by_id = {t["id"]: t for t in tasks}
    rubric_ids = _rubric_judge_task_ids(tasks)

    rows = db.fetch_runs(run_group, db_path=db_path)
    if not rows:
        raise JudgeValidationError(f"No logged runs found for run_group={run_group!r}.")

    rubric_rows = [r for r in rows if r["task_id"] in rubric_ids]
    if not rubric_rows:
        raise JudgeValidationError(
            f"run_group={run_group!r} has no rubric_judge task runs to validate "
            "(only exact_match tasks were logged, or the task set has changed)."
        )

    enriched = [dict(r, **{
        "prompt": task_by_id[r["task_id"]].get("prompt", ""),
        "reference": task_by_id[r["task_id"]].get("reference", ""),
    }) for r in rubric_rows if r["task_id"] in task_by_id]

    with_text = [r for r in enriched if (r.get("response_text") or "").strip()]
    if not with_text:
        raise JudgeValidationError(
            f"run_group={run_group!r} has {len(rubric_rows)} rubric_judge run(s) "
            "but none stored response_text — this run predates that column "
            "(or was logged by an older run_benchmark.py). Re-run "
            "`python run_benchmark.py` to capture response text, then "
            "validate that new run_group instead."
        )
    return with_text


def agreement_stats(
    pairs: list[tuple[float, float]], threshold: float = AGREEMENT_THRESHOLD
) -> dict[str, Any]:
    """
    Compute agreement between two lists of paired scores (e.g. primary judge
    vs second judge, or judge vs human).

    Args:
        pairs: List of (score_a, score_b) tuples.
        threshold: Max |a - b| to count as "agreement" for pct_within_threshold.

    Returns:
        {"n", "mean_abs_diff", "pct_within_threshold", "threshold"}. The
        two stats are None (with n=0) if `pairs` is empty.
    """
    if not pairs:
        return {"n": 0, "mean_abs_diff": None, "pct_within_threshold": None, "threshold": threshold}
    diffs = [abs(a - b) for a, b in pairs]
    within = sum(1 for d in diffs if d <= threshold)
    return {
        "n": len(pairs),
        "mean_abs_diff": statistics.mean(diffs),
        "pct_within_threshold": within / len(pairs) * 100,
        "threshold": threshold,
    }


def run_inter_judge_agreement(
    run_group: Optional[str] = None,
    db_path: Optional[Union[str, Path]] = None,
    second_judge_model: str = SECOND_JUDGE_MODEL,
) -> dict[str, Any]:
    """
    Re-score the rubric_judge tasks of `run_group` (default: the latest
    logged run_group) with `second_judge_model` and compute agreement
    against the scores already stored by the primary judge (FRONTIER_MODEL).

    Under AWR_FORCE_MOCK (or with no key configured for second_judge_model's
    provider), `router.adapters.get_adapter()` returns the offline
    MockAdapter for the second judge too — the exact same fallback the
    primary judge uses in router.scoring — so this is fully exercisable
    offline. A real run makes live `second_judge_model` API calls, gated
    behind that provider's API key exactly like the primary judge.

    Args:
        run_group: run_group to validate. Defaults to db.latest_run_group().
        db_path: Optional db path override (for tests).
        second_judge_model: The independent judge model to re-score with.

    Returns:
        {"run_group", "primary_judge_model", "second_judge_model", "n",
         "mean_abs_diff", "pct_within_threshold", "threshold", "details"}
        where "details" is a list of per-task {"task_id", "primary_score",
        "second_score", "abs_diff"} dicts.

    Raises:
        JudgeValidationError: If there's no run_group to validate, or (via
            collect_rubric_rows) nothing scorable in it.
    """
    if run_group is None:
        run_group = db.latest_run_group(db_path=db_path)
        if run_group is None:
            raise JudgeValidationError("No runs logged yet — nothing to validate. Run the benchmark first.")

    rows = collect_rubric_rows(run_group, db_path=db_path)

    pairs: list[tuple[float, float]] = []
    details: list[dict[str, Any]] = []
    for row in rows:
        primary_score = row["quality_score"]
        response = Response(
            text=row["response_text"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            latency_ms=row["latency_ms"],
            model=row["model"],
            simulated=bool(row["simulated"]),
            success=True,
        )
        task = {"prompt": row["prompt"], "reference": row["reference"]}
        second_score = judge_score_with_model(task, response, second_judge_model)
        pairs.append((primary_score, second_score))
        details.append({
            "task_id": row["task_id"],
            "primary_score": primary_score,
            "second_score": second_score,
            "abs_diff": abs(primary_score - second_score),
        })

    stats = agreement_stats(pairs)
    return {
        "run_group": run_group,
        "primary_judge_model": FRONTIER_MODEL,
        "second_judge_model": second_judge_model,
        **stats,
        "details": details,
    }


def print_agreement_summary(result: dict[str, Any]) -> None:
    """Print an inter-judge (or judge-vs-human) agreement result to stdout."""
    print(f"Judge agreement — run_group={result.get('run_group', '?')}")
    if "primary_judge_model" in result:
        print(f"  primary judge:  {result['primary_judge_model']}")
        print(f"  second judge:   {result['second_judge_model']}")
    print(f"  n tasks scored: {result['n']}")
    if result["n"]:
        print(f"  mean |diff|:    {result['mean_abs_diff']:.3f}")
        print(
            f"  pct within {result['threshold']:.2f}: "
            f"{result['pct_within_threshold']:.1f}%"
        )
    else:
        print("  nothing scored — no agreement stats to show.")


def export_human_label_sheet(
    run_group: Optional[str] = None,
    path: Union[str, Path] = DEFAULT_HUMAN_SHEET_PATH,
    db_path: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Write a CSV sheet for a human to label the rubric_judge tasks of
    `run_group` (default: the latest logged run_group).

    Columns: task_id, task_type, prompt (truncated ~200 chars), model_answer
    (truncated ~300 chars), rubric (=reference), judge_score, human_score
    (left blank for the human to fill in).

    Args:
        run_group: run_group to export. Defaults to db.latest_run_group().
        path: Output CSV path. Defaults to data/human_label_sheet.csv.
        db_path: Optional db path override (for tests).

    Returns:
        The path the sheet was written to.

    Raises:
        JudgeValidationError: If there's no run_group to export, or (via
            collect_rubric_rows) nothing scorable in it.
    """
    if run_group is None:
        run_group = db.latest_run_group(db_path=db_path)
        if run_group is None:
            raise JudgeValidationError("No runs logged yet — nothing to export. Run the benchmark first.")

    rows = collect_rubric_rows(run_group, db_path=db_path)
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_HUMAN_SHEET_FIELDS)
        for row in rows:
            writer.writerow([
                row["task_id"],
                row["task_type"],
                _truncate(row["prompt"], 200),
                _truncate(row["response_text"], 300),
                row["reference"],
                row["quality_score"],
                "",
            ])
    return out_path


def score_human_agreement(
    path: Union[str, Path] = DEFAULT_HUMAN_SHEET_PATH,
    threshold: float = AGREEMENT_THRESHOLD,
) -> dict[str, Any]:
    """
    Read a human-labeled sheet (written by export_human_label_sheet, with
    the human_score column filled in) and compute agreement between
    judge_score and human_score.

    Rows with a blank or unparseable human_score are skipped (not everything
    has to be labeled).

    Args:
        path: Path to the filled-in CSV sheet.
        threshold: Max |judge - human| to count as agreement.

    Returns:
        {"path", "skipped_blank_or_invalid", "n", "mean_abs_diff",
         "pct_within_threshold", "threshold"}

    Raises:
        JudgeValidationError: If the sheet doesn't exist.
    """
    path = Path(path)
    if not path.exists():
        raise JudgeValidationError(
            f"Human label sheet not found: {path}. Run export_human_label_sheet() first."
        )

    pairs: list[tuple[float, float]] = []
    skipped = 0
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            human_raw = (row.get("human_score") or "").strip()
            if not human_raw:
                skipped += 1
                continue
            try:
                human_score = float(human_raw)
                judge_score = float(row["judge_score"])
            except (TypeError, ValueError):
                skipped += 1
                continue
            pairs.append((judge_score, human_score))

    stats = agreement_stats(pairs, threshold=threshold)
    return {"path": str(path), "skipped_blank_or_invalid": skipped, **stats}

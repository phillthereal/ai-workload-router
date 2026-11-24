"""Bring your own workload: load, estimate, run and report one JSONL file (`awr bench`).

Every row is answered by the default rule and by frontier-only, and the report
states cost cut and quality kept with 99% paired-bootstrap ranges. The design is
in docs/BYO_WORKLOAD.md; its section numbers are cited below.

Privacy: no prompt, reference or answer text is ever printed, logged or written
to the report. Errors name a line and a key, never a value.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .. import db, gates
from ..adapters import SpendMeter
from ..adapters.base import Response
from ..api import (
    ANSWER_ONLY, CONSENSUS_FIRST, CONSENSUS_LAST, FINAL_LINE, FrontierOnlyPolicy, Result, Router,
    default_policy,
)
from ..config import FRONTIER_MODEL, ROSTERS, get_model
from ..lookup import shared_lookup
from ..report import COST_REDUCTION_TARGET_PCT, QUALITY_RETENTION_TARGET_PCT, BenchmarkReport, build_report
from ..scoring import _JUDGE_PROMPT_TEMPLATE, rubric_judge, score
from . import guards
from .stats import bootstrap, net_reduction, retention

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
BYO_DIR = DATA_DIR / "byo"
RUNS_DB_NAME = "runs_byo.db"
"""The BYO run log lives in BYO_DIR, never in data/runs.db (section 5)."""

MAX_FILE_BYTES = 5 * 1024 * 1024  # (chosen) section 6
MAX_ROWS = 1_000  # (chosen) section 6
MIN_ROWS_FOR_VERDICT = 30  # (chosen) section 3
OUTPUT_TOKENS = 1_024
"""Worst-case output per call: the adapters' _MAX_TOKENS ceiling while no effort config is sent."""
ALPHA = 0.01  # 99% ranges
JUDGE_MODEL = FRONTIER_MODEL
NO_REFERENCE_RUBRIC = (
    "No reference answer was given. Score how correct, complete and suited to the "
    "task the candidate answer is."
)  # (chosen) section 2
CLOSING_LINE = (
    "A small file gives wide ranges; judged rows are a model's opinion; prices are list prices "
    "from `config.py`, not your bill; the frontier answer is the quality baseline, not the truth; "
    "one run on one file says nothing about other workloads."
)

_ALLOWED_KEYS = ("prompt", "reference", "scoring", "task_type", "difficulty", "id")
_ROW_SCORING = {"final_answer": "final_answer", "exact_match": "exact_match", "judge": "rubric_judge"}
_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_LABEL_RE = re.compile(r"[a-z0-9_-]{1,32}")
_STEM_DROP = re.compile(r"[^A-Za-z0-9_-]")
_KEY_CUT = 40

SOURCE_REFERENCE, SOURCE_JUDGE, SOURCE_SIMULATED = "reference", "judge", "simulated"


class BYOError(ValueError):
    """A workload file or output path that `awr bench` refuses, before any call."""


def _key(name: str) -> str:
    return repr(name[:_KEY_CUT])


# ---------------------------------------------------------------- 1. loading
def load_rows(path: str | Path, roster: str) -> list[dict[str, Any]]:
    """Read and check a workload file. Raises BYOError naming the line and key, never a value."""
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        raise BYOError("cannot read the input file") from None
    if size > MAX_FILE_BYTES:
        raise BYOError(f"file is over the {MAX_FILE_BYTES // (1024 * 1024)} MB limit")
    try:
        text = p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise BYOError("file is not UTF-8") from None

    rows: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(rows) == MAX_ROWS:
            raise BYOError(f"line {lineno}: more than {MAX_ROWS} rows")
        row = _parse_line(line, lineno)
        row_id = row.get("id")
        if row_id is not None:
            if row_id in seen_ids:
                raise BYOError(f"line {lineno}: key 'id' repeats the id on line {seen_ids[row_id]}")
            seen_ids[row_id] = lineno
        row["_line"] = lineno
        rows.append(row)
    if not rows:
        raise BYOError("file has no rows")

    taken = set(seen_ids)
    counter = 0
    out = []
    for row in rows:
        if row.get("id") is None:
            while True:  # an explicit id may already use a generated form
                counter += 1
                generated = f"byo-{counter:04d}"
                if generated not in taken:
                    break
            row["id"] = generated
            taken.add(generated)
        else:
            counter += 1
        out.append(_prepare(row, roster))
    return out


def _parse_line(line: str, lineno: int) -> dict[str, Any]:
    try:
        obj = json.loads(line)
    except ValueError:
        raise BYOError(f"line {lineno}: not valid JSON") from None
    if not isinstance(obj, dict):
        raise BYOError(f"line {lineno}: not a JSON object")
    for key in obj:
        if key not in _ALLOWED_KEYS:
            raise BYOError(f"line {lineno}: unknown key {_key(key)}")
    if "prompt" not in obj:
        raise BYOError(f"line {lineno}: missing key 'prompt'")
    prompt = obj["prompt"]
    if isinstance(prompt, (list, dict)):
        raise BYOError(f"line {lineno}: key 'prompt' is multi-turn or structured; only one text prompt is supported")
    if not isinstance(prompt, str):
        raise BYOError(f"line {lineno}: key 'prompt' must be a string")
    if not prompt.strip():
        raise BYOError(f"line {lineno}: key 'prompt' is empty")
    row: dict[str, Any] = {"prompt": prompt}
    if "id" in obj:
        if not (isinstance(obj["id"], str) and _ID_RE.fullmatch(obj["id"])):
            raise BYOError(f"line {lineno}: key 'id' must match [A-Za-z0-9_.-]{{1,64}}")
        row["id"] = obj["id"]
    if "reference" in obj:
        ref = obj["reference"]
        if not (isinstance(ref, str) and ref.strip()):
            raise BYOError(f"line {lineno}: key 'reference' must be a non-empty string")
        row["reference"] = ref
    if "scoring" in obj:
        if "reference" not in obj:
            raise BYOError(f"line {lineno}: key 'scoring' needs a 'reference'")
        if obj["scoring"] not in _ROW_SCORING:
            raise BYOError(f"line {lineno}: key 'scoring' must be final_answer, exact_match or judge")
        row["scoring"] = obj["scoring"]
    for label in ("task_type", "difficulty"):
        if label in obj:
            value = obj[label]
            if not (isinstance(value, str) and _LABEL_RE.fullmatch(value)):
                raise BYOError(f"line {lineno}: key {label!r} must match [a-z0-9_-]{{1,32}}")
            row[label] = value
    return row


def _prepare(row: dict[str, Any], roster: str) -> dict[str, Any]:
    """Fix the prompt as sent, the scorer, the planned source and the report labels for one row."""
    lineno = row["_line"]
    prompt = row["prompt"]
    reference = row.get("reference")
    choice = row.get("scoring", "final_answer") if reference else "judge"
    if choice == "final_answer" and "Answer:" not in prompt:
        prompt += FINAL_LINE  # once, before either arm sees it, so both arms get the same prompt
    predicted = shared_lookup().classify({"prompt": prompt})
    task = {
        "id": row["id"], "prompt": prompt,
        "reference": reference if reference else NO_REFERENCE_RUBRIC,
        "scoring": _ROW_SCORING[choice],
        "source": SOURCE_REFERENCE if choice != "judge" else SOURCE_JUDGE,
        "user_rubric": choice == "judge" and bool(reference),
        "pred_type": predicted.task_type, "pred_difficulty": predicted.difficulty,
        "task_type": row.get("task_type") or f"{predicted.task_type} (predicted)",
        "difficulty": row.get("difficulty") or f"{predicted.difficulty} (predicted)",
        "line": lineno,
    }
    for model, text in _call_list(task, roster):
        if not gates.fits_context(model, text):
            raise BYOError(f"line {lineno}: key 'prompt' is too long for {model}")
    return task


# ---------------------------------------------------------------- 4. estimating
def _consensus_prompt(prompt: str) -> str:
    return prompt if "Answer:" in prompt else prompt + FINAL_LINE


def _judge_input(task: dict[str, Any]) -> str:
    return _JUDGE_PROMPT_TEMPLATE.format(task_prompt=task["prompt"], reference=task["reference"], candidate="")


def _call_list(task: dict[str, Any], roster_name: str) -> list[tuple[str, str]]:
    """(model, prompt text) for every call the worst case can make on this row."""
    r = ROSTERS[roster_name]
    p = task["prompt"]
    c = _consensus_prompt(p)
    calls = [(r.budget, p), (r.mid, p), (r.frontier, p),
             (CONSENSUS_FIRST, c + ANSWER_ONLY), (r.mid, c + ANSWER_ONLY), (r.budget, c),
             (CONSENSUS_LAST, c + ANSWER_ONLY)]
    if task["source"] == SOURCE_JUDGE:
        calls.append((JUDGE_MODEL, _judge_input(task)))
    return calls


def _cost(model: str, text: str, extra_input: int = 0) -> float:
    tokens = gates.estimate_prompt_tokens(text) + extra_input
    return get_model(model).cost_for_tokens(tokens, OUTPUT_TOKENS)


def row_worst_case(task: dict[str, Any], roster_name: str) -> tuple[float, float, float]:
    """(router arm, frontier arm, judge) list-price worst case in USD for one row.

    Router arm: the dearest path the default rule can take, whatever the lookup predicts.
    """
    r = ROSTERS[roster_name]
    p = task["prompt"]
    c = _consensus_prompt(p)
    e = c + ANSWER_ONLY
    paths = (
        _cost(r.budget, p) + _cost(r.mid, p) + _cost(r.frontier, p),  # classification: agree or frontier
        _cost(CONSENSUS_FIRST, c) + _cost(r.mid, c) + _cost(r.budget, c) + _cost(CONSENSUS_LAST, c),  # reasoning
        _cost(CONSENSUS_FIRST, e) + _cost(r.mid, e) + _cost(CONSENSUS_LAST, e),  # extraction
        _cost(r.frontier, p),  # short writing: the rules table's top
    )
    judge = 2 * _cost(JUDGE_MODEL, _judge_input(task), OUTPUT_TOKENS) if task["source"] == SOURCE_JUDGE else 0.0
    return max(paths), _cost(r.frontier, p), judge


@dataclass
class Estimate:
    """The dry run: list-price worst case per arm and the planned score sources."""

    rows: int
    roster: str
    router_usd: float
    frontier_usd: float
    judge_usd: float
    max_row_usd: float
    max_frontier_call_usd: float
    planned: dict[str, int]
    user_rubrics: int

    @property
    def total_usd(self) -> float:
        """Worst case of the whole run."""
        return self.router_usd + self.frontier_usd + self.judge_usd

    @property
    def reserve_usd(self) -> float:
        """Per-batch reserve: the largest row worst case plus one frontier call (a failover's extra call)."""
        return self.max_row_usd + self.max_frontier_call_usd


def estimate(tasks: Sequence[dict[str, Any]], roster_name: str) -> Estimate:
    """Price the worst case of a run on `tasks`, with no calls."""
    router = frontier = judge = biggest = biggest_frontier = 0.0
    for t in tasks:
        a, b, j = row_worst_case(t, roster_name)
        router, frontier, judge = router + a, frontier + b, judge + j
        biggest, biggest_frontier = max(biggest, a + b + j), max(biggest_frontier, b)
    planned = {SOURCE_REFERENCE: 0, SOURCE_JUDGE: 0}
    for t in tasks:
        planned[t["source"]] += 1
    return Estimate(len(tasks), roster_name, router, frontier, judge, biggest, biggest_frontier, planned,
                    sum(t["user_rubric"] for t in tasks))


def format_dry_run(est: Estimate, input_name: str, mock: bool) -> str:
    """The dry run text, printed before any call."""
    lines = [
        f"Dry run: {est.rows} rows from {input_name}, roster {est.roster}. No calls made.",
        f"List-price worst case (every call writes {OUTPUT_TOKENS:,} output tokens, prices from config.py):",
        f"  router arm (default rule)          ${est.router_usd:.4f}",
        f"  frontier_only arm                  ${est.frontier_usd:.4f}",
        f"  judge ({JUDGE_MODEL}, 2 calls per judged row)  ${est.judge_usd:.4f}",
        f"  total                              ${est.total_usd:.4f}",
        f"Planned score sources for a live run: {est.planned[SOURCE_REFERENCE]} reference, "
        f"{est.planned[SOURCE_JUDGE]} judge ({est.user_rubrics} with your rubric).",
    ]
    if mock:
        lines.append("Mode: mock (AWR_FORCE_MOCK=1). Every answer is simulated and scored by the offline "
                     "mock judge, so every row will be 'simulated'. Nothing is paid.")
    return "\n".join(lines)


# ---------------------------------------------------------------- 5. paths
def report_stem(input_path: str | Path) -> str:
    """The input name cut to [A-Za-z0-9_-]{1,40}."""
    return _STEM_DROP.sub("", Path(input_path).stem)[:40] or "workload"


def default_report_path(stem: str, run_group: str, simulated: bool) -> Path:
    """data/byo/benchmark_report_byo[_simulated]_<stem>_<run_group>.md."""
    kind = "byo_simulated" if simulated else "byo"
    return BYO_DIR / f"benchmark_report_{kind}_{stem}_{run_group}.md"


def check_out_path(raw: str) -> Path:
    """--out must end in .md, must not exist, and stays out of data/ except data/byo/."""
    out = Path(raw).expanduser().resolve()
    if out.suffix != ".md":
        raise BYOError("--out must end in .md")
    if out.exists():
        raise BYOError("--out already exists")
    data, byo = DATA_DIR.resolve(), BYO_DIR.resolve()
    if out.is_relative_to(data) and not out.is_relative_to(byo):
        raise BYOError("--out may not point inside the repo's data/ folder, except data/byo/")
    return out


# ---------------------------------------------------------------- run
@dataclass
class RowOutcome:
    """One row's paired result, kept in memory for the bootstrap."""

    task: dict[str, Any]
    frontier_cost: float = 0.0
    router_net_cost: float = 0.0
    frontier_q: float = 0.0
    router_q: float = 0.0
    source: str = ""


@dataclass
class ByoRun:
    """What a finished run hands to the formatter."""

    run_group: str
    roster: str
    input_name: str
    outcomes: list[RowOutcome]
    degraded: int = 0
    truncated: int = 0
    report: Optional[BenchmarkReport] = None
    out_path: Optional[Path] = None


class _PromptOnly:
    """Hands the router {id, prompt} only, so a label in the file cannot change routing."""

    def __init__(self, router: Router) -> None:
        self.router = router

    def complete(self, prompt: str, task: dict[str, Any]) -> Result:
        return self.router.complete(prompt, task={"id": task["id"]})


def score_row(task: dict[str, Any], response: Response, strict: bool = False) -> float:
    """Score one arm's answer. Both arms of a row always use the same scorer (section 2).

    A simulated answer goes to the offline mock judge on the predicted labels, as in
    web._score; anything else goes to the row's own scorer, with no disk cache.
    """
    if response.simulated:
        mock_task = {"id": task["id"], "task_type": task["pred_type"], "difficulty": task["pred_difficulty"]}
        return rubric_judge(mock_task, response)
    scoring_task = {"prompt": task["prompt"], "reference": task["reference"], "scoring": task["scoring"]}
    return score(scoring_task, response, strict=strict, use_cache=False)


def row_source(task: dict[str, Any], response: Response) -> str:
    """score_source for one answer: simulated first, else the row's planned source."""
    return SOURCE_SIMULATED if response.simulated else task["source"]


def run(tasks: list[dict[str, Any]], *, roster: str, input_name: str, live: bool,
        max_spend: Optional[float], est: Estimate, workers: int = 1,
        db_path: Optional[Path] = None,
        scorer: Callable[..., float] = score_row) -> ByoRun:
    """Answer every row on both arms, log to the BYO run log, and build the report numbers."""
    db_path = db_path or BYO_DIR / RUNS_DB_NAME
    db.init_db(db_path)
    run_group = f"byo-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    r = ROSTERS[roster]
    if live:
        extra = tuple(dict.fromkeys(m for m in (CONSENSUS_FIRST, CONSENSUS_LAST, JUDGE_MODEL)
                                    if m not in (r.budget, r.mid, r.frontier)))
        guards._preflight(roster, extra_models=extra)
    outcomes = {t["id"]: RowOutcome(t) for t in tasks}
    result = ByoRun(run_group, roster, input_name, list(outcomes.values()))
    for pol in (default_policy(), FrontierOnlyPolicy()):
        router = Router(pol, roster_name=roster, allow_mock_fallback=not live, use_cache=False)
        for task, res, quality in guards._run_tasks(_PromptOnly(router), tasks, workers,
                                                    max_spend if live else None,
                                                    reserve=est.reserve_usd, scorer=scorer):
            response = res.response
            assert response is not None  # every policy sets it
            source = row_source(task, response)
            o = outcomes[task["id"]]
            if pol.name == "frontier_only":
                o.frontier_cost, o.frontier_q = res.answer_cost_usd, quality
            else:
                o.router_net_cost, o.router_q = res.answer_cost_usd + res.routing_cost_usd, quality
                o.source = source
            result.degraded += res.degraded
            result.truncated += response.truncated
            db.log_run(
                run_group=run_group, strategy=pol.name, task_id=task["id"],
                task_type=task["task_type"], difficulty=task["difficulty"],
                model=res.model, input_tokens=response.input_tokens,
                output_tokens=response.output_tokens, cost_usd=res.answer_cost_usd,
                latency_ms=res.latency_ms or response.latency_ms, quality_score=quality,
                success=response.success, simulated=response.simulated,
                response_text="",  # answers can quote the prompt
                effort=response.effort, roster=roster,
                routing_cost_usd=res.routing_cost_usd, classifier_agreed=res.classifier_agreed,
                quality_source=source, prompt_text=None, db_path=db_path,
            )
    result.report = build_report(run_group, db_path=db_path)
    return result


# ---------------------------------------------------------------- 3. report
Pair = tuple[float, float, float, float]


def _pairs(outcomes: Sequence[RowOutcome]) -> list[Pair]:
    return [(o.frontier_cost, o.router_net_cost, o.frontier_q, o.router_q) for o in outcomes]


def _ranged(pairs: list[Pair], stat: Callable[[Any], float]) -> Optional[tuple[float, float, float]]:
    """(point, lo, hi) at 99%, or None when a resample makes the statistic undefined."""
    try:
        return bootstrap(pairs, stat, alpha=ALPHA)
    except ZeroDivisionError:
        return None


def _fmt(r: Optional[tuple[float, float, float]]) -> str:
    if r is None:
        return "undefined (a resample has a zero baseline)"
    return f"{r[0]:.1f}% (99% range {r[1]:.1f}% to {r[2]:.1f}%)"


def verdict(n: int, simulated: bool, cost: Optional[tuple[float, float, float]],
            quality: Optional[tuple[float, float, float]]) -> str:
    """meets / misses / not settled at the 99% bounds, with the small-file and simulated rules first."""
    if n < MIN_ROWS_FOR_VERDICT:
        return f"too few rows for a verdict ({n} rows; a verdict needs {MIN_ROWS_FOR_VERDICT})"
    if simulated:
        return "none: a simulated run gets no verdict"
    if cost is None or quality is None:
        return "not settled"
    if cost[1] >= COST_REDUCTION_TARGET_PCT and quality[1] >= QUALITY_RETENTION_TARGET_PCT:
        return "meets"
    if cost[2] < COST_REDUCTION_TARGET_PCT or quality[2] < QUALITY_RETENTION_TARGET_PCT:
        return "misses"
    return "not settled"


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f}%"


def _breakdown(lines: list[str], title: str, key: str, outcomes: Sequence[RowOutcome],
               getter: Callable[[RowOutcome], str]) -> None:
    groups: dict[str, list[RowOutcome]] = {}
    for o in outcomes:
        groups.setdefault(getter(o), []).append(o)
    lines += ["", f"### By {title}", "",
              f"| {key} | n | router cost (net) | frontier cost | router quality | frontier quality |",
              "|---|---|---|---|---|---|"]
    for name in sorted(groups):
        g = groups[name]
        n = len(g)
        lines.append(f"| {name} | {n} | ${sum(o.router_net_cost for o in g):.6f} | "
                     f"${sum(o.frontier_cost for o in g):.6f} | {sum(o.router_q for o in g) / n:.3f} | "
                     f"{sum(o.frontier_q for o in g) / n:.3f} |")


def format_byo_markdown(run_: ByoRun) -> str:
    """The BYO report. It never prints build_report's point-estimate hypothesis verdict."""
    report = run_.report
    assert report is not None
    outcomes = run_.outcomes
    n = len(outcomes)
    pairs = _pairs(outcomes)
    simulated = not report.all_real
    cost, quality = _ranged(pairs, net_reduction), _ranged(pairs, retention)
    counts = {s: sum(o.source == s for o in outcomes) for s in (SOURCE_REFERENCE, SOURCE_JUDGE, SOURCE_SIMULATED)}
    mix = (f"{_pct(counts[SOURCE_REFERENCE], n)} of rows checked against your reference, "
           f"{_pct(counts[SOURCE_JUDGE], n)} judged with no answer key")
    if counts[SOURCE_SIMULATED]:
        mix += f", {_pct(counts[SOURCE_SIMULATED], n)} simulated"

    lines = ["# Bring your own workload: `awr bench` report", ""]
    if simulated:
        lines.append("**SIMULATED**: at least one model was simulated (mock adapter and mock judge). "
                     "These numbers are not measurements of anything.")
    else:
        lines.append("**LIVE**: every answer came from a real provider call.")
    lines += ["", f"Input: `{run_.input_name}`, {n} rows. Roster: `{run_.roster}`. Run group: `{run_.run_group}`.",
              "This is not a published result.", "", "## Headline", "",
              f"- **Cost cut** (net of routing cost): {_fmt(cost)}. Sources: {mix}.",
              f"- **Quality kept** (router mean / frontier mean): {_fmt(quality)}. Sources: {mix}.",
              ""]
    for s in (SOURCE_REFERENCE, SOURCE_JUDGE, SOURCE_SIMULATED):
        group = [o for o in outcomes if o.source == s]
        if group:
            extra = ""
            if s == SOURCE_JUDGE:
                rubrics = sum(o.task["user_rubric"] for o in group)
                extra = f"; {rubrics} with your rubric, {len(group) - rubrics} with the fixed rubric"
            lines.append(f"- Quality kept, `{s}` rows (n={len(group)}{extra}): {_fmt(_ranged(_pairs(group), retention))}")
    lines += ["", f"**Verdict** (targets: cost cut >= {COST_REDUCTION_TARGET_PCT:.0f}%, quality kept >= "
              f"{QUALITY_RETENTION_TARGET_PCT:.0f}%, judged at the 99% bounds): "
              f"{verdict(n, simulated, cost, quality)}.", "",
              "## Breakdowns"]
    lines += ["", "### By score_source", "", "| score_source | n |", "|---|---|"]
    for s in (SOURCE_REFERENCE, SOURCE_JUDGE, SOURCE_SIMULATED):
        lines.append(f"| {s} | {counts[s]} of {n} |")
    _breakdown(lines, "task type", "task type", outcomes, lambda o: o.task["task_type"])
    _breakdown(lines, "difficulty", "difficulty", outcomes, lambda o: o.task["difficulty"])
    lines += ["", "Labels marked `(predicted)` came from the free lookup, not from your file; "
              "they are used for this breakdown only, never for routing.", "",
              "## Run health", "",
              f"- Degraded results (classifier fallback or provider failover): {run_.degraded}",
              f"- Answers that hit the {OUTPUT_TOKENS:,}-token ceiling: {run_.truncated}", "",
              "| Model | Status |", "|---|---|"]
    for model, sim in sorted(report.model_simulated.items()):
        lines.append(f"| `{model}` | {'SIMULATED' if sim else 'LIVE'} |")
    lines += ["", CLOSING_LINE, ""]
    return "\n".join(lines)


def write_report(run_: ByoRun, stem: str, out: Optional[Path] = None) -> Path:
    """Write the report to --out or its data/byo/ name; never overwrites a file."""
    assert run_.report is not None
    path = out or default_report_path(stem, run_.run_group, not run_.report.all_real)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as fh:
        fh.write(format_byo_markdown(run_))
    run_.out_path = path
    return path


def spent() -> float:
    """Real spend so far in this process."""
    return float(SpendMeter.usd)

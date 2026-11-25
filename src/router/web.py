"""Small FastAPI app: ``POST /route`` plus a one-page UI at ``/``.

Install with ``pip install -e ".[web]"`` and run ``ai-router-web``, then open
http://127.0.0.1:8000.

DEMO MODE IS THE DEFAULT, even if real API keys are configured: answers, token
counts and quality scores are simulated and nothing is billed. Real calls need
AWR_WEB_ALLOW_PAID=1, and are then limited by SpendGuard. There is no login, so
the app only accepts localhost Host headers; do not expose it publicly.

A pasted task has no answer key, so the benchmark's judged score cannot be
computed here. ``quality_source`` says what ``quality_score`` really is:
  * "simulated": the placeholder table in router.scoring (demo mode).
  * "verifier": a model other than the answering one (the roster's budget
    model, else CONSENSUS_FIRST, else the mid model) estimating adequacy with
    no answer key. This is an opinion, not a measurement.
  * "self-check": no other model was available, so the mid model graded an
    answer written by the same model.
  * "unavailable": the verifier call failed; the score is null.

The frontier comparison is an ESTIMATE: ``frontier_cost_usd`` is the roster's frontier
model priced at list price for this answer's token counts. The frontier model is never
called. ``saving_pct`` compares the total actually spent (answer + routing + quality
check) with that estimate, so it can be negative.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .adapters import get_adapter
from .adapters.base import Response
from .api import CONSENSUS_FIRST, ProviderError, Result, Router, default_policy
from .cascade import verify_adequacy
from .config import MODEL_TIER, ROSTERS, get_model, get_roster
from .scoring import rubric_judge
from .secrets import force_mock
from .spend_guard import SpendGuard, spend_store_from_env

MAX_PROMPT_CHARS = 32_000
DEFAULT_DAILY_BUDGET_USD = 0.50
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
UI_PATH = Path(__file__).with_name("web_ui.html")


class RouteRequest(BaseModel):
    """Body of POST /route."""

    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    roster: Optional[str] = None


class RouteResponse(BaseModel):
    """What the router did with one prompt."""

    answer: str
    model: str
    tier: str
    roster: str
    task_type: Optional[str]
    difficulty: Optional[str]
    cost_usd: float
    answer_cost_usd: float
    routing_cost_usd: float
    quality_score: Optional[float]
    quality_source: str
    simulated: bool
    degraded: bool
    latency_ms: float
    trace: list[str]
    frontier_model: str
    # ESTIMATE: list price of the frontier model for this answer's token counts; it is not called.
    frontier_cost_usd: float
    # (1 - cost_usd / frontier_cost_usd) * 100; None when the estimate is 0; negative if dearer.
    saving_pct: Optional[float]


def _real_call(model: str, prompt: str, max_tokens: Optional[int] = None) -> Response:
    """One uncached provider call that raises on failure instead of faking a score."""
    response = get_adapter(model, False, use_cache=False).complete(prompt, max_tokens=max_tokens)
    if not response.success:
        raise ProviderError(f"verifier call failed on {model}")
    return response


def _score(prompt: str, result: Result, roster: str) -> tuple[Optional[float], str, float]:
    """Return (quality_score, quality_source, extra_cost_usd) for a routed answer."""
    assert result.response is not None  # RulesPolicy always sets it
    if result.simulated:
        task = {"id": prompt, "task_type": result.task_type, "difficulty": result.difficulty}
        return rubric_judge(task, result.response), "simulated", 0.0
    ladder = get_roster(roster)
    verifier = next((m for m in (ladder.budget, CONSENSUS_FIRST, ladder.mid) if m != result.model), None)
    source = "verifier"
    if verifier is None:  # every candidate wrote the answer: grade it with the mid model
        verifier, source = ladder.mid, "self-check"
    try:
        score, cost = verify_adequacy({"prompt": prompt}, result.response,
                                      verifier, complete=_real_call)
    except Exception:  # noqa: BLE001 - the answer is already paid for, so keep it
        return None, "unavailable", 0.0
    return score, source, cost


def create_app() -> FastAPI:
    """Build the app. Without AWR_WEB_ALLOW_PAID=1 this process stays in demo mode."""
    if os.environ.get("AWR_WEB_ALLOW_PAID") != "1":
        os.environ["AWR_FORCE_MOCK"] = "1"
    guard = SpendGuard(float(os.environ.get("AWR_WEB_DAILY_BUDGET_USD", DEFAULT_DAILY_BUDGET_USD)),
                       store=spend_store_from_env())

    app = FastAPI(title="AI Workload Router", docs_url=None, redoc_url=None)

    # Unlike server.py (no UI, so it refuses every Origin), this app must allow its own page,
    # whose fetch("/route") sends a same-origin Origin header.
    @app.middleware("http")
    async def refuse_cross_origin(request: Request, call_next: Any) -> Any:
        origin = request.headers.get("origin")
        if origin is not None and request.url.path != "/healthz":
            origin_host = origin.split("://", 1)[-1].rstrip("/")
            if origin_host.lower() != request.headers.get("host", "").lower():
                return JSONResponse(
                    {"detail": "Forbidden: cross-origin browser requests are not allowed"},
                    status_code=403)
        return await call_next(request)

    # Added after the Origin middleware so it is outermost: Host is checked first.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def ui() -> str:
        mode = "demo" if force_mock() else "paid"
        return UI_PATH.read_text().replace("__AWR_MODE__", mode)

    @app.post("/route")
    def route(req: RouteRequest) -> RouteResponse:
        roster = req.roster or get_roster(None).name
        if roster not in ROSTERS:
            raise HTTPException(400, f"unknown roster; use one of: {', '.join(sorted(ROSTERS))}")
        paid = not force_mock()
        if paid and not guard.rate_ok():
            raise HTTPException(429, "rate limit reached")
        # ponytail: checked before the call, so requests in flight can overshoot the
        # budget slightly. Reserve a worst case up front if this ever serves real traffic.
        if paid and guard.over_budget():
            raise HTTPException(429, f"daily budget of ${guard.daily_budget_usd:.2f} reached")
        router = Router(default_policy(), roster_name=roster, use_cache=False)
        try:
            result = router.complete(req.prompt)
        except ProviderError:
            raise HTTPException(502, "all providers failed") from None
        if paid and result.degraded:
            score, source, extra = None, "unavailable", 0.0
        else:
            score, source, extra = _score(req.prompt, result, roster)
        result.routing_cost_usd += extra
        if paid:
            guard.add_spend(result.total_cost_usd)
        assert result.response is not None  # RulesPolicy always sets it
        frontier = get_roster(roster).frontier
        frontier_cost = get_model(frontier).cost_for_tokens(
            result.response.input_tokens, result.response.output_tokens)
        saving = (round((1 - result.total_cost_usd / frontier_cost) * 100, 1)
                  if frontier_cost > 0 else None)
        fields: dict[str, Any] = dict(
            answer=result.text, model=result.model, tier=MODEL_TIER[result.model], roster=roster,
            task_type=result.task_type, difficulty=result.difficulty,
            cost_usd=result.total_cost_usd, answer_cost_usd=result.answer_cost_usd,
            routing_cost_usd=result.routing_cost_usd, quality_score=score, quality_source=source,
            simulated=result.simulated, degraded=result.degraded, latency_ms=result.latency_ms, trace=result.trace,
            frontier_model=frontier, frontier_cost_usd=frontier_cost, saving_pct=saving,
        )
        return RouteResponse(**fields)

    return app


def main() -> None:
    """Serve the app on localhost."""
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)

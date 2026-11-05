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
  * "verifier": the roster's mid model estimating adequacy with no answer key.
    This is an opinion, not a measurement, and the mid model grades itself when
    it also wrote the answer.
  * "unavailable": the verifier call failed; the score is null.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .adapters import get_adapter
from .adapters.base import Response
from .api import ProviderError, Result, Router, default_policy
from .cascade import verify_adequacy
from .config import MODEL_TIER, ROSTERS, get_roster
from .scoring import rubric_judge
from .secrets import force_mock
from .spend_guard import SpendGuard

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
    latency_ms: float
    trace: list[str]


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
    try:
        score, cost = verify_adequacy({"prompt": prompt}, result.response,
                                      get_roster(roster).mid, complete=_real_call)
    except Exception:  # noqa: BLE001 - the answer is already paid for, so keep it
        return None, "unavailable", 0.0
    return score, "verifier", cost


def create_app() -> FastAPI:
    """Build the app. Without AWR_WEB_ALLOW_PAID=1 this process stays in demo mode."""
    if os.environ.get("AWR_WEB_ALLOW_PAID") != "1":
        os.environ["AWR_FORCE_MOCK"] = "1"
    guard = SpendGuard(float(os.environ.get("AWR_WEB_DAILY_BUDGET_USD", DEFAULT_DAILY_BUDGET_USD)))

    app = FastAPI(title="AI Workload Router", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    @app.get("/", response_class=HTMLResponse)
    def ui() -> str:
        return UI_PATH.read_text()

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
        score, source, extra = _score(req.prompt, result, roster)
        result.routing_cost_usd += extra
        if paid:
            guard.add_spend(result.total_cost_usd)
        fields: dict[str, Any] = dict(
            answer=result.text, model=result.model, tier=MODEL_TIER[result.model], roster=roster,
            task_type=result.task_type, difficulty=result.difficulty,
            cost_usd=result.total_cost_usd, answer_cost_usd=result.answer_cost_usd,
            routing_cost_usd=result.routing_cost_usd, quality_score=score, quality_source=source,
            simulated=result.simulated, latency_ms=result.latency_ms, trace=result.trace,
        )
        return RouteResponse(**fields)

    return app


def main() -> None:
    """Serve the app on localhost."""
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)

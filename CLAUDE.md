# AI Workload Router

Routes each LLM task to the cheapest model that can answer it well, and logs
cost, latency and quality. Python 3.11+, stdlib-first; offline by default in tests.

## Map
- `src/router/` core code: `api.py` (Router + policies), `router.py`/`classifier.py` (rules routing),
  `cascade.py`, `learned.py`, `jury.py`, `jev.py` (Jev policies), `db.py` (SQLite run log), `report.py`
- `src/router/adapters/` provider clients (`typesafe.py` = Jev, `mock.py` = offline fake)
- `tests/` unittest suite (offline); `data/` tasks, rulebooks, published reports
- `data/runs.db` local run log, gitignored (never commit, never edit by hand)
- `docs/` design notes and findings; `run_benchmark.py`, `serve.py` main entry points

## Check everything (same as CI)
`scripts/check.sh` runs the unit tests plus the offline benchmark smoke run
with `AWR_FORCE_MOCK=1`. It fails if any step fails. Run it before every commit.

## Rules
- Branch before committing; never commit straight to `main`.
- Push, open PRs or merge only when the owner asks. The repo is public once pushed.
- No secrets in command lines or files; never read or print `.env`.
- Paid API calls (OpenAI, Anthropic, TypeSafe/Jev) only with the owner's OK. Tests use mocks.
- Jev is pinned to `jev-1.14.0` (`src/router/adapters/typesafe.py`) because the 0.90
  confidence threshold was calibrated on it. See `docs/JEV_CALIBRATION.md`. Do not change either casually.
- Another local tool imports this checkout live, so do not switch the main checkout's
  branch casually; use a git worktree for feature work.
- Do not delete, skip or weaken tests to get a pass.

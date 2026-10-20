# v5: Kimi K3 — offline evaluation

Moonshot's Kimi K3 is wired in as a mid-tier rival to Claude Sonnet (`kimi_mid` roster:
GPT-4o mini / Kimi K3 / Claude Opus 4.8). The live run is pending account credit, so this
is what can be established **without** calling Kimi. Quality cannot be measured offline.

**Facts (Moonshot docs, 2026-10-20):** `kimi-k3`, $3 / $15 per 1M input/output tokens —
identical to Sonnet 5's list price — 1M context. It **always reasons**, controlled by
`reasoning_effort` = `low` | `high` | `max`, **defaulting to `max`**, and reasoning is billed
as output tokens.

## 1. Wiring and routing (mock mode, `AWR_FORCE_MOCK=1 python3 run_benchmark.py --roster kimi_mid`)

The full pipeline runs. 7 of the 25 tasks route to Kimi — every medium-difficulty
extraction, classification and short-generation task, as the mid slot should:
`ext-002, ext-006, cls-006, cls-007, cls-008, gen-002, gen-005`. Split: 11 budget,
7 Kimi, 7 frontier. Zero cache writes, zero spend.

## 2. Cost projection from real token counts

Those 7 tasks were answered live by mid-tier models in earlier runs (mean 54 input /
140 output tokens). Re-priced at each model's rates, with Kimi's unknown hidden reasoning
as a sensitivity range:

| Model | Cost for the 7 tasks |
|---|---|
| DeepSeek Chat | $0.0012 |
| Claude Sonnet 5 | $0.0159 |
| Kimi K3, no reasoning (theoretical floor) | $0.0159 |
| Kimi K3, +500 reasoning tokens/task | $0.0684 (~4× Sonnet) |
| Kimi K3, +2,000 reasoning tokens/task | $0.2259 (~14× Sonnet) |
| Kimi K3, +8,000 reasoning tokens/task | $0.8559 (~54× Sonnet) |

The reasoning-token figures are **assumptions**, not measurements — the live run replaces
them. The direction does not depend on them: at Sonnet's per-token price plus mandatory
billed reasoning, Kimi's floor *is* Sonnet's cost. **On short-answer mid-tier work Kimi can
only compete on quality, never on price.**

## 3. The change this caused

Because sending no effort means Moonshot's most expensive setting, the Router now defaults
Kimi to `low` (`ModelConfig.default_effort`). An explicit effort still wins, and a failover
hop to another model does not inherit Kimi's default. Tests: `tests/test_kimi.py`.

## 4. What the live run (~$0.30) will answer

Real reasoning tokens at `low` vs `max`, whether `completion_tokens` already includes
reasoning (the billing assumption flagged in `openai_compatible.py`), and Kimi's judged
quality on its 7 tasks vs Sonnet and DeepSeek.

*Also verified live, at zero Kimi cost: with the account unfunded, the router tried Kimi,
received `HTTP 429 exceeded_current_quota_error`, and failed over to Opus — with the reason
shown in the trace and no account identifiers leaked.*

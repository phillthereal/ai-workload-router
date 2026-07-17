# Effort grid — (model × effort) cost/quality frontier

**LIVE RESULTS** — real provider calls, real Opus judge.

| Model | Effort | Total cost | Mean quality | Truncated |
|---|---|---|---|---|
| claude-haiku-4-5 | — | $0.01187 | 0.988 | 0 |
| claude-sonnet-5 | off | $0.08147 | 0.994 | 0 |
| claude-sonnet-5 | low | $0.07215 | 1.000 | 0 |
| claude-sonnet-5 | high | $0.08450 | 0.994 | 0 |
| claude-opus-4-8 | off | $0.11908 | 0.988 | 0 |
| claude-opus-4-8 | low | $0.12190 | 0.984 | 0 |
| claude-opus-4-8 | high | $0.14256 | 0.988 | 0 |

## Key comparisons

| Comparison | Cost Δ | Quality Δ |
|---|---|---|
| Sonnet@low vs Opus@off | -39.4% | +0.012 |
| Opus@high vs Opus@off | +19.7% | -0.000 |
| Sonnet@low vs Haiku | +507.9% | +0.012 |

_n=25. On a task set this easy, quality saturates near 0.99 across the board, so treat sub-percent quality deltas as noise — the cost deltas are the real signal. A harder task set is needed to bend the frontier._

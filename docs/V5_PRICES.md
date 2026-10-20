# V5 price drift (2026-10-20)

Source: https://openrouter.ai/api/v1/models. Drift threshold 10%. Prices are USD per 1M tokens (input/output).

| Model | OpenRouter id | Config | Feed | Diff % (in/out) | Status |
|---|---|---|---|---|---|
| gpt-4o-mini | openai/gpt-4o-mini | 0.15/0.6 | 0.15/0.6 | +0.0/+0.0 | OK |
| deepseek-chat | deepseek/deepseek-chat | 0.27/1.1 | 0.2574/1.029 | -4.7/-6.5 | OK |
| gemini-2.0-flash | google/gemini-2.0-flash-001 | 0.1/0.4 | - | - | NOT_FOUND |
| claude-haiku-4-5 | anthropic/claude-haiku-4.5 | 1/5 | 1/5 | +0.0/+0.0 | OK |
| claude-sonnet-5 | anthropic/claude-sonnet-5 | 3/15 | 2/10 | -33.3/-33.3 | DRIFT |
| claude-opus-4-8 | anthropic/claude-opus-4.8 | 5/25 | 5/25 | +0.0/+0.0 | OK |
| kimi-k3 | moonshotai/kimi-k3 | 3/15 | 0.239/11.94 | -92.0/-20.4 | SUSPECT (>10x jump vs config) |

Caveats: OpenRouter is an aggregator; its price can differ from first-party (e.g. Kimi K3 is ~$0.24/$11.9 there vs $3/$15 from Moonshot). config.py intentionally uses LIST price (e.g. Sonnet 5 at $3/$15 although a promo rate is listed). Drift here is a prompt to look, not a reason to auto-edit. NOT_FOUND/UNMAPPED/SUSPECT/REJECTED models keep their config price in the re-cost and ladder check.

## Re-cost of run_group 20260717T082024* (non-simulated, 50 rows, from logged tokens)

| Prices | Router $ | Frontier-only $ | Savings |
|---|---|---|---|
| config | 0.0585 | 0.1260 | 53.56% |
| feed | 0.0585 | 0.1260 | 53.58% |

## Ladder order (cheapest -> dearest, by output price)

| Roster | Config | Feed | Changed |
|---|---|---|---|
| cross_vendor | budget < mid < frontier | budget < mid < frontier | no |
| claude_tiers | budget < mid < frontier | budget < mid < frontier | no |
| cross_vendor_4 | budget < mid < frontier | budget < mid < frontier | no |
| kimi_mid | budget < mid < frontier | budget < mid < frontier | no |

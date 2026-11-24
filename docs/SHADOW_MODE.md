# Shadow mode

Shadow mode lets a team keep sending every request to the frontier model while
the router writes down what it **would** have done. Each answer still comes
from the frontier. Next to it, the run log records which cheaper models the
router would have called and an estimate of what that would have cost. The
router's own answer is never produced.

It is a selector on the OpenAI-compatible server (`src/router/server.py`),
implemented by `ShadowComparePolicy` in `src/router/shadow.py`. The router's
choice comes from `RulesPolicy.dry_run`, which follows the same steps as the
default rule (free lookup, rules table, agreement or consensus) without calling
any model.

(This is a different thing from [`SHADOW_TEST.md`](SHADOW_TEST.md), which
describes an earlier one-off test.)

## Start it

Free demo, all answers from the offline mock:

```bash
AWR_FORCE_MOCK=1 python serve.py --allow-frontier --log-db shadow.db
```

Without `AWR_FORCE_MOCK=1` every request is answered by the real frontier model
and **spends real money**, within the server's `--daily-budget`.
`--allow-frontier` is required: like `router:frontier`, `router:shadow` is
refused with a 400 without it. `--log-db` is what keeps the estimates; without
it they appear only in each response.

## Send requests

Use the usual OpenAI-style request with `"model": "router:shadow"`:

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer $AWR_SERVER_TOKEN" -H "Content-Type: application/json" \
  -d '{"model": "router:shadow", "messages": [{"role": "user", "content": "What is 17 * 23?"}]}'
```

The answer and `model` are the frontier's. `x_router.shadow` holds:

| field | meaning |
|---|---|
| `task_type`, `difficulty` | the lookup's label for the prompt |
| `would_call` | models the router would call, in order, in its best case |
| `would_keep` | whose answer it would return in that case |
| `estimated_cost_usd` | list price of `would_call` on the frontier answer's token counts |
| `frontier_cost_usd` | what the frontier answer cost |
| `saving_usd` | frontier minus estimate (can be negative) |
| `basis` | a one-line note of how the estimate was made |

If the estimate cannot be made, the frontier answer is still returned,
`shadow` is left out and the trace says `shadow: no estimate (...)`. Other
selectors' responses have no `shadow` key.

## Report

```bash
python scripts/shadow_report.py shadow.db
```

It prints the request count, the frontier spend, the estimated router spend and
the difference, then the same by task type. Mock rows are skipped by default;
`--include-simulated` counts them and marks every line `SIMULATED`, because mock
numbers are not results. With no real shadow rows it exits with an error.

## What the number does and does not prove

- **Quality is unknown.** The router's answer was never produced, so nothing
  here says whether the cheaper models would have answered well enough.
- **The router figure is an estimate in the router's best case.** It prices the
  models at list prices on the frontier answer's token counts and assumes the
  cheap models agree. Real router spend is higher when they disagree (a third
  vote on reasoning, then Sonnet 5.5; the frontier for classification), when a
  provider fails over to a dearer model, and whenever cheap models write
  longer answers than the frontier did. It is lower if they write shorter ones.
- **The labels are unverified.** The lookup's task type and difficulty on real
  prompts have not been checked against hand labels.
- **The frontier spend is real** (outside mock mode): it is what those requests
  actually cost.

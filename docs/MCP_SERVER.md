# MCP server

`ai-router-mcp` lets an MCP client (Claude Code, Cursor, Claude Desktop) ask the router what it would do with a prompt, and what that would cost. It runs over stdio only, as a local process started by the client. There is no HTTP or SSE transport.

The code is `src/router/mcp_server.py`. It wraps functions that already exist (`heuristic_classify`, `route_task`, `gates.estimate_prompt_tokens`, `config.ROSTERS` and `MODELS`, `Router`). Spend limits live in `src/router/spend_guard.py`, which the OpenAI-compatible HTTP server (`src/router/server.py`) now shares.

## The five tools

| Tool | What it returns | Cost |
|---|---|---|
| `classify_task(prompt, roster=None)` | Tier (budget, mid, frontier), model, task type, difficulty, one-line reason | $0 |
| `estimate_cost(prompt, expected_output_tokens=500, roster=None)` | Estimated USD per model in the roster, the router's pick, saving against frontier-only | $0 |
| `list_models()` | Models and rosters from `config.py`: tier, price per million tokens, context window, and whether the provider key is set (true or false only) | $0 |
| `route_request(prompt, roster=None, dry_run=True, max_tokens=500)` | `dry_run=True`: the decision and the most it could cost. `dry_run=False`: a real call, see below | $0 by default |
| `explain_decision(prompt, roster=None)` | Keyword and length signals, the rule that fired, cost at each tier, failover order | $0 |

Also one resource, `router://rosters` (roster definitions as JSON with list prices), and one prompt, `cost_review`, which asks the client model to run `estimate_cost` over a list of prompts (a JSON array of strings, or one prompt per line).

The free tools use the router's free classifier, a rough length and keyword heuristic. They never use the LLM classifier, the cascade verifier or the jury. `docs/REAL_TRAFFIC_ASSESSMENT.md` shows the heuristic sent every real email to the top tier, so read its tier as a starting point. Each result says so.

`estimate_cost` is an estimate. Input tokens come from a local characters-per-token guess (3.5, biased high). Prices are the list prices in `config.py`, read offline; it does not call `prices.py`, which fetches OpenRouter over the network. Reasoning tokens (Kimi K3, for example) are billed as output and are not included.

## Install

```bash
pip install -e ".[mcp]"        # installs mcp>=2.3.0,<3; the core router gains no dependency
```

Tested with `mcp` 2.3.0. In that version the server class is `mcp.server.mcpserver.MCPServer` (renamed from `FastMCP` in 1.x), so the extra is pinned to the 2.x line. Without the extra, `ai-router-mcp` prints an install hint to stderr and exits.

## Register it

Claude Code:

```bash
claude mcp add ai-router -- ai-router-mcp
```

Cursor (`.cursor/mcp.json`) or Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "ai-router": {
      "command": "ai-router-mcp"
    }
  }
}
```

If `ai-router-mcp` is not on the client's PATH, use the absolute path to the console script, or `"command": "python3", "args": ["-m", "router.mcp_server"]` with `PYTHONPATH` pointing at `src`.

## Safety model

**Paid calls are off by default.** The existing caps ($1 a day, 30 requests a minute, the frontier gate) live in the HTTP server, not in `Router`, so a tool calling `Router` directly would have had none of them. They are now in `SpendGuard`, used by both servers (HTTP server behaviour and tests are unchanged). Because an MCP client can be talked into anything by text it reads, `route_request(dry_run=False)` is refused unless the server's own environment has `AI_ROUTER_MCP_ALLOW_PAID=1`. A client cannot set that.

When paid calls are on:

| Control | Setting | Default |
|---|---|---|
| Daily budget (UTC day, in-process, resets on restart) | `AI_ROUTER_MCP_DAILY_BUDGET_USD` | 0.50 |
| Per-call output cap (calls above it are rejected) | `AI_ROUTER_MCP_MAX_TOKENS` | 1000 |
| Frontier model allowed | `AI_ROUTER_MCP_ALLOW_FRONTIER=1` | off |
| Rate limit | fixed | 10 calls a minute |

Before each call the server checks that today's spend plus the worst case for that call (input estimate plus `max_tokens` at the dearest model failover could reach) stays under the budget. Failover is capped below the frontier unless the frontier is allowed. When a cap is hit the client gets a plain error and nothing is retried.

**Nothing is stored.** Real calls bypass the record/replay disk cache and the run log, so no prompt or answer is written to `.cache/` or `data/runs.db`. Both are opt-in parameters (`Router(use_cache=False, max_tier=...)`); the defaults everywhere else are unchanged.

**Answers are untrusted.** A real answer comes back in a field named `untrusted_model_output` with a warning. A client should treat it as data, never as instructions.

**Input is untrusted.** Prompts must be non-empty strings of at most 32,000 characters; `expected_output_tokens` must be 1 to 32,000; roster names must exist; `dry_run` must be a boolean. Bad input returns a short error message with no stack trace. Unexpected errors return "internal error" and log only the exception type to stderr. The `cost_review` prompt wraps each prompt in `untrusted.fence` tags so a reviewed prompt cannot close the wrapper and give the client model orders.

**Secrets.** No tool returns, logs or echoes a key. `list_models` reports `api_key_set` as a boolean for each model. Be aware that this tells the client which providers you have configured.

**stdout is the protocol.** Nothing in the server prints to stdout; logging goes to stderr. A test checks this.

## Limitations

- The free classifier is a length and keyword heuristic. It cannot tell an easy long prompt from a hard one, and `docs/REAL_TRAFFIC_ASSESSMENT.md` measured it saving nothing on real email. The tools do not use the LLM classifier, cascade or jury, so none of the cheap-first savings measured elsewhere in this repo appear here.
- Cost figures are estimates from list prices in `config.py`, which can go stale, and a character-based token guess.
- The daily budget and rate limit are per process. Two server processes sharing one API key each get their own allowance, and a restart resets the total.
- Single-turn only: one prompt in, one routing decision out. No streaming, no conversation history, no feedback tool, no learned or cascade policy.
- A paid call goes to whichever roster model the heuristic picks. There is no quality check on the answer.
- stdio only. There is no authentication because the client launches the process itself; do not wrap it in a network transport without adding it.
- It does not write to the Notion log, email triage or any other production data path.

## Tests

`tests/test_mcp_server.py` covers each tool, the spend caps, the opt-in, the frontier gate, the rate limit, the no-cache and no-log rule, validation, stdout silence, and a stdio end-to-end run using the SDK client. Free tools are tested with the adapters and the network patched to raise. The SDK-dependent tests skip when `mcp` is not installed. Run the demo: `python3 experiments/mcp_demo.py`.

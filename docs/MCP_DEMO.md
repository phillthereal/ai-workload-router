# MCP demo: a real stdio session

This is the output of `python3 experiments/mcp_demo.py` run on 2026-10-27. The script starts `ai-router-mcp` as a subprocess and talks to it with the MCP SDK's own stdio client (mcp 2.3.0). It uses free tools and dry-run only, so no model was called and nothing was spent. Output is pasted unedited.

What it shows, with the numbers from this run:

- Three prompts priced at 300 output tokens: the short note and the sentiment review both go to `gpt-4o-mini` at about $0.00018, 97.6% below frontier-only ($0.0076). The reasoning prompt goes to `claude-opus-4-8` at $0.0077, a 0% saving.
- `explain_decision` on the reasoning prompt: the keyword "explain your reasoning" fired, the rule `reasoning/easy -> frontier` applied, and the same prompt would cost $0.0003 on the budget model at 500 output tokens against $0.0127 on the frontier model.
- `route_request` with `dry_run=false` is refused because the server was started without `AI_ROUTER_MCP_ALLOW_PAID=1`.
- An empty prompt returns one line, no stack trace.
- The estimates are for a 23 to 34 token input, so the output-token figure drives most of the cost. All token counts are local estimates.

```text
tools: ['classify_task', 'estimate_cost', 'list_models', 'route_request', 'explain_decision']

### estimate_cost: short note
budget -> gpt-4o-mini  $0.000183  vs frontier-only $0.007615  (saving 97.6%)  [23 input tokens, estimate]

### estimate_cost: classification
budget -> gpt-4o-mini  $0.000185  vs frontier-only $0.00767  (saving 97.6%)  [34 input tokens, estimate]

### estimate_cost: reasoning
frontier -> claude-opus-4-8  $0.007665  vs frontier-only $0.007665  (saving 0.0%)  [33 input tokens, estimate]

### explain_decision: reasoning prompt
{
  "tier": "frontier",
  "model": "claude-opus-4-8",
  "roster": "cross_vendor",
  "signals": {
    "task_type": "reasoning",
    "difficulty": "easy",
    "keyword_groups_matched": [
      "reasoning"
    ],
    "length_chars_scored": 114,
    "meta_claims_removed": false,
    "difficulty_rule": "under 120 chars easy, under 220 medium, else hard"
  },
  "rule_applied": "reasoning/easy -> frontier per DEFAULT_ROUTING_RULES",
  "gates": {
    "models_removed_by_context_gate": []
  },
  "reason": "reasoning/easy -> frontier tier per default policy; chose claude-opus-4-8 (roster=cross_vendor, effort=None)",
  "options_at_500_output_tokens": [
    {
      "tier": "budget",
      "model": "gpt-4o-mini",
      "estimated_cost_usd": 0.000305
    },
    {
      "tier": "mid",
      "model": "deepseek-chat",
      "estimated_cost_usd": 0.000559
    },
    {
      "tier": "frontier",
      "model": "claude-opus-4-8",
      "estimated_cost_usd": 0.012665
    }
  ],
  "failover_order": [
    "claude-opus-4-8"
  ],
  "failover_note": "Failover happens only on real calls, and is capped below the frontier unless AI_ROUTER_MCP_ALLOW_FRONTIER=1.",
  "cost_usd": 0.0,
  "note": "Classified by the free classifier, a rough length and keyword heuristic. docs/REAL_TRAFFIC_ASSESSMENT.md found it routed 100% of real email to the top tier, so treat the tier as a starting point, not a verdict."
}

### route_request (dry_run defaults to true)
{
  "dry_run": true,
  "tier": "budget",
  "model": "gpt-4o-mini",
  "roster": "cross_vendor",
  "task_type": "short_generation",
  "difficulty": "easy",
  "reason": "short_generation/easy -> budget tier per default policy; chose gpt-4o-mini (roster=cross_vendor, effort=None)",
  "max_cost_usd_if_run": 0.000303,
  "cost_usd": 0.0,
  "note": "Classified by the free classifier, a rough length and keyword heuristic. docs/REAL_TRAFFIC_ASSESSMENT.md found it routed 100% of real email to the top tier, so treat the tier as a starting point, not a verdict. No model was called."
}

### route_request dry_run=false, paid calls not enabled  [error]
Error executing tool route_request: Paid calls are disabled on this server. The owner must start it with AI_ROUTER_MCP_ALLOW_PAID=1 to allow dry_run=false. Use dry_run=true.

### classify_task with an empty prompt  [error]
Error executing tool classify_task: prompt must not be empty

### list_models (key flags only)
gpt-4o-mini        budget    $0.15/$0.6 per 1M  key set: True
deepseek-chat      mid       $0.27/$1.1 per 1M  key set: True
gemini-2.0-flash   budget    $0.1/$0.4 per 1M  key set: True
claude-haiku-4-5   budget    $1.0/$5.0 per 1M  key set: True
claude-sonnet-5    mid       $3.0/$15.0 per 1M  key set: True
claude-opus-4-8    frontier  $5.0/$25.0 per 1M  key set: True
kimi-k3            mid       $3.0/$15.0 per 1M  key set: True

### resource router://rosters (first lines)
{
  "cross_vendor": {
    "budget": "gpt-4o-mini",
    "mid": "deepseek-chat",
    "frontier": "claude-opus-4-8",
    "frontier_to_budget_output_price_ratio": 41.7,
    "models": {
      "budget": {

### prompt cost_review (first 300 chars)
Review the prompts below for cost. For each one, call the estimate_cost tool (it is free and offline), then report the model the router would pick, the estimated cost, and the saving against the frontier model. Finish with one paragraph on which prompts are worth rewriting to be shorter or to need l
```

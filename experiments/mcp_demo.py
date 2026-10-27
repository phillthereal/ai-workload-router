"""Drive the MCP server over stdio with the SDK client and print what comes back.
Free tools and dry-run only: no model is called. Run: python experiments/mcp_demo.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = {
    "short note": "Summarise this note in one line: the team meeting has moved to Friday at 10am.",
    "classification": "Classify the sentiment of this review as positive or negative: 'The battery died in a week and support never replied.'",
    "reasoning": "Explain your reasoning step by step: three friends split a 90 pound bill, one pays double. How much does each pay?",
}


def show(title, res):
    body = res.structured_content if res.structured_content is not None else res.content[0].text
    print(f"\n### {title}" + ("  [error]" if res.is_error else ""))
    print(json.dumps(body, indent=2) if not isinstance(body, str) else body)


async def main():
    env = {k: v for k, v in os.environ.items() if not k.startswith("AI_ROUTER_MCP_")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "router.mcp_server"],
                                   env={**env, "PYTHONPATH": str(ROOT / "src")})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        print("tools:", [t.name for t in (await s.list_tools()).tools])
        for name, p in PROMPTS.items():
            res = await s.call_tool("estimate_cost", {"prompt": p, "expected_output_tokens": 300})
            c = res.structured_content
            print(f"\n### estimate_cost: {name}\n{c['router_pick']['tier']} -> {c['router_pick']['model']}  "
                  f"${c['router_pick']['estimated_cost_usd']}  vs frontier-only ${c['frontier_only_cost_usd']}  "
                  f"(saving {c['saving_vs_frontier_pct']}%)  [{c['estimated_input_tokens']} input tokens, estimate]")
        show("explain_decision: reasoning prompt", await s.call_tool("explain_decision", {"prompt": PROMPTS["reasoning"]}))
        show("route_request (dry_run defaults to true)", await s.call_tool("route_request", {"prompt": PROMPTS["short note"]}))
        show("route_request dry_run=false, paid calls not enabled", await s.call_tool("route_request", {"prompt": PROMPTS["short note"], "dry_run": False}))
        show("classify_task with an empty prompt", await s.call_tool("classify_task", {"prompt": "  "}))
        res = await s.call_tool("list_models", {})
        print("\n### list_models (key flags only)")
        for m in res.structured_content["models"]:
            print(f"{m['name']:18} {m['tier']:9} ${m['usd_per_1m_input_tokens']}/${m['usd_per_1m_output_tokens']} per 1M  key set: {m['api_key_set']}")
        print("\n### resource router://rosters (first lines)")
        print("\n".join((await s.read_resource("router://rosters")).contents[0].text.splitlines()[:8]))
        g = await s.get_prompt("cost_review", {"prompts": json.dumps(list(PROMPTS.values())[:2])})
        print("\n### prompt cost_review (first 300 chars)\n" + g.messages[0].content.text[:300])


asyncio.run(main())

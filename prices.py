#!/usr/bin/env python3
"""
Price-drift check: compare hardcoded src/router/config.py prices with
OpenRouter's free public model feed. READ-ONLY -- never edits config.py; the
feed is untrusted data and a human decides whether to change prices.

Usage:
    python3 prices.py                       # live fetch, markdown to stdout
    python3 prices.py --offline FEED.json   # saved feed, no network
    python3 prices.py --write docs/V5_PRICES.md
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import sqlite3
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from router.config import MODELS, ROSTERS  # noqa: E402

FEED_URL = "https://openrouter.ai/api/v1/models"
MAX_BYTES = 10 * 1024 * 1024
TIMEOUT_S = 10
MAX_PRICE_PER_1M = 1000.0   # above this a price is SUSPECT
MAX_JUMP = 10.0             # >10x up or down vs config is SUSPECT

# Explicit config-id -> OpenRouter id. Never guessed: a model missing here is
# reported UNMAPPED; one mapped but absent from the feed is NOT_FOUND.
OPENROUTER_IDS: dict[str, str] = {
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "deepseek-chat": "deepseek/deepseek-chat",
    "gemini-2.0-flash": "google/gemini-2.0-flash-001",
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "claude-sonnet-5": "anthropic/claude-sonnet-5",
    "claude-opus-4-8": "anthropic/claude-opus-4.8",
    "kimi-k3": "moonshotai/kimi-k3",
}

USABLE = ("OK", "DRIFT")  # statuses whose feed price may be used for re-costing


class FeedError(ValueError):
    pass


def fetch_live() -> bytes:
    with urllib.request.urlopen(FEED_URL, timeout=TIMEOUT_S) as r:
        raw = r.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise FeedError("feed exceeds size cap")
    return raw


def parse_feed(raw: bytes | str) -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
    """-> ({id: (in_per_1m, out_per_1m)}, {id: reason_rejected}). Raises FeedError
    if the document itself is malformed."""
    if len(raw) > MAX_BYTES:
        raise FeedError("feed exceeds size cap")
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise FeedError(f"invalid JSON: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        raise FeedError("unexpected shape: expected {'data': [...]}")
    ok: dict[str, tuple[float, float]] = {}
    bad: dict[str, str] = {}
    for m in doc["data"]:
        mid = m.get("id") if isinstance(m, dict) else None
        if not isinstance(mid, str):
            continue
        try:
            p = m["pricing"]
            vals = []
            for k in ("prompt", "completion"):
                v = float(p[k])
                if not math.isfinite(v) or v < 0:
                    raise ValueError(f"{k}={p[k]!r} not a finite non-negative number")
                vals.append(v * 1e6)
            ok[mid] = (vals[0], vals[1])
        except (KeyError, TypeError, ValueError) as e:
            bad[mid] = f"bad pricing: {e}"
    return ok, bad


def drift_rows(feed, rejected, threshold_pct=10.0, models=None, ids=None):
    """One dict per config model: status in OK / DRIFT / SUSPECT / REJECTED /
    NOT_FOUND / UNMAPPED, plus config/feed prices and % diffs."""
    models = MODELS if models is None else models
    ids = OPENROUTER_IDS if ids is None else ids
    rows = []
    for name, cfg in models.items():
        r = {"model": name, "or_id": ids.get(name), "cfg": (cfg.cost_per_1m_input_tokens,
             cfg.cost_per_1m_output_tokens), "feed": None, "pct": None, "note": ""}
        oid = r["or_id"]
        if oid is None:
            r["status"] = "UNMAPPED"
        elif oid in rejected:
            r["status"], r["note"] = "REJECTED", rejected[oid]
        elif oid not in feed:
            r["status"] = "NOT_FOUND"
        else:
            fin, fout = r["feed"] = feed[oid]
            cin, cout = r["cfg"]
            r["pct"] = tuple(((f - c) / c * 100) if c else (0.0 if f == 0 else math.inf)
                             for f, c in ((fin, cin), (fout, cout)))
            if max(fin, fout) > MAX_PRICE_PER_1M:
                r["status"], r["note"] = "SUSPECT", f"> ${MAX_PRICE_PER_1M:g}/1M"
            elif any(c > 0 and not (1 / MAX_JUMP <= f / c <= MAX_JUMP)
                     for f, c in ((fin, cin), (fout, cout))):
                r["status"], r["note"] = "SUSPECT", f">{MAX_JUMP:g}x jump vs config"
            elif any(abs(p) > threshold_pct for p in r["pct"]):
                r["status"] = "DRIFT"
            else:
                r["status"] = "OK"
        rows.append(r)
    return rows


def feed_prices(rows) -> dict[str, tuple[float, float]]:
    """Usable feed prices only; everything else falls back to config."""
    return {r["model"]: r["feed"] for r in rows if r["status"] in USABLE}


def _cost(tokens_in, tokens_out, prices):
    return tokens_in / 1e6 * prices[0] + tokens_out / 1e6 * prices[1]


def recost(db_path, run_group_prefix, fprices, models=None):
    """Re-cost non-simulated runs of a run_group (prefix match) from logged
    tokens. Returns dict with router/frontier totals + savings % under config
    and feed prices, and models that fell back to config under feed."""
    models = MODELS if models is None else models
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT strategy, model, input_tokens, output_tokens, routing_cost_usd FROM runs "
            "WHERE run_group LIKE ? AND simulated = 0", (run_group_prefix + "%",)).fetchall()
    finally:
        conn.close()
    cfg = {n: (m.cost_per_1m_input_tokens, m.cost_per_1m_output_tokens) for n, m in models.items()}
    out = {"n": len(rows), "fallback": sorted({r[1] for r in rows if r[1] not in fprices})}
    for label, table in (("config", cfg), ("feed", {**cfg, **fprices})):
        tot = {"router": 0.0, "frontier_only": 0.0}
        for strat, model, ti, to, rc in rows:
            if strat in tot:
                tot[strat] += _cost(ti, to, table[model]) + (rc or 0.0)  # routing cost not re-priced
        f = tot["frontier_only"]
        out[label] = {"router": tot["router"], "frontier": f,
                      "savings_pct": (f - tot["router"]) / f * 100 if f else 0.0}
    return out


def ladder_changes(fprices, rosters=None, models=None):
    """Per roster: tier order by output price (tie: input) under config vs feed."""
    rosters = ROSTERS if rosters is None else rosters
    models = MODELS if models is None else models
    cfg = {n: (m.cost_per_1m_output_tokens, m.cost_per_1m_input_tokens) for n, m in models.items()}
    live = {**cfg, **{n: (p[1], p[0]) for n, p in fprices.items()}}
    res = []
    for r in rosters.values():
        tiers = {"budget": r.budget, "mid": r.mid, "frontier": r.frontier}
        order = lambda t: sorted(tiers, key=lambda k: t[tiers[k]])  # noqa: E731
        a, b = order(cfg), order(live)
        res.append({"roster": r.name, "config": a, "feed": b, "changed": a != b})
    return res


def _fmt(p):
    return "-" if p is None else f"{p[0]:.4g}/{p[1]:.4g}"


def render(rows, rc, ladders, threshold, run_group, source, date):
    L = [f"# V5 price drift ({date})", "",
         f"Source: {source}. Drift threshold {threshold:g}%. Prices are USD per 1M tokens (input/output).", "",
         "| Model | OpenRouter id | Config | Feed | Diff % (in/out) | Status |", "|---|---|---|---|---|---|"]
    for r in rows:
        pct = "-" if r["pct"] is None else "/".join(f"{p:+.1f}" for p in r["pct"])
        note = f" ({r['note']})" if r["note"] else ""
        L.append(f"| {r['model']} | {r['or_id'] or '-'} | {_fmt(r['cfg'])} | {_fmt(r['feed'])} | {pct} | {r['status']}{note} |")
    L += ["", "Caveats: OpenRouter is an aggregator; its price can differ from first-party "
          "(e.g. Kimi K3 is ~$0.24/$11.9 there vs $3/$15 from Moonshot). config.py intentionally "
          "uses LIST price (e.g. Sonnet 5 at $3/$15 although a promo rate is listed). Drift here is "
          "a prompt to look, not a reason to auto-edit. NOT_FOUND/UNMAPPED/SUSPECT/REJECTED models "
          "keep their config price in the re-cost and ladder check.", ""]
    L += [f"## Re-cost of run_group {run_group}* (non-simulated, {rc['n']} rows, from logged tokens)", "",
          "| Prices | Router $ | Frontier-only $ | Savings |", "|---|---|---|---|"]
    for k in ("config", "feed"):
        L.append(f"| {k} | {rc[k]['router']:.4f} | {rc[k]['frontier']:.4f} | {rc[k]['savings_pct']:.2f}% |")
    if rc["fallback"]:
        L.append(f"\nUnder feed prices these models fell back to config price: {', '.join(rc['fallback'])}.")
    L += ["", "## Ladder order (cheapest -> dearest, by output price)", "",
          "| Roster | Config | Feed | Changed |", "|---|---|---|---|"]
    for x in ladders:
        L.append(f"| {x['roster']} | {' < '.join(x['config'])} | {' < '.join(x['feed'])} | {'YES' if x['changed'] else 'no'} |")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offline", metavar="FILE", help="read a saved feed JSON instead of the network")
    ap.add_argument("--threshold", type=float, default=10.0)
    ap.add_argument("--run-group", default="20260717T082024", help="run_group prefix to re-cost (v1)")
    ap.add_argument("--db", default=str(Path(__file__).parent / "data" / "runs.db"))
    ap.add_argument("--write", metavar="FILE", help="also write the markdown here")
    a = ap.parse_args(argv)
    try:
        raw = Path(a.offline).read_bytes() if a.offline else fetch_live()
        feed, rejected = parse_feed(raw)
    except (OSError, FeedError) as e:
        sys.exit(f"prices: cannot get a valid feed: {e}")
    rows = drift_rows(feed, rejected, a.threshold)
    fp = feed_prices(rows)
    out = render(rows, recost(a.db, a.run_group, fp), ladder_changes(fp), a.threshold, a.run_group,
                 f"offline file {a.offline}" if a.offline else FEED_URL, datetime.date.today().isoformat())
    print(out)
    if a.write:
        Path(a.write).write_text(out)


if __name__ == "__main__":
    main()

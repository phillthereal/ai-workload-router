"""Cost-only assessment of the router against real email-classification traffic.

Reads calls.jsonl produced by a local extraction of n8n execution data (path
given as argv[1]); each line: kind, started, prompt, pt, ct, cat. The private
data is never embedded or printed here - only aggregates are output.
Makes ZERO API calls. Quality on cheaper models is UNMEASURED.
"""
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from router.classifier import heuristic_classify  # noqa: E402
from router.config import MODELS, ROSTERS, Roster  # noqa: E402
from router.router import route_task  # noqa: E402

BASE = "claude-haiku-4-5"
MINI = "gpt-4o-mini"
PROTECTED = {"ACTION_NEEDED", "JOB_REJECTION", "RECRUITER_REPLY"}
# Roster lives here, not in config.py: frontier = CURRENT production model.
ROSTERS["assess"] = Roster("assess", budget=MINI, mid="deepseek-chat", frontier=BASE)


def cost(model, pt, ct):
    m = MODELS[model]
    return (pt * m.cost_per_1m_input_tokens + ct * m.cost_per_1m_output_tokens) / 1e6


def total(calls, choose):
    """Sum cost of calls, each priced on the model choose(call) returns."""
    return sum(cost(choose(c), c["pt"], c["ct"]) for c in calls)


def cascade_total(calls):
    """Cheap model first for all; protected categories re-run on the base model."""
    return total(calls, lambda c: MINI) + sum(
        cost(BASE, c["pt"], c["ct"]) for c in calls if c.get("cat") in PROTECTED)


def routed_model(c, i):
    d = heuristic_classify(c["prompt"])
    return route_task({"id": str(i), "prompt": c["prompt"]}, task_type=d.task_type,
                      difficulty=d.difficulty, roster_name="assess").chosen_model


def main(path):
    calls = [json.loads(l) for l in open(path)]
    cls = [c for c in calls if c["kind"] == "classify"]
    alr = [c for c in calls if c["kind"] == "alerts"]
    ts = sorted(datetime.fromisoformat(c["started"].replace("Z", "+00:00")) for c in calls)
    days = (ts[-1] - ts[0]).total_seconds() / 86400
    uniq = len({c["prompt"] for c in cls})
    n = len(cls)
    print(f"classifier calls {n} (unique prompts {uniq}), alert-scoring calls {len(alr)}, window {days:.2f} days")
    print("category dist:", {k: f"{v} ({v / n:.0%})" for k, v in Counter(c["cat"] for c in cls).most_common()})
    for name, grp in (("classify", cls), ("alerts", alr)):
        pt, ct = sum(c["pt"] for c in grp), sum(c["ct"] for c in grp)
        print(f"{name}: tokens in {pt} out {ct}; avg/call in {pt / len(grp):.0f} out {ct / len(grp):.0f}; "
              f"haiku ${total(grp, lambda c: BASE):.4f} total, ${total(grp, lambda c: BASE) / len(grp):.5f}/call")
    haiku = total(calls, lambda c: BASE)
    per_email = haiku / n  # blended: alert-scoring spend attributed to the classified emails
    print(f"blended Haiku cost/email ${per_email:.5f}; per 1k ${per_email * 1000:.2f}")
    rate = n / days
    print(f"observed rate {rate:.0f} calls/day -> month(30d) {rate * 30:.0f} calls ${per_email * rate * 30:.2f}; "
          f"300/wk -> month ${per_email * 300 * 52 / 12:.2f}")
    backlog_days = (datetime(2026, 10, 20) - datetime(2026, 8, 3)).days
    print(f"backlog {backlog_days}d ESTIMATE: observed-rate ${per_email * rate * backlog_days:.2f} "
          f"({rate * backlog_days:.0f} emails); 300/wk ${per_email * 300 * backlog_days / 7:.2f} "
          f"({300 * backlog_days / 7:.0f} emails)")

    print("\n-- routing mix (heuristic_classify -> route_task, roster budget=mini mid=deepseek frontier=haiku)")
    for name, grp in (("classify", cls), ("alerts", alr)):
        mix = Counter(routed_model(c, i) for i, c in enumerate(grp))
        print(name, {k: f"{v / len(grp):.0%}" for k, v in mix.items()})
    print("heuristic (type,difficulty):", Counter((lambda d: (d.task_type, d.difficulty))(heuristic_classify(c["prompt"])) for c in cls))

    def pick(grp):
        return {id(c): routed_model(c, i) for i, c in enumerate(grp)}
    pm = {**pick(cls), **pick(alr)}
    routed = total(calls, lambda c: pm[id(c)])
    allmini = total(calls, lambda c: MINI)
    casc = cascade_total(calls)
    prot_routed = total(calls, lambda c: BASE if c.get("cat") in PROTECTED else pm[id(c)])
    print("\n-- projected, blended per email (classify + alerts), vs Haiku-for-all")
    for label, v in (("routed (as-is)", routed), ("routed + protected on Haiku", prot_routed),
                     ("all gpt-4o-mini (bound)", allmini), ("mini first, protected re-run on Haiku (cascade)", casc)):
        pe = v / n
        print(f"{label}: ${pe * 1000:.2f}/1k, savings {1 - v / haiku:.1%}, "
              f"${(per_email - pe) * rate * 30:.2f}/mo at observed rate, ${(per_email - pe) * 1300:.2f}/mo at 300/wk")
    prot_share = sum(c["cat"] in PROTECTED for c in cls) / n
    print(f"protected-category share of classifier calls {prot_share:.1%}")


if __name__ == "__main__":
    main(sys.argv[1])

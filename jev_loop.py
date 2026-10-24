"""Jev learning loop: can a teacher LLM's generic rulebook edits raise Jev's handled share? (offline replay)

Usage:  python3 jev_loop.py run    <real-traffic-dir> [--cap 1.00] [--rounds 8] [--teacher claude-haiku-4-5]   # live Jev + Haiku-teacher calls
        python3 jev_loop.py report <real-traffic-dir>                              # aggregates only, no calls

Jev (current rulebook) -> sure (p >= 0.9, not protected)? -> handled
                       -> else teacher (Haiku) sees disagreements -> proposes ONE generic edit
                       -> re-test on TRAIN -> gate -> keep or discard.
Chronological split: oldest ~2/3 TRAIN (learn + gate), newest ~1/3 HOLDOUT (scored, never used to accept/reject).
Haiku's recorded production label is the REFERENCE, not truth. Private artefacts (learned rulebooks, proposal
log) go ONLY to <dir>/loop/. The teacher is called through AnthropicAdapter directly: no record/replay .cache/.
"""
from __future__ import annotations

import json, os, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "src"))
from jev_calibration import COSTLY, HAIKU_1K, JEV_PER_TOK, split_prompt  # noqa: E402
from shadow_test import HAIKU, PROTECTED, cost, load  # noqa: E402

T = 0.9
TRAIN_FRAC = 2 / 3
BASE = Path(__file__).parent / "data" / "rulebooks" / "email_base.json"


class Stop(Exception):
    pass


def split_chrono(calls, frac=TRAIN_FRAC):
    """-> (train idxs, holdout idxs) over indices into `calls`, oldest `frac` first by `started`."""
    order = sorted(range(len(calls)), key=lambda i: (calls[i]["started"], i))
    k = round(len(order) * frac)
    return sorted(order[:k]), sorted(order[k:])


def metrics(rows):
    """rows: dicts {i, ref, choice, p, tok, hcost} (choice None = failed call -> escalated)."""
    n = len(rows)
    hi = [r for r in rows if r["choice"] and r["p"] >= T]
    handled = [r for r in hi if r["choice"] not in PROTECTED]
    hset = {r["i"] for r in handled}
    final_ok = sum((r["choice"] == r["ref"]) if r["i"] in hset else True for r in rows)
    cost_ = sum(r["tok"] * JEV_PER_TOK + (0 if r["i"] in hset else r["hcost"]) for r in rows)
    return dict(
        n=n, handled=len(handled) / n,
        agree_hi=(sum(r["choice"] == r["ref"] for r in hi) / len(hi)) if hi else 1.0, n_hi=len(hi),
        agree=sum(r["choice"] == r["ref"] for r in rows) / n,
        miss=sorted(r["i"] for r in hi if r["ref"] == "ACTION_NEEDED" and r["choice"] in COSTLY),
        cascade_agree=final_ok / n, per1k=cost_ / n * 1000)


def gate(base, new):
    """Accept iff (a) agreement among >=0.9 decisions doesn't drop, (b) handled share OR overall agreement rises,
    (c) no NEW costly misses. -> (accepted, reason)."""
    if new["agree_hi"] < base["agree_hi"] - 1e-9:
        return False, "agreement among sure decisions dropped"
    if set(new["miss"]) - set(base["miss"]):
        return False, "new costly miss"
    if not (new["handled"] > base["handled"] + 1e-9 or new["agree"] > base["agree"] + 1e-9):
        return False, "no gain in handled share or agreement"
    return True, "ok"


def select_examples(rows, rnd, k=6):
    """Costly misses first, then disagreements, then right-but-unsure; rotate by round so rounds see different cases."""
    miss = [r for r in rows if r["ref"] == "ACTION_NEEDED" and r["choice"] in COSTLY and r["p"] >= T]
    dis = [r for r in rows if r["choice"] != r["ref"] and r not in miss]
    unsure = [r for r in rows if r["choice"] == r["ref"] and (r["p"] < T or r["choice"] in PROTECTED)]
    rot = lambda xs, m: (xs[(rnd * m) % len(xs):] + xs[:(rnd * m) % len(xs)]) if xs else xs
    return (miss[:2] + rot(dis, 2)[:k - 2] + rot(unsure, 2))[:k]


def grams(text, n=6):
    w = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def leak(text, email_grams):
    """Reason string if a proposal could carry email content (address, link, id, 6-word overlap), else None."""
    if "@" in text or "http" in text.lower() or re.search(r"\b[0-9a-f]{8,}\b", text, re.I):
        return "contains address/link/id"
    if grams(text) & email_grams:
        return "copies 6+ words from an email"
    return None


def teacher_prompt(rb, examples, states, past):
    opts = "\n".join(f"- {k}: {v}" for k, v in rb.criteria.items())
    notes = "\n".join(f"- {n}" for n in rb.notes) or "(none)"
    ex = "\n\n".join(f"[{j}] reference={r['ref']} classifier={r['choice']} (p={r['p']:.2f})\n{states[r['i']][:700]}"
                     for j, r in enumerate(examples))
    avoid = "\n".join(f"- {p}" for p in past[-6:]) or "(none)"
    return (f"You maintain a rulebook that a small classifier uses to sort emails.\n\nCurrent categories:\n{opts}\n\n"
            f"Current clarifying notes:\n{notes}\n\nBelow are emails where the classifier was unsure or disagreed with "
            f"the reference label (the reference can occasionally be wrong).\n\n{ex}\n\n"
            f"Propose exactly ONE edit that helps the classifier on this kind of case: reword one category description, "
            f"or add one short clarifying note. The edit must be a GENERIC rule about types of email. Never copy "
            f"names, companies, email addresses, links or quoted text from the emails. Maximum 300 characters. "
            f"Do not repeat these already-rejected ideas:\n{avoid}\n\n"
            f'Reply with ONLY JSON: {{"edit":"criterion","option":"<CATEGORY>","text":"<new description>"}} '
            f'or {{"edit":"note","text":"<note>"}}')


def parse_proposal(text, rb):
    """-> (kind, option, text) or None."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0))
        t = d["text"].strip()
        if not t or len(t) > 300:
            return None
        if d["edit"] == "criterion" and d["option"] in rb.criteria:
            return "criterion", d["option"], t
        if d["edit"] == "note":
            return "note", None, t
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return None


def apply_proposal(rb, p):
    return rb.with_criterion(p[1], p[2]) if p[0] == "criterion" else rb.with_note(p[2])


def run_loop(calls, rb, evaluate, teach, rounds=8, patience=3, log=None):
    """evaluate(rulebook, idxs)->rows; teach(prompt)->text. Returns list of per-round aggregate dicts.
    Holdout never influences accept/reject."""
    train, hold = split_chrono(calls)
    states = {i: split_prompt(calls[i]["prompt"])[1] for i in range(len(calls))}
    email_grams = set().union(*(grams(s) for s in states.values()))
    log = log or (lambda rec: None)
    cur = evaluate(rb, train); cur_m = metrics(cur)
    out = [dict(round=0, version=rb.version, hash=rb.content_hash(), accepted=None, reason="baseline",
                train=cur_m, holdout=metrics(evaluate(rb, hold)))]
    past, rejects = [], 0
    for rnd in range(1, rounds + 1):
        ex = select_examples(cur, rnd)
        if not ex:
            break
        prop = parse_proposal(teach(teacher_prompt(rb, ex, states, past)), rb)
        reason = "unparseable proposal" if prop is None else leak(prop[2], email_grams)
        acc, cand, cand_rows = False, None, None
        if prop is not None and reason is None:
            cand = apply_proposal(rb, prop)
            cand_rows = evaluate(cand, train)
            acc, reason = gate(cur_m, metrics(cand_rows))
        if acc:
            rb, cur, cur_m, rejects = cand, cand_rows, metrics(cand_rows), 0
        else:
            rejects += 1
            if prop:
                past.append(prop[2])
        rec = dict(round=rnd, version=rb.version, hash=rb.content_hash(), accepted=acc, reason=reason,
                   kind=prop[0] if prop else None, train=cur_m, holdout=metrics(evaluate(rb, hold)),
                   candidate_train=metrics(cand_rows) if cand_rows else None)
        out.append(rec)
        log(dict(rec, proposal=prop, rulebook=rb.to_dict()))
        if rejects >= patience:
            break
    return out


# ---- live wiring -----------------------------------------------------------------------------------------

def _spend_path(d):
    return Path(d) / "loop" / "spend.json"


def live(d, cap, rounds, teacher_model=HAIKU):
    from router.adapters import _PROVIDER_BASE_URL
    from router.adapters.anthropic_adapter import AnthropicAdapter
    from router.adapters.openai_compatible import OpenAICompatibleAdapter
    from router.config import get_model
    from router.adapters.typesafe import JevClient
    from router.rulebook import Rulebook
    from router.secrets import force_mock, get_api_key
    if force_mock():
        sys.exit("AWR_FORCE_MOCK is set: refusing to run the loop on mock decisions")
    calls = load(d)
    states = [split_prompt(x["prompt"])[1] for x in calls]
    loop = Path(d) / "loop"; loop.mkdir(exist_ok=True)
    sp = _spend_path(d)
    spent = [json.loads(sp.read_text())["spent"] if sp.exists() else 0.0]
    jev, memo = JevClient(), {}
    prov = get_model(teacher_model).provider
    key = get_api_key(prov.upper() + "_API_KEY")
    teacher = (AnthropicAdapter(teacher_model, key) if prov == "anthropic" else
               OpenAICompatibleAdapter(teacher_model, key, _PROVIDER_BASE_URL[prov], provider=prov))
    soft = cap * 0.8

    def pay(x):
        spent[0] += x; sp.write_text(json.dumps({"spent": spent[0]}))
        if spent[0] > soft:
            raise Stop(f"spend ${spent[0]:.4f} nears cap ${cap}")

    def evaluate(rb, idxs):
        rows = []
        for i in idxs:
            k = (rb.content_hash(), i)
            if k not in memo:
                if spent[0] + 0.0001 > soft:
                    raise Stop("spend would exceed cap")
                dec = jev.decide(states[i], rb.render_instructions(), rb.criteria)
                pay(dec.cost_usd)
                memo[k] = dict(i=i, ref=calls[i]["cat"], choice=dec.choice, p=dec.top_prob,
                               tok=dec.input_tokens, hcost=cost(HAIKU, calls[i]["pt"], calls[i]["ct"]))
            rows.append(memo[k])
        return rows

    def teach(prompt):
        r = teacher.complete(prompt, max_tokens=400)
        if not r.success:
            raise Stop(f"teacher call failed ({r.error or 'provider error, e.g. no credit'})")
        pay(cost(teacher_model, r.input_tokens, r.output_tokens))
        return r.text

    def log(rec):
        rb = rec.pop("rulebook")
        Rulebook(rb["version"], rb["instructions"], rb["criteria"], rb["notes"]).save(loop / f"rulebook_v{rb['version']}.json")
        with open(loop / "proposals.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")

    rb0 = Rulebook.load(BASE)
    try:
        res = run_loop(calls, rb0, evaluate, teach, rounds, log=log)
    except Stop as e:
        print("STOPPED:", e); return
    (loop / "rounds.json").write_text(json.dumps(dict(rounds=res, spent=spent[0], n=len(calls)), indent=1))
    print(f"done; spent ${spent[0]:.5f}")


def report(d):
    r = json.loads((Path(d) / "loop" / "rounds.json").read_text())
    train, hold = split_chrono(load(d))
    print(f"n={r['n']}: TRAIN {len(train)} (oldest), HOLDOUT {len(hold)} (newest); spend ${r['spent']:.5f}")
    print("| round | v | accepted | why | TRAIN handled/agree/miss | HOLDOUT handled/agree/miss |\n|---|---|---|---|---|---|")
    f = lambda m: f"{m['handled']*100:.0f}% / {m['agree']*100:.0f}% / {len(m['miss'])}"
    for x in r["rounds"]:
        print(f"| {x['round']} | {x['version']} | {x['accepted']} | {x['reason']} | {f(x['train'])} | {f(x['holdout'])} |")
    print("\nHOLDOUT handled share by round (each # = 2%):")
    for x in r["rounds"]:
        h = x["holdout"]["handled"]; print(f" r{x['round']} {'#' * round(h * 50):<30} {h*100:.0f}%")
    b, a = r["rounds"][0]["holdout"], r["rounds"][-1]["holdout"]
    print(f"\nHOLDOUT cascade before -> after: handled {b['handled']*100:.0f}% -> {a['handled']*100:.0f}%, "
          f"agreement {b['agree']*100:.1f}% -> {a['agree']*100:.1f}%, final agreement {b['cascade_agree']*100:.1f}% -> "
          f"{a['cascade_agree']*100:.1f}%, costly misses {len(b['miss'])} -> {len(a['miss'])}, "
          f"cost/1k ${b['per1k']:.3f} -> ${a['per1k']:.3f} (Haiku-only ${HAIKU_1K}/1k)")


if __name__ == "__main__":
    a = sys.argv
    if len(a) < 3 or a[1] not in ("run", "report"):
        sys.exit(__doc__)
    if a[1] == "run":
        live(a[2], float(a[a.index("--cap") + 1]) if "--cap" in a else 1.0,
             int(a[a.index("--rounds") + 1]) if "--rounds" in a else 8,
             a[a.index("--teacher") + 1] if "--teacher" in a else HAIKU)
    else:
        report(a[2])

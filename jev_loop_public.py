"""Jev calibration + self-improving rulebook loop on a PUBLIC dataset with TRUE labels (Banking77, CC-BY-4.0).

Usage:  python3 jev_loop_public.py calibrate [--cap 0.50]        # Jev on the fixed 800-query TEST sample (live)
        python3 jev_loop_public.py mini [--n 300] [--cap 0.50]   # gpt-4o-mini self-reported confidence, first n of that sample
        python3 jev_loop_public.py run [--teacher manual|claude-haiku-4-5] [--rounds 8] [--cap 0.50]
        python3 jev_loop_public.py report

Splits (seed 77, fixed): LEARN 300 + VAL 300 from official train.csv (the teacher sees only LEARN mistakes; the
gate and final version selection use VAL), TEST 800 from official test.csv (first 300 scored every round; all
800 for calibration). Each round is gated on VAL; the final version is the best VAL score.
`--teacher manual`: loop writes loop/teacher_prompt_N.txt and stops; write loop/teacher_reply_N.txt, re-run
(Jev decisions are memoised in loop/jev_memo.json, so a re-run replays for free). Everything here is public data.
"""
from __future__ import annotations

import csv, json, random, re, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
import jev_loop as L  # noqa: E402  (reuse gate / select_examples / leak / parse_proposal / apply_proposal)
from jev_calibration import BINS, JEV_PER_TOK, auroc, calibration  # noqa: E402

DATA = ROOT / "data" / "public" / "banking77"
BASE = ROOT / "data" / "rulebooks" / "banking77_base.json"
LOOP = DATA / "loop"
T = 0.9
SEED, N_LEARN, N_VAL, N_TEST, N_TEST_ROUND = 77, 300, 300, 800, 300


def load_csv(name):
    return [(r["text"], r["category"]) for r in csv.DictReader(open(DATA / name, encoding="utf-8"))]


def splits(train, test, seed=SEED):
    """-> (items {id: (text, label)}, learn ids, val ids, test ids). Deterministic in `seed`."""
    rng = random.Random(seed)
    tr = rng.sample(range(len(train)), N_LEARN + N_VAL)
    te = rng.sample(range(len(test)), N_TEST)
    items = {f"train:{i}": train[i] for i in tr} | {f"test:{i}": test[i] for i in te}
    return items, [f"train:{i}" for i in tr[:N_LEARN]], [f"train:{i}" for i in tr[N_LEARN:]], [f"test:{i}" for i in te]


def metrics(rows):
    """rows: {i, ref (true label), choice, p, tok}. handled = p >= T (no protected classes here)."""
    n = len(rows)
    hi = [r for r in rows if r["choice"] and r["p"] >= T]
    ok_hi = sum(r["choice"] == r["ref"] for r in hi)
    return dict(n=n, handled=len(hi) / n, agree_hi=ok_hi / len(hi) if hi else 1.0, n_hi=len(hi),
                agree=sum(r["choice"] == r["ref"] for r in rows) / n, miss=[],
                score=ok_hi / n, per1k=sum(r["tok"] for r in rows) / n * JEV_PER_TOK * 1000)


def teacher_prompt(rb, examples, items, past):
    opts = "\n".join(f"- {k}: {v}" for k, v in rb.criteria.items())
    notes = "\n".join(f"- {n}" for n in rb.notes) or "(none)"
    ex = "\n".join(f'[{j}] "{items[r["i"]][0]}" true={r["ref"]} classifier={r["choice"]} (p={r["p"]:.2f})'
                   for j, r in enumerate(examples))
    avoid = "\n".join(f"- {p}" for p in past[-6:]) or "(none)"
    return (f"You maintain a rulebook that a small classifier uses to sort customer banking queries into intents.\n\n"
            f"Current intents:\n{opts}\n\nCurrent clarifying notes:\n{notes}\n\nBelow are queries where the classifier "
            f"was wrong or unsure. The true label is ground truth.\n\n{ex}\n\n"
            f"Propose exactly ONE edit that helps on this kind of case: reword one intent description (sharpen the "
            f"boundary with the intent it is confused with), or add one short clarifying note. It must be a GENERIC "
            f"rule, not a copy of any query. Maximum 300 characters. Do not repeat these rejected ideas:\n{avoid}\n\n"
            f'Reply with ONLY JSON: {{"edit":"criterion","option":"<INTENT>","text":"<new description>"}} '
            f'or {{"edit":"note","text":"<note>"}}')


def select_final(rounds):
    """Best VAL score (ties -> overall VAL accuracy -> earlier round)."""
    return max(rounds, key=lambda r: (r["val"]["score"], r["val"]["agree"], -r["round"]))


def run_loop(items, learn, val, test, rb, evaluate, teach, rounds=8, patience=3, log=None):
    """evaluate(rb, ids)->rows; teach(prompt, rnd)->text. The gate sees only VAL; TEST never influences anything."""
    log = log or (lambda rec: None)
    eg = set().union(*(L.grams(t) for t, _ in items.values()))
    cur_tr = evaluate(rb, learn); cur_val = metrics(evaluate(rb, val)); cur_te = metrics(evaluate(rb, test[:N_TEST_ROUND]))
    out = [dict(round=0, version=rb.version, hash=rb.content_hash(), accepted=None, reason="baseline",
                learn=metrics(cur_tr), val=cur_val, test=cur_te)]
    past, rejects = [], 0
    for rnd in range(1, rounds + 1):
        ex = L.select_examples(cur_tr, rnd)
        if not ex:
            break
        prop = L.parse_proposal(teach(teacher_prompt(rb, ex, items, past), rnd), rb)
        reason = "unparseable proposal" if prop is None else L.leak(prop[2], eg)
        acc, cand, cand_val = False, None, None
        if prop is not None and reason is None:
            cand = L.apply_proposal(rb, prop)
            cand_val = metrics(evaluate(cand, val))
            acc, reason = L.gate(cur_val, cand_val)
        if acc:
            rb, cur_val, rejects = cand, cand_val, 0
            cur_tr = evaluate(rb, learn); cur_te = metrics(evaluate(rb, test[:N_TEST_ROUND]))
        else:
            rejects += 1
            if prop:
                past.append(prop[2])
        rec = dict(round=rnd, version=rb.version, hash=rb.content_hash(), accepted=acc, reason=reason,
                   kind=prop[0] if prop else None, learn=metrics(cur_tr), val=cur_val, test=cur_te,
                   candidate_val=cand_val)
        out.append(rec)
        log(dict(rec, proposal=prop, rulebook=rb.to_dict()))
        if rejects >= patience:
            break
    return out


# ---- live wiring -----------------------------------------------------------------------------------------

class Env:
    """Shared by every subcommand: data, spend ledger, memoised Jev decisions."""

    def __init__(self, cap):
        from router.adapters.typesafe import JevClient
        from router.rulebook import Rulebook
        from router.secrets import force_mock
        if force_mock():
            sys.exit("AWR_FORCE_MOCK is set: refusing to run on mock decisions")
        LOOP.mkdir(exist_ok=True)
        self.items, self.learn, self.val, self.test = splits(load_csv("train.csv"), load_csv("test.csv"))
        self.rb0 = Rulebook.load(BASE)
        self.jev, self.cap, self.soft = JevClient(), cap, cap * 0.8
        self.sp, self.mp = LOOP / "spend.json", LOOP / "jev_memo.json"
        self.spent = json.loads(self.sp.read_text())["spent"] if self.sp.exists() else 0.0
        self.memo = ({tuple(json.loads(k)): v for k, v in json.loads(self.mp.read_text()).items()}
                     if self.mp.exists() else {})

    def pay(self, x):
        self.spent += x; self.sp.write_text(json.dumps({"spent": self.spent}))

    def evaluate(self, rb, ids):
        h, ins = rb.content_hash(), rb.render_instructions()
        todo = [i for i in ids if (h, i) not in self.memo]
        if self.spent + len(todo) * 0.0001 > self.soft:
            raise L.Stop(f"spend ${self.spent:.4f} + {len(todo)} calls would pass 80% of cap ${self.cap}")

        def one(i):
            for _ in range(2):
                d = self.jev.decide(self.items[i][0], ins, rb.criteria)
                if d.success:
                    break
            return i, d
        with ThreadPoolExecutor(8) as ex:
            for i, d in ex.map(one, todo):
                self.pay(d.cost_usd)
                self.memo[(h, i)] = dict(i=i, ref=self.items[i][1], choice=d.choice, p=d.top_prob, tok=d.input_tokens)
        if todo:
            self.mp.write_text(json.dumps({json.dumps(list(k)): v for k, v in self.memo.items()}))
        return [self.memo[(h, i)] for i in ids]


def cmd_run(cap, rounds, teacher_model):
    from router.rulebook import Rulebook
    e = Env(cap)
    manual = teacher_model == "manual"
    if not manual:  # API teacher: supported, not used by default
        from router.adapters import _PROVIDER_BASE_URL
        from router.adapters.anthropic_adapter import AnthropicAdapter
        from router.adapters.openai_compatible import OpenAICompatibleAdapter
        from router.config import get_model
        from router.secrets import get_api_key
        from shadow_test import cost
        prov = get_model(teacher_model).provider
        key = get_api_key(prov.upper() + "_API_KEY")
        api = (AnthropicAdapter(teacher_model, key) if prov == "anthropic" else
               OpenAICompatibleAdapter(teacher_model, key, _PROVIDER_BASE_URL[prov], provider=prov))

    def teach(prompt, rnd):
        if manual:
            reply = LOOP / f"teacher_reply_{rnd}.txt"
            if reply.exists():
                return reply.read_text()
            (LOOP / f"teacher_prompt_{rnd}.txt").write_text(prompt)
            raise L.Stop(f"awaiting {reply.name} (prompt written next to it); re-run after writing it")
        r = api.complete(prompt, max_tokens=400)
        if not r.success:
            raise L.Stop(f"teacher call failed ({r.error or 'provider error'})")
        e.pay(cost(teacher_model, r.input_tokens, r.output_tokens))
        return r.text

    def log(rec):
        rb = rec.pop("rulebook")
        Rulebook(rb["version"], rb["instructions"], rb["criteria"], rb["notes"]).save(LOOP / f"rulebook_v{rb['version']}.json")
        with open(LOOP / "proposals.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")

    # replay is deterministic, so rebuild the log from scratch on every (re)start
    (LOOP / "proposals.jsonl").unlink(missing_ok=True)
    stopped = None
    try:
        res = run_loop(e.items, e.learn, e.val, e.test, e.rb0, e.evaluate, teach, rounds, log=log)
    except L.Stop as s:
        stopped = str(s)
        print("STOPPED:", s)
        return
    finally:
        (LOOP / "state.json").write_text(json.dumps(dict(stopped=stopped, spent=e.spent)))
    (LOOP / "rounds.json").write_text(json.dumps(dict(rounds=res, spent=e.spent), indent=1))
    print(f"done; spent ${e.spent:.5f}")


def cmd_calibrate(cap):
    e = Env(cap)
    rows = e.evaluate(e.rb0, e.test)
    pairs = [(r["p"], int(r["choice"] == r["ref"])) for r in rows if r["choice"]]
    out = calib_summary(pairs, len(rows))
    out["spent"] = e.spent
    (LOOP / "calibration_jev.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


def calib_summary(pairs, n):
    t, ece = calibration(pairs)
    hi = [(p, c) for p, c in pairs if p >= T]
    return dict(n=n, answered=len(pairs), accuracy=sum(c for _, c in pairs) / len(pairs), ece=ece, auroc=auroc(pairs),
                acc_at_090=sum(c for _, c in hi) / len(hi) if hi else None, n_at_090=len(hi), coverage_090=len(hi) / n,
                bins=[dict(lo=lo, hi=hi_, n=k, mean_p=mp, acc=acc) for lo, hi_, k, mp, acc in t])


MINI = "gpt-4o-mini"


def cmd_mini(cap, n):
    from router.adapters import _PROVIDER_BASE_URL
    from router.adapters.openai_compatible import OpenAICompatibleAdapter
    from router.config import get_model
    from router.secrets import get_api_key
    from shadow_test import cost
    e = Env(cap)
    prov = get_model(MINI).provider
    api = OpenAICompatibleAdapter(MINI, get_api_key(prov.upper() + "_API_KEY"), _PROVIDER_BASE_URL[prov], provider=prov)
    path = LOOP / "mini_answers.json"
    got = json.loads(path.read_text()) if path.exists() else {}
    ids = e.test[:n]

    def one(i):
        r = api.complete(e.rb0.render_prompt(e.items[i][0]).replace(
            "Reply with ONLY the option name,", 'Reply with ONLY JSON {"category":"<option>","confidence":<0-100>}, category being'), max_tokens=60)
        return i, r
    todo = [i for i in ids if i not in got]
    with ThreadPoolExecutor(8) as ex:
        for i, r in ex.map(one, todo):
            if e.spent + 0.0005 > e.soft:
                raise L.Stop("spend would exceed cap")
            got[i] = dict(text=r.text if r.success else "", cost=cost(MINI, r.input_tokens, r.output_tokens))
            e.pay(got[i]["cost"])
    path.write_text(json.dumps(got))
    pairs = []
    for i in ids:
        cat, conf = parse_mini(got[i]["text"], e.rb0.criteria)
        if cat is not None:
            pairs.append((conf / 100, int(cat == e.items[i][1])))
    out = calib_summary(pairs, len(ids))
    jev = [r for r in e.evaluate(e.rb0, ids)]
    out["jev_same_subset"] = calib_summary([(r["p"], int(r["choice"] == r["ref"])) for r in jev if r["choice"]], len(ids))
    out["spent"] = e.spent
    (LOOP / "calibration_mini.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "bins"}, indent=1))


def parse_mini(text, criteria):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0))
        cat, conf = d["category"], float(d["confidence"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None, None
    return (cat, conf) if cat in criteria else (None, None)


def report():
    r = json.loads((LOOP / "rounds.json").read_text()) if (LOOP / "rounds.json").exists() else None
    if r is None:
        sys.exit("no rounds.json yet (loop still waiting on the teacher or not finished)")
    f = lambda m: f"{m['handled']*100:.0f}% / {m['agree_hi']*100:.0f}% / {m['agree']*100:.0f}%"
    print("| round | accepted | why | LEARN | VAL | TEST300 |\n|---|---|---|---|---|---|")
    for x in r["rounds"]:
        print(f"| {x['round']} | {x['accepted']} | {x['reason']} | {f(x['learn'])} | {f(x['val'])} | {f(x['test'])} |")
    s = select_final(r["rounds"])
    print(f"\n(cells: handled / accuracy at p>=0.9 / overall accuracy). Selected by best VAL score: round {s['round']} "
          f"(v{s['version']}); TEST300 {f(s['test'])}; spend ${r['spent']:.4f}")


if __name__ == "__main__":
    a = sys.argv
    opt = lambda k, d, t=str: t(a[a.index(k) + 1]) if k in a else d
    cmd = a[1] if len(a) > 1 else ""
    try:
        if cmd == "calibrate":
            cmd_calibrate(opt("--cap", 0.5, float))
        elif cmd == "mini":
            cmd_mini(opt("--cap", 0.5, float), opt("--n", 300, int))
        elif cmd == "run":
            cmd_run(opt("--cap", 0.5, float), opt("--rounds", 8, int), opt("--teacher", "manual"))
        elif cmd == "report":
            report()
        else:
            sys.exit(__doc__)
    except L.Stop as s:
        print("STOPPED:", s)

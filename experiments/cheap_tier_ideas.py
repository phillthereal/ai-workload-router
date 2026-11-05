#!/usr/bin/env python3
"""Free what-ifs on the saved answers from cheap_tier_test.py: no provider calls."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "experiments")]

from analyze import bootstrap, net_reduction, retention  # noqa: E402
from cheap_tier_test import BENCH, CLASSIFIER_USD, DEEPSEEK, HAIKU, MINI, OPUS, OUT, SETS, SONNET  # noqa: E402
from router.scoring import committed_answer, final_answer  # noqa: E402

REF = {t["id"]: t["reference"] for f in SETS.values() for t in json.loads((BENCH / f).read_text())["tasks"]}


def same(x, y):
    return final_answer(x["text"], committed_answer(y["text"])) == 1.0


def words(a):
    return re.findall(r"\w+", committed_answer(a["text"]).lower())


def overlap(x, y):
    """Longest run of words both answers share."""
    a, b, best = words(x), words(y), []
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            if k > len(best):
                best = a[i:i + k]
    return " ".join(best)


def graded(row, text):
    return final_answer(f"Answer: {text}", REF[row["id"]])


def agree_else(keep, fallback):
    def arm(r):
        a = r["answers"]; h, d = a[HAIKU], a[DEEPSEEK]; f = a[fallback]
        ok = same(h, d)
        return ((a[keep] if ok else f)["score"], h["usd"] + d["usd"] + (0 if ok else f["usd"]), ok)
    return arm


def two_step(r):
    """Disagreement goes to Sonnet; Sonnet siding with either keeps its answer, otherwise Opus."""
    a = r["answers"]; h, d, s, o = a[HAIKU], a[DEEPSEEK], a[SONNET], a[OPUS]
    base = h["usd"] + d["usd"]
    if same(h, d):
        return d["score"], base, True
    if same(s, h) or same(s, d):
        return s["score"], base + s["usd"], False
    return o["score"], base + s["usd"] + o["usd"], False


def all_three(r):
    a = r["answers"]; h, d, m, s = a[HAIKU], a[DEEPSEEK], a[MINI], a[SONNET]
    ok = same(h, d) and same(m, d)
    return ((d if ok else s)["score"], h["usd"] + d["usd"] + m["usd"] + (0 if ok else s["usd"]), ok)


def any_two(r):
    a = r["answers"]; h, d, m, s = a[HAIKU], a[DEEPSEEK], a[MINI], a[SONNET]
    pick = d if same(h, d) or same(m, d) else h if same(h, m) else None
    return ((pick or s)["score"], h["usd"] + d["usd"] + m["usd"] + (0 if pick else s["usd"]), pick is not None)


def contains(r):
    """One answer inside the other counts as agreement, and the shorter one is kept."""
    a = r["answers"]; h, d, s = a[HAIKU], a[DEEPSEEK], a[SONNET]
    x, y = (" ".join(words(h)), " ".join(words(d)))
    short, long = sorted((x, y), key=len)
    ok = bool(short) and f" {short} " in f" {long} "
    return (graded(r, short) if ok else s["score"], h["usd"] + d["usd"] + (0 if ok else s["usd"]), ok)


def overlap_arm(r):
    a = r["answers"]; h, d, s = a[HAIKU], a[DEEPSEEK], a[SONNET]
    o = overlap(h, d)
    return (graded(r, o) if o else s["score"], h["usd"] + d["usd"] + (0 if o else s["usd"]), bool(o))


ARMS = {
    "H+D agree, keep DeepSeek, else Sonnet": agree_else(DEEPSEEK, SONNET),
    "H+D agree, keep Haiku, else Sonnet": agree_else(HAIKU, SONNET),
    "H+D agree, keep Haiku, else Opus": agree_else(HAIKU, OPUS),
    "H+D agree, else Sonnet, else Opus": two_step,
    "all 3 cheap agree, else Sonnet": all_three,
    "any 2 of 3 cheap agree, else Sonnet": any_two,
    "contained answers count, else Sonnet": contains,
    "shared words of H+D, else Sonnet": overlap_arm,
}


def table(rows, label):
    print(f"\n{label}: {len(rows)} tasks\n| Idea | Saved % 99% range | Quality % 99% range | Kept |\n|---|---|---|---|")
    for name, arm in ARMS.items():
        res = [arm(r) for r in rows]
        p = [(r["answers"][OPUS]["usd"], u + CLASSIFIER_USD, r["answers"][OPUS]["score"], s) for r, (s, u, _) in zip(rows, res)]
        c, q = bootstrap(p, net_reduction, alpha=.01), bootstrap(p, retention, alpha=.01)
        print(f"| {name} | {c[0]:.1f} [{c[1]:.1f}, {c[2]:.1f}] | {q[0]:.1f} [{q[1]:.1f}, {q[2]:.1f}] | {sum(k for *_, k in res)} |")


if __name__ == "__main__":
    rows = json.loads(OUT.read_text())
    new = [r for r in rows if r["set"] == "new tasks"]
    table([r for r in new if r["type"] == "reasoning"], "reasoning")
    table([r for r in new if r["type"] == "extraction"], "extraction")
    table([r for r in rows if r["set"] == "hard set"], "hard set")

#!/usr/bin/env python3
"""Free test: can word-overlap nearest neighbours replace the paid classifier? Each task set is
predicted from the other sets only. Standard library, no model calls."""
import collections, json, math, re
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
FILES = ("tasks_public.json", "tasks_public_fresh.json", "tasks_public_fresh2.json", "tasks_public_fresh3.json")
sets = {f: json.loads((ROOT / "data/public/benchmark" / f).read_text())["tasks"] for f in FILES}
def bag(p): return collections.Counter(re.findall(r"[a-z]+", p.lower()))
def vec(c, idf):
    v = {w: (1 + math.log(n)) * idf.get(w, 0) for w, n in c.items()}
    s = math.sqrt(sum(x * x for x in v.values())) or 1
    return {w: x / s for w, x in v.items()}
for strip in (False, True):
    # strip=True removes the benchmark's fixed instructions, closer to what real traffic looks like
    clean = (lambda p: re.sub(r"End your reply.*|Give the answer.*|Using only this passage.*?\n|Answer:.*", " ", p, flags=re.S)) if strip else (lambda p: p)
    right = total = 0; miss = collections.Counter()
    for held in FILES:
        train = [(bag(clean(t["prompt"])), t["task_type"]) for f in FILES if f != held for t in sets[f]]
        df = collections.Counter(w for b, _ in train for w in b)
        idf = {w: math.log(len(train) / n) for w, n in df.items()}
        tv = [(vec(b, idf), y) for b, y in train]
        inv = collections.defaultdict(list)
        for i, (v, _) in enumerate(tv):
            for w, x in v.items():
                inv[w].append((i, x))
        for t in sets[held]:
            sc = collections.Counter()
            for w, x in vec(bag(clean(t["prompt"])), idf).items():
                for i, y in inv.get(w, ()):
                    sc[i] += x * y
            vote = collections.Counter(tv[i][1] for i, _ in sc.most_common(7)).most_common(1)
            guess = vote[0][0] if vote else "none"
            total += 1; right += guess == t["task_type"]
            if guess != t["task_type"]: miss[(t["task_type"], guess)] += 1
    print("instructions removed" if strip else "full prompts", f"{right}/{total} = {right / total:.1%}", miss.most_common(4))

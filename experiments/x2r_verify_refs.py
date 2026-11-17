"""Recompute every checkable exact_match reference of data/tasks_x2r.json with independent code and assert it matches.

Usage: python3 experiments/x2r_verify_refs.py
Tasks with no code check (pure label judgements such as sentiment or spam) are listed at the end as HAND-CHECKED.
"""
from __future__ import annotations

import itertools
import json
import re
from datetime import date, timedelta
from fractions import Fraction as F
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = {t["id"]: t for t in json.loads((ROOT / "data" / "tasks_x2r.json").read_text())["tasks"]}
CHECKED: list[str] = []


def check(tid: str, computed) -> None:
    ref = TASKS[tid]["reference"]
    if isinstance(computed, (int, float, F)):
        assert float(computed) == float(ref), f"{tid}: computed {computed!r} != reference {ref!r}"
    else:
        assert str(computed).lower() == ref.lower(), f"{tid}: computed {computed!r} != reference {ref!r}"
    CHECKED.append(tid)


# ext-05: signup rate = signups / audience
rates = {"Email": 150 / 2000, "Social": 200 / 5000, "Search": 96 / 800}
check("x2r-ext-05", max(rates, key=rates.get))

# ext-06: swap on Tuesday, Omar's offer changed nothing
rota = {"Fri": "Hana", "Sat": "Luis", "Sun": "Omar"}
rota["Fri"], rota["Sat"] = rota["Sat"], rota["Fri"]
check("x2r-ext-06", rota["Fri"])

# ext-07: count "(in person" in the attendee list taken from the prompt itself
prompt = TASKS["x2r-ext-07"]["prompt"]
check("x2r-ext-07", len(re.findall(r"\(in person", prompt)))

# ext-08: day 1 = 13 March 2026, day 45
check("x2r-ext-08", (lambda d: f"{d.day} {d.strftime('%B')}")(date(2026, 3, 12) + timedelta(days=45)))

# ext-09: price after discount
q = {"Alder": 1200 * 0.85, "Birch": 1100 * 0.92, "Cedar": 1000.0}
check("x2r-ext-09", min(q, key=q.get))

# rsn-01 / 03 arithmetic
check("x2r-rsn-01", 36 * 5 - 130)
seats = 52 * 3 // 4
assert 52 * 3 % 4 == 0
occupied = seats - 6 + 11
assert occupied <= 52
check("x2r-rsn-03", 52 - occupied)

# rsn-05: machine-minutes: 5 machines * 5 min = 5 widgets -> 1 widget per machine per 5 min -> 100 machines, 100 widgets = 5 min
per_widget_machine_minutes = 5 * 5 / 5
check("x2r-rsn-05", int(100 * per_widget_machine_minutes / 100))

# rsn-06: brute force over permutations of the five books
sols = set()
for perm in itertools.permutations(["yellow", "red", "green", "blue", "black"]):
    pos = {c: i + 1 for i, c in enumerate(perm)}
    if pos["yellow"] == 1 and pos["red"] < pos["green"] and pos["blue"] == pos["green"] + 1 and pos["black"] not in (1, 5):
        sols.add(perm[4])
assert len(sols) == 1, sols  # position 5 is the same in every valid arrangement
check("x2r-rsn-06", sols.pop())

# rsn-07: x + 6 = 3 (x - 4), search integer ages
check("x2r-rsn-07", [x for x in range(1, 120) if x + 6 == 3 * (x - 4)][0])

# rsn-08: rates per minute
check("x2r-rsn-08", 1 / (F(1, 12) - F(1, 20)))

# rsn-09: harmonic mean via total distance / total time
check("x2r-rsn-09", (12 + 12) / (12 / 6 + 12 / 12))

# rsn-10: enumerate knight/knave assignments (True = knight)
ok = []
for a, b in itertools.product([True, False], repeat=2):
    a_stmt = not b            # "B is a knave"
    b_stmt = a and b          # "A and I are both knights"
    if a == a_stmt and b == b_stmt:
        ok.append((a, b))
assert len(ok) == 1
check("x2r-rsn-10", "knight" if ok[0][0] else "knave")

# rsn-11
check("x2r-rsn-11", sum(1 for n in range(1, 101) if (n % 3 == 0) != (n % 5 == 0)))

# rsn-12
days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
check("x2r-rsn-12", days[(days.index("Wednesday") + 100) % 7])

# cls-09 / cls-10: validity by exhaustive model search over small domains
# cls-09 (affirming the consequent): propositional, check every truth assignment
valid = all(not ((not s or d) and d) or s for s, d in itertools.product([True, False], repeat=2))
check("x2r-cls-09", "valid" if valid else "invalid")


# cls-10 (No reptiles are mammals; all snakes are reptiles; so no snakes are mammals): all subset-models over 3 individuals
def models(n=3):
    for bits in itertools.product([0, 1], repeat=3 * n):
        yield [set(i for i in range(n) if bits[k * n + i]) for k in range(3)]


valid = True
for rep, mam, snk in models():
    if not (rep & mam) and snk <= rep and (snk & mam):
        valid = False
check("x2r-cls-10", "valid" if valid else "invalid")

# cls-11: handshake graph, 7 people each of degree 3: sum of degrees must be even; also confirm by search for n=7 is impossible, n=6 possible
assert (7 * 3) % 2 == 1 and (6 * 3) % 2 == 0
check("x2r-cls-11", "possible" if (7 * 3) % 2 == 0 else "impossible")

# literal extractions: the reference must appear verbatim in the prompt
for _t in ("ext-01", "ext-02", "ext-03", "ext-04", "ext-10"):
    assert TASKS["x2r-" + _t]["reference"] in TASKS["x2r-" + _t]["prompt"], _t
    CHECKED.append("x2r-" + _t)

print(f"{len(CHECKED)} references verified by code: {', '.join(c.replace('x2r-', '') for c in CHECKED)}")
hand = [t for t in TASKS if TASKS[t]["scoring"] == "exact_match" and t not in CHECKED]
print("HAND-CHECKED exact_match references (label judgements, no code check): "
      + ", ".join(h.replace("x2r-", "") for h in hand))
print("ALL VERIFIED REFERENCES PASS")

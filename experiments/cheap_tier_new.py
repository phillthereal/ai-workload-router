#!/usr/bin/env python3
"""Tries the framework-level ideas on the new tasks: trimmed passages, answer-only replies,
arithmetic expressions, and free local models. Saves to data/cheap_tier_new.json.

    python3 experiments/cheap_tier_new.py paid --max-spend 0.12
    python3 experiments/cheap_tier_new.py local
"""
import ast
import json
import operator
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "experiments")]

from cheap_tier_test import BENCH, DEEPSEEK, EFFORT, HAIKU, OPUS, OUT, SETS, SONNET  # noqa: E402
from router.adapters import SpendMeter, get_adapter  # noqa: E402
from router.config import get_model  # noqa: E402
from router.scoring import committed_answer, final_answer  # noqa: E402

NEW = ROOT / "data" / "cheap_tier_new.json"
LOCAL = ("qwen2.5:7b", "qwen3.5:9b")
STOP = set("a an and are as at be by did do does for from had has have how in is it its of on or that the "
           "their this to was were what when where which who why will with".split())
ANSWER_ONLY = "\nReply with only that final line, with no explanation."
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv}


def tasks():
    by_id = {t["id"]: t for f in SETS.values() for t in json.loads((BENCH / f).read_text())["tasks"]}
    return json.loads(OUT.read_text()), by_id


def trim(prompt, keep=2):
    """Keep only the passage sentences that share the most words with the question."""
    m = re.search(r"Passage:\s*(.*?)\n\s*\n?Question:\s*(.*?)\n", prompt, re.S)
    if not m:
        return prompt
    passage, question = m.groups()
    q = set(re.findall(r"\w+", question.lower())) - STOP
    sents = re.split(r"(?<=[.!?])\s+", passage)
    ranked = sorted(range(len(sents)), key=lambda i: -len(q & set(re.findall(r"\w+", sents[i].lower()))))
    return prompt.replace(passage, " ".join(sents[i] for i in sorted(ranked[:keep])))


def calc(node):
    """Evaluate plain arithmetic only: nothing the model writes is ever run as code."""
    if isinstance(node, ast.Expression):
        return calc(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        return OPS[type(node.op)](calc(node.left), calc(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -calc(node.operand)
    raise ValueError("not arithmetic")


def paid_call(model, prompt, ref):
    r = get_adapter(model, False).complete(prompt, EFFORT.get(model))
    return {"text": r.text, "in": r.input_tokens, "out": r.output_tokens, "answered": r.success,
            "usd": get_model(model).cost_for_tokens(r.input_tokens, r.output_tokens),
            "score": final_answer(r.text, ref)}


def same(x, y):
    return final_answer(x["text"], committed_answer(y["text"])) == 1.0


def paid(cap):
    rows, by_id = tasks()
    new = [r for r in rows if r["set"] == "new tasks"]
    ext = [r for r in new if r["type"] == "extraction"]
    gsm = [r for r in new if r["id"].startswith("gsm")]
    out = {}
    with ThreadPoolExecutor(4) as pool:
        for name, change in (("trimmed", lambda p: trim(p)), ("answer only", lambda p: p + ANSWER_ONLY),
                             ("trimmed answer only", lambda p: trim(p) + ANSWER_ONLY)):
            res = {}
            for r in ext:
                if SpendMeter.usd >= cap:
                    print("Stopped at the cap", file=sys.stderr)
                    break
                t = by_id[r["id"]]
                pr = change(t["prompt"])
                h, d = pool.map(lambda m: paid_call(m, pr, t["reference"]), (HAIKU, DEEPSEEK))
                res[r["id"]] = {HAIKU: h, DEEPSEEK: d}
                if not same(h, d):  # Sonnet is only needed where the cheap pair disagrees
                    res[r["id"]][SONNET] = paid_call(SONNET, pr, t["reference"])
            out[name] = res
        sample = ext[::3]  # 28 tasks: enough to see whether Opus keeps its quality on a trimmed passage
        out["opus trimmed"] = {r["id"]: paid_call(OPUS, trim(by_id[r["id"]]["prompt"]), by_id[r["id"]]["reference"])
                               for r in sample if SpendMeter.usd < cap}
        expr = {}
        for r in gsm:
            t = by_id[r["id"]]
            p = t["prompt"].split("\n\nGive the answer")[0] + (
                "\n\nDo not solve it in words. Reply with one line in exactly this form:\nExpression: <one arithmetic expression that gives the answer>")
            a = paid_call(HAIKU, p, t["reference"])
            m = re.search(r"Expression:\s*(.+)", a["text"])
            try:
                v = calc(ast.parse(m.group(1).replace("×", "*").replace("$", "").replace(",", "").strip(), mode="eval"))
                a["score"] = final_answer(f"Answer: {v:g}", t["reference"])
            except Exception:  # noqa: BLE001 - unusable expression scores 0
                a["score"] = 0.0
            expr[r["id"]] = a
        out["expression"] = expr
    data = json.loads(NEW.read_text()) if NEW.exists() else {}
    data.update(out)
    NEW.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"Real spend ${SpendMeter.usd:.4f}")


def ask_local(model, prompt):
    body = {"model": model, "prompt": prompt, "stream": False, "think": False,
            "options": {"temperature": 0, "num_predict": 1024}}
    req = urllib.request.Request("http://localhost:11434/api/generate", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=300))["response"]


def local():
    rows, by_id = tasks()
    data = json.loads(NEW.read_text()) if NEW.exists() else {}
    for model in LOCAL:
        res = data.setdefault(model, {})
        start = time.time()
        for n, r in enumerate(rows):
            if r["id"] in res:
                continue
            t = by_id[r["id"]]
            text = ask_local(model, t["prompt"])
            res[r["id"]] = {"text": text, "usd": 0.0, "score": final_answer(text, t["reference"])}
            if n % 20 == 0:
                NEW.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
                print(model, n, round(time.time() - start), flush=True)
        NEW.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    if sys.argv[1] == "paid":
        paid(float(sys.argv[3]))
    else:
        local()

#!/usr/bin/env python3
"""Check the public run's short-writing answers against IFEval's countable rules.

No model calls. Needs data/runs.db, and reads the rules from the pinned IFEval
dataset. A what-if test only: the published scores stay as the plan fixed them.
"""
import json, re, statistics as st

import analyze as a
import build_public_tasks as b

G = '20261105T092337-0eb276cf'

def rel(n, r, k): return n >= k if r == 'at least' else n < k
def words(t): return re.findall(r'\w+', t)
def sentences(t): return len([x for x in re.split(r'(?<=[.!?])\s+', t.strip()) if re.search(r'\w', x)])
def is_json(t):
    t = re.sub(r'^```(?:json|JSON|Json)?|```$', '', t.strip()).strip()
    try: json.loads(t); return True
    except ValueError: return False
def paragraphs(t):
    p = re.split(r'\s?\*\*\*\s?', t)
    if p and not p[0].strip(): p = p[1:]
    if p and not p[-1].strip(): p = p[:-1]
    return p
def highlights(t):
    return len([m for m in re.findall(r'\*[^\n*]*\*', t) if m.strip('*').strip()]) + \
           len([m for m in re.findall(r'\*\*[^\n*]*\*\*', t) if m.strip('*').strip()])
def bullets(t): return len(re.findall(r'^\s*\*[^*].*$', t, re.M)) + len(re.findall(r'^\s*-.*$', t, re.M))
def two(t):
    p = t.split('******')
    if p and not p[0].strip(): p = p[1:]
    if p and not p[-1].strip(): p = p[:-1]
    return len(p) == 2 and all(x.strip() for x in p) and p[0].strip() != p[1].strip()
def postscript(t, m):
    pat = r'\s*p\.\s?p\.\s?s.*$' if m == 'P.P.S' else r'\s*p\.\s?s\..*$' if m == 'P.S.' else r'\s*' + re.escape(m.lower()) + r'.*$'
    return bool(re.findall(pat, t.lower(), re.M))
def nth(t, k):
    p = re.split(r'\n\n', t)
    if len([x for x in p if x.strip()]) != len(p) or len(p) != k['num_paragraphs'] or k['nth_paragraph'] > len(p): return False
    w = p[k['nth_paragraph'] - 1].strip().split()
    return bool(w) and re.sub(r'\W', '', w[0]).lower() == k['first_word'].lower()

def check(i, k, t):
    """True/False, or None for a rule code here can't check."""
    if i == 'punctuation:no_comma': return ',' not in t
    if i == 'keywords:forbidden_words': return not any(re.search(r'\b' + re.escape(w) + r'\b', t, re.I) for w in k['forbidden_words'])
    if i == 'keywords:existence': return all(re.search(re.escape(w), t, re.I) for w in k['keywords'])
    if i == 'keywords:frequency': return rel(len(re.findall(re.escape(k['keyword']), t, re.I)), k['relation'], k['frequency'])
    if i == 'keywords:letter_frequency': return rel(t.lower().count(k['letter'].lower()), k['let_relation'], k['let_frequency'])
    if i == 'length_constraints:number_words': return rel(len(words(t)), k['relation'], k['num_words'])
    if i == 'length_constraints:number_sentences': return rel(sentences(t), k['relation'], k['num_sentences'])
    if i == 'length_constraints:number_paragraphs': p = paragraphs(t); return len(p) == k['num_paragraphs'] and all(x.strip() for x in p)
    if i == 'length_constraints:nth_paragraph_first_word': return nth(t, k)
    if i == 'combination:repeat_prompt': return t.strip().lower().startswith(k['prompt_to_repeat'].strip().lower())
    if i == 'combination:two_responses': return two(t)
    if i == 'detectable_format:number_highlighted_sections': return highlights(t) >= k['num_highlights']
    if i == 'detectable_format:number_bullet_lists': return bullets(t) == k['num_bullets']
    if i == 'detectable_format:json_format': return is_json(t)
    if i == 'detectable_format:title': return any(x.strip('<>').strip() for x in re.findall(r'<<[^\n]+>>', t))
    if i == 'detectable_format:constrained_response': return any(x in t for x in ('My answer is yes.', 'My answer is no.', 'My answer is maybe.'))
    if i == 'detectable_content:number_placeholders': return len(re.findall(r'\[.*?\]', t)) >= k['num_placeholders']
    if i == 'detectable_content:postscript': return postscript(t, k['postscript_marker'])
    if i == 'change_case:capital_word_frequency': return rel(len([w for w in re.findall(r"[^\W\d_]+", t) if w.isupper()]), k['capital_relation'], k['capital_frequency'])
    if i == 'change_case:english_lowercase': return t.islower()
    if i == 'change_case:english_capital': return t.isupper()
    if i == 'startend:end_checker': return t.strip().strip('"').lower().endswith(k['end_phrase'].strip().lower())
    if i == 'startend:quotation': t = t.strip(); return len(t) > 1 and t[0] == '"' and t[-1] == '"'
    if i == 'language:response_language': return None
    raise KeyError(i)


def load_rules():
    """The rule list for each judged task, from the dataset commit the tasks were built from."""
    b.check_pin('google/IFEval')
    tasks = json.load(open('data/public/benchmark/tasks_public.json'))['tasks']
    rules = {}
    for t in (x for x in tasks if x['scoring'] == 'rubric_judge'):
        index = int(t['id'].split('-')[1])
        row = b._page('google/IFEval', index // b.PAGE)[index % b.PAGE]['row']
        assert row['prompt'] == t['prompt'], t['id']
        rules[t['id']] = row
    return rules


def main():
    R = load_rules()
    c = a.connect()
    rows = [r for r in c.execute("select * from runs where run_group=? and task_type='short_generation'", (G,))]
    res = {}
    for r in rows:
        d = R[r['task_id']]
        out = [(i, check(i, {x: y for x, y in k.items() if y is not None}, r['response_text'])) for i, k in zip(d['instruction_id_list'], d['kwargs'])]
        res[r['task_id'], r['strategy']] = (out, r['quality_score'], r['model'], r['output_tokens'])

    tasks = sorted({t for t, s in res if any(x[1] is not None for x in res[t, s][0])})
    print('tasks with a checkable rule', len(tasks), 'of 72; rules unchecked (language)', sum(1 for t in R for i in R[t]['instruction_id_list'] if i.startswith('language')))
    def full(t, s): return float(all(x[1] for x in res[t, s][0] if x[1] is not None))
    def frac(t, s):
        v = [x[1] for x in res[t, s][0] if x[1] is not None]; return sum(v) / len(v)
    for s in ('router', 'frontier_only'):
        print(s, 'all rules passed', round(st.mean(full(t, s) for t in tasks), 3), 'rules passed', round(st.mean(frac(t, s) for t in tasks), 3), 'judge', round(st.mean(res[t, s][1] for t in tasks), 3))
        print('  cut off answers failing', sum(res[t, s][3] >= 1024 and not full(t, s) for t in tasks), 'of', sum(res[t, s][3] >= 1024 for t in tasks))
    p = [(0, 0, full(t, 'frontier_only'), full(t, 'router')) for t in tasks]
    print('retention all-rules', [round(x, 1) for x in a.bootstrap(p, a.retention)])
    p2 = [(0, 0, frac(t, 'frontier_only'), frac(t, 'router')) for t in tasks]
    print('retention rule-level', [round(x, 1) for x in a.bootstrap(p2, a.retention)])
    J = [(res[t, s][1], full(t, s), frac(t, s)) for t in tasks for s in ('router', 'frontier_only')]
    print('judge vs code: corr full', round(st.correlation([x[0] for x in J], [x[1] for x in J]), 3), 'corr frac', round(st.correlation([x[0] for x in J], [x[2] for x in J]), 3))
    print('judge>=0.5 agrees with all-rules', sum((x[0] >= .5) == (x[1] == 1) for x in J), 'of', len(J), '| judge passes but code fails', sum(x[0] >= .9 and x[1] == 0 for x in J), '| judge low, code passes', sum(x[0] < .5 and x[1] == 1 for x in J))
    fails = {}
    for t in tasks:
        for s in ('router', 'frontier_only'):
            for i, ok in res[t, s][0]:
                if ok is False: fails.setdefault(i, [0, 0])[s == 'router'] += 1
    print('failures [opus, router]', sorted(fails.items(), key=lambda x: -sum(x[1])))
    # Sensitivity: swap the judge's short-writing score for the code result across all 300 tasks.
    allp = a.paired_tasks(c, G)
    q = [(b, e, full(t, 'frontier_only') if t in tasks else bq, full(t, 'router') if t in tasks else eq) for t, b, e, bq, eq in allp]
    print('run A retention, code replaces judge', [round(x, 1) for x in a.bootstrap(q, a.retention)], 'means', round(st.mean(x[2] for x in q), 3), round(st.mean(x[3] for x in q), 3))


if __name__ == "__main__":
    main()

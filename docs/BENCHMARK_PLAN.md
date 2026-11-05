# Public benchmark plan

Fixed before any paid call, so the test can't be chosen after seeing the result. This commit holds the plan, the tasks in `data/public/benchmark/` and the pass bar. **No real run has happened yet.**

## The question

Does the saving hold on a bigger set of public tasks? The current 65.6% comes from 25 tasks, with a 95% range of 40% to 98%.

## The runs

Both use the `cross_vendor` roster and the real classifier, compared with sending everything to Claude Opus 4.8.

| Run | File | Tasks |
|---|---|---|
| A: mixed | `tasks_public.json` | 300: Banking77 96, SQuAD v2 84, IFEval 72, GSM8K 24, MMLU-Pro 24 |
| B: hard | `tasks_public_hard.json` | 40: the repo's 10 hard tasks and 30 from MMLU-Pro |

Run A keeps the task-type shares of `data/tasks.json`. Run B mainly tests the classifier: if it spots every hard task, all 40 go to Opus and the saving is 0%, which would be a good result. Quality is the number to watch there.

```bash
python run_benchmark.py --classify --tasks data/public/benchmark/tasks_public.json --max-spend USD
```

```bash
python run_benchmark.py --classify --tasks data/public/benchmark/tasks_public_hard.json --max-spend USD
```

## Pass bar

The same as July: **at least 40% cheaper, keeping at least 95% of quality** on Run A. The report gives the 95% range and a breakdown by task type.

**The result gets published whatever it says.**

## Budget

The hard cap is **$8 across both runs**. The estimate from these prompts at list prices is about $4.30 if every call is charged, assuming answer lengths that only a real run can confirm. The cap counts one process at a time, so each run or restart gets only what is left.

## Stop rules

A capped run stops, and writes no report, when:

- less than $0.10 of the cap is left
- a classifier call falls back to the keyword heuristic
- a judge call fails or its reply is not just a score
- a provider returns an error
- any answer is marked simulated

A stopped run is reported as stopped, not patched up.

## Review

A person scores 30 judged answers by hand, to check the Opus judge. `judge_validation` adds GPT-4o mini as a second judge.

## Known limits

- It tests a fixed mix of public tasks, not a real workload, and the mix drives the answer.
- These models have probably seen public benchmarks in training, so scores may look better than on private work.
- Answers are capped at 1,024 tokens. A cut-off answer loses its `Answer:` line and scores 0, and the run prints how many were cut off.
- Answers in the wrong format score 0, for every model.
- The judge reads IFEval's instructions but does not check them with code, so short writing is the softest score.

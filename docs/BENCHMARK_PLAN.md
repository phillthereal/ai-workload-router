# Public benchmark plan

Fixed before any paid call, so the test can't be chosen after seeing the result. This commit holds the plan, the tasks in `data/public/benchmark/` and the pass bar. One real attempt has stopped early; see "Change after the first attempt".

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
- a judge call fails or its reply has no line that is just a score
- a provider returns an error
- any answer is marked simulated

A stopped run is reported as stopped, not patched up.

## Change after the first attempt

The first Run A stopped at task 190 of 300, having spent $0.0837 and before any comparison existed. The Opus judge wrote out its working and put a score on the last line. With Filip's approval the judge check now accepts a score alone on the first or last line, and still stops if neither line is just a score or if the two disagree. The tasks, the router, the judge's instructions and the pass bar did not change. The rerun gets only what is left of the $8.

## Results

Real spend was $2.7228 of the $8: $0.0837 on the stopped attempt, $2.3805 on Run A and $0.2586 on Run B. The 95% ranges below come from `analyze.py`'s paired bootstrap, and they aren't in the run reports.

| Run | Cheaper than all-Opus | 95% range | Quality kept | 95% range | Pass bar |
|---|---|---|---|---|---|
| A: mixed, 300 tasks | 84.1% | 79.0% to 88.7% | 91.8% | 86.9% to 96.6% | **Not met** |
| B: hard, 40 tasks | 22.2% | 9.0% to 40.3% | 96.1% | 89.5% to 100.0% | Applies to Run A only |

Run A quality kept by task type: classification 80.3%, extraction 91.8%, reasoning 97.8%, short writing 101.1%. Run B sent 27 of its 40 tasks to Opus. Neither run had a classifier fallback. The runs printed 11 and 4 cut-off answers, but the database shows 14 Run A answers at the 1,024-token limit, and that gap isn't explained. The reports are `data/benchmark_report_cross_vendor_classified_public.md` and `data/benchmark_report_cross_vendor_classified_public_hard.md`.

## Review

A person scores 30 judged answers by hand, to check the Opus judge. `judge_validation` adds GPT-4o mini as a second judge.

## Known limits

- It tests a fixed mix of public tasks, not a real workload, and the mix drives the answer.
- These models have probably seen public benchmarks in training, so scores may look better than on private work.
- Answers are capped at 1,024 tokens. A cut-off answer loses its `Answer:` line and scores 0, and the run prints how many were cut off.
- Answers in the wrong format score 0, for every model.
- The judge reads IFEval's instructions but does not check them with code, so short writing is the softest score.

# Public benchmark tasks

Built by `python build_public_tasks.py`, with a fixed seed. It stops if a dataset has moved from its pinned version. No model is called. The task files were committed before any paid run, and [the plan](../../../docs/BENCHMARK_PLAN.md) fixes how they are used.

| File | Tasks |
|---|---|
| `tasks_public.json` | 300 mixed tasks, at the same task-type shares as `data/tasks.json` |
| `tasks_public_hard.json` | 40 hard tasks: the repo's 10 plus 30 from MMLU-Pro |

| Source | Used for | Licence on its dataset card | Credit |
|---|---|---|---|
| [Banking77](https://github.com/PolyAI-LDN/task-specific-datasets) | Classification | CC-BY-4.0 | Casanueva et al., 2020, PolyAI Ltd. |
| [SQuAD v2](https://huggingface.co/datasets/rajpurkar/squad_v2) | Extraction | CC-BY-SA-4.0 | Rajpurkar, Jia and Liang, 2018 |
| [IFEval](https://huggingface.co/datasets/google/IFEval) | Short writing | Apache-2.0 | Zhou et al., 2023, Google |
| [GSM8K](https://huggingface.co/datasets/openai/gsm8k) | Reasoning | MIT | Cobbe et al., 2021, OpenAI |
| [MMLU-Pro](https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro) | Reasoning | MIT | Wang et al., 2024, TIGER-Lab |

**This folder is not under the repo's MIT licence.** The task files contain SQuAD text, so they are shared under CC-BY-SA-4.0, with the credits above.

Prompts were reformatted to ask for a final `Answer:` line, and each task records its `source`. Classification, extraction and reasoning use the `final_answer` scorer, which reads only that line. Short writing uses the LLM judge.

Difficulty labels here are assigned per source, not per task. The router never sees them, because the benchmark runs with `--classify`.

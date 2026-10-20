# Power BI guide: AI Workload Router dashboard

Data lives in `data/bi/` (regenerate with `python3 analyze.py --export`). It has no prompt or response text, only numbers, ids and labels.

## 1. Import
1. Power BI Desktop > Get data > Text/CSV. Load all four files: `fact_runs.csv`, `dim_models.csv`, `dim_tasks.csv`, `dim_run_groups.csv`.
2. For each, click Transform Data and check types: `cost_usd`, `routing_cost_usd`, `quality_score`, `latency_ms`, prices = Decimal number; `created_at` = Date/Time (it is blank for the oldest rows, that is expected); `success` = Whole number.
3. Close & Apply.

## 2. Relationships (Model view)
Star schema, fact in the middle. All are many-to-one, single direction (dim filters fact):
- `fact_runs[model]` -> `dim_models[model]`
- `fact_runs[task_id]` -> `dim_tasks[task_id]`
- `fact_runs[run_group]` -> `dim_run_groups[run_group]`

Caveat: `task_id` is unique per task set but check `dim_tasks` for duplicates; if Power BI refuses the relationship, remove duplicate rows in Power Query.

## 3. Important: compare like with like
Every run_group holds two arms: `frontier_only` (the baseline) and one experimental strategy (`router`, `cascade`, `jury`). Savings only make sense **within one experiment**, so always put `dim_run_groups[experiment]` on a slicer and select one experiment (or use it as the visual's axis). Groups labelled `unlabelled/replay` are early runs and cache replays of 2026-07-17 calls (the 2026-07-19 groups re-ran the same calls from cache); keep them out of headline numbers.

## 4. DAX measures
Create a `Measures` table (Home > Enter data, any blank table) and add:

```DAX
Total Cost = SUM(fact_runs[cost_usd])

Routing Overhead = SUM(fact_runs[routing_cost_usd])

Baseline Cost =
CALCULATE(SUM(fact_runs[cost_usd]), fact_runs[strategy] = "frontier_only")

Routed Cost (net) =
CALCULATE(SUM(fact_runs[cost_usd]) + SUM(fact_runs[routing_cost_usd]),
          fact_runs[strategy] <> "frontier_only")

Net Savings % = DIVIDE([Baseline Cost] - [Routed Cost (net)], [Baseline Cost])

Gross Savings =
[Baseline Cost] - CALCULATE(SUM(fact_runs[cost_usd]), fact_runs[strategy] <> "frontier_only")

Overhead % of Savings = DIVIDE([Routing Overhead], [Gross Savings])

Quality Retention =
DIVIDE(
    CALCULATE(AVERAGE(fact_runs[quality_score]), fact_runs[strategy] <> "frontier_only"),
    CALCULATE(AVERAGE(fact_runs[quality_score]), fact_runs[strategy] = "frontier_only"))

Cost per Quality Point = DIVIDE([Total Cost], SUM(fact_runs[quality_score]))
```
Format the `%` measures as Percentage and costs as Currency with 4-6 decimals (costs per task are fractions of a cent).

These match the project's own report definitions (net savings subtract routing overhead; retention is routed quality divided by baseline quality), so a single-experiment view should reproduce the published numbers, e.g. 53.6% for "v1 cross_vendor / labels / easy".

## 5. Suggested dashboard

### Page 1: "Does routing pay off?"
- Slicer: `dim_run_groups[experiment]` (single select).
- Cards: `Net Savings %`, `Quality Retention`, `Overhead % of Savings`, `Baseline Cost`.
- Clustered bar: axis `experiment`, value `Net Savings %` (no slicer on this one; edit interactions to ignore it). Shows which setups win and which go negative.
- Clustered column: axis `dim_tasks[difficulty]`, values `Baseline Cost` and `Routed Cost (net)`.
- Note text box: "n = 10 to 25 tasks per experiment; see docs/ANALYSIS.md for confidence intervals."

### Page 2: "Where does the money and quality go?"
- Stacked bar: axis `dim_models[tier]` (or `model`), value `Total Cost`, legend `strategy`.
- Matrix: rows `dim_tasks[task_type]`, columns `dim_tasks[difficulty]`, values `Net Savings %` and `Quality Retention` (conditional formatting: red when negative / below 95%).
- Table: `model`, `provider`, `Total Cost`, `Cost per Quality Point`, average `quality_score`.
- Scatter: x `Total Cost` per `task_id`, y average `quality_score`, legend `strategy`, to spot expensive tasks that gain no quality.

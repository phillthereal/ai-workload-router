"""
AI Workload Router — routes LLM tasks to the cheapest capable model.

This package implements the core routing and benchmarking pipeline:
- adapters: normalized interface to multiple LLM providers
- benchmark: task loading and test harness
- router: rules-based task routing
- scoring: output quality evaluation
- db: performance logging and querying
- report: benchmark report generation
"""

__version__ = "0.1.0"

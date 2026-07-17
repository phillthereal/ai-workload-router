"""
Task loading from data/tasks.json.

Parses and validates the benchmark task set. Each task has:
- id: unique identifier
- task_type: classification, extraction, short_generation, or reasoning
- difficulty: easy, medium, or hard
- prompt: the input text sent to the model
- reference: expected answer (exact_match) or rubric (rubric_judge)
- scoring: exact_match or rubric_judge
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Union

DEFAULT_TASKS_PATH = Path(__file__).resolve().parents[3] / "data" / "tasks.json"

_REQUIRED_FIELDS = ("id", "task_type", "difficulty", "prompt", "reference", "scoring")
_VALID_TASK_TYPES = {"classification", "extraction", "short_generation", "reasoning"}
_VALID_DIFFICULTIES = {"easy", "medium", "hard"}
_VALID_SCORING = {"exact_match", "rubric_judge"}


def load_tasks(path: Optional[Union[str, Path]] = None) -> list[dict[str, Any]]:
    """
    Load the benchmark task set from data/tasks.json.

    Args:
        path: Path to tasks.json. If None, uses the default project location.

    Returns:
        List of task dictionaries, each with id, task_type, difficulty,
        prompt, reference, scoring.

    Raises:
        FileNotFoundError: If tasks.json not found.
        ValueError: If schema is invalid.
    """
    resolved = Path(path) if path else DEFAULT_TASKS_PATH
    if not resolved.exists():
        raise FileNotFoundError(f"Task file not found: {resolved}")

    data = json.loads(resolved.read_text())
    if data.get("version") not in (1, 2):
        raise ValueError(f"Unsupported tasks.json version: {data.get('version')!r}")

    tasks = data.get("tasks", [])
    for task in tasks:
        validate_task(task)
    return tasks


def validate_task(task: dict[str, Any]) -> bool:
    """
    Validate a single task against the schema.

    Args:
        task: Task dict to validate.

    Returns:
        True if valid.

    Raises:
        ValueError: If task is missing required fields or has invalid values.
    """
    missing = [f for f in _REQUIRED_FIELDS if f not in task]
    if missing:
        raise ValueError(f"Task {task.get('id', '?')} missing fields: {missing}")
    if task["task_type"] not in _VALID_TASK_TYPES:
        raise ValueError(f"Task {task['id']} has invalid task_type: {task['task_type']!r}")
    if task["difficulty"] not in _VALID_DIFFICULTIES:
        raise ValueError(f"Task {task['id']} has invalid difficulty: {task['difficulty']!r}")
    if task["scoring"] not in _VALID_SCORING:
        raise ValueError(f"Task {task['id']} has invalid scoring: {task['scoring']!r}")
    return True

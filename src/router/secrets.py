"""
Tiny stdlib `.env` loader and environment-flag helpers.

Deliberately NOT python-dotenv (see task brief: no extra deps). Parses
`KEY=VALUE` pairs from the project-root `.env` file into a private,
in-process cache. Values are never printed, logged, or returned in any
exception message — callers get `None`/`False`, never the raw secret, when
something is missing.

Two things live here:
  - `get_api_key(env_var_name)`: provider credential lookup, real
    environment first, then `.env` file.
  - `force_mock()` / `refresh_cache()`: small boolean escape hatches so
    tests and manual runs can force the offline mock path or bypass the
    record/replay cache, via `AWR_FORCE_MOCK` / `AWR_REFRESH` env vars.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_cache: Optional[dict[str, str]] = None


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple `KEY=VALUE` `.env` file. No interpolation, no export."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip a single layer of matching quotes, if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        _cache = _parse_env_file(_ENV_PATH)
    return _cache


def get_api_key(env_var_name: str) -> Optional[str]:
    """
    Look up an API key by environment variable name.

    Prefers a real process environment variable (e.g. set by CI) over the
    `.env` file, then falls back to the `.env` file. Returns None (never
    raises) when unset or blank, so callers can gracefully degrade to the
    mock adapter instead of crashing the benchmark. Never logs the value.

    Args:
        env_var_name: Name of the env var, e.g. "ANTHROPIC_API_KEY".

    Returns:
        The key value, or None if not configured.
    """
    if not env_var_name:
        return None
    value = os.environ.get(env_var_name)
    if value:
        return value
    return _load().get(env_var_name) or None


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def force_mock() -> bool:
    """
    True if AWR_FORCE_MOCK is set — forces every adapter (and the judge) to
    the offline mock path regardless of configured API keys. Used by the
    test suite so it never makes a real network call even when real keys
    are present in .env.
    """
    return _env_flag("AWR_FORCE_MOCK")


def refresh_cache() -> bool:
    """True if AWR_REFRESH is set — bypasses cache reads (writes still happen)."""
    return _env_flag("AWR_REFRESH")

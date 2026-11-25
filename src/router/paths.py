"""Where the router keeps its writable files (``data/`` and ``.cache/``).

By default that is the repo root, found from this file's location, so a checkout
behaves as it always has. Set ``AWR_HOME`` to put both folders somewhere else,
for example in a container or a normal (non-editable) install.

Package data that ships next to the code (the web UI page, the lookup task
list) is not affected: it stays beside its module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ENV_VAR = "AWR_HOME"
_REPO_ROOT = Path(__file__).resolve().parents[2]  # src/router/paths.py -> repo root


def resolve_home(env: Mapping[str, str]) -> Path:
    """The home folder: ``AWR_HOME`` (``~`` expanded) if set and not blank, else the repo root."""
    raw = env.get(ENV_VAR, "").strip()
    return Path(raw).expanduser() if raw else _REPO_ROOT


HOME = resolve_home(os.environ)
DATA_DIR = HOME / "data"
CACHE_DIR = HOME / ".cache"

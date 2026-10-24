"""Versioned, learnable rulebook: the instructions + option descriptions Jev decides with.

Plain data (JSON on disk) so a learning loop can edit it, diff it, and gate each edit. `content_hash()`
changes iff the decision-relevant text changes (version number alone does not).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Rulebook:
    version: int
    instructions: str
    criteria: dict[str, str]          # option -> description
    notes: list[str] = field(default_factory=list)  # learned clarifications, appended to instructions

    def render_instructions(self) -> str:
        if not self.notes:
            return self.instructions
        return self.instructions + "\n\nClarifications:\n" + "\n".join(f"- {n}" for n in self.notes)

    def render_prompt(self, state: str) -> str:
        """The same rulebook as a prompt for an LLM that must pick one option (the escalation path)."""
        opts = "\n".join(f"- {k}: {v}" for k, v in self.criteria.items())
        return (f"{self.render_instructions()}\n\nOptions:\n{opts}\n\nText to decide on:\n{state}\n\n"
                f"Reply with ONLY the option name, one of: {', '.join(self.criteria)}.")

    def parse_choice(self, text: str):
        """First option named as a whole token in `text`, else None."""
        hits = [(m.start(), o) for o in self.criteria for m in [re.search(rf"\b{re.escape(o)}\b", text or "")] if m]
        return min(hits)[1] if hits else None

    def with_criterion(self, option: str, description: str) -> "Rulebook":
        if option not in self.criteria:
            raise KeyError(option)
        return replace(self, version=self.version + 1, criteria={**self.criteria, option: description})

    def with_note(self, note: str) -> "Rulebook":
        return replace(self, version=self.version + 1, notes=[*self.notes, note])

    def content_hash(self) -> str:
        body = json.dumps([self.instructions, self.criteria, self.notes], sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        return dict(version=self.version, instructions=self.instructions, criteria=self.criteria,
                    notes=self.notes, hash=self.content_hash())

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    @classmethod
    def load(cls, path) -> "Rulebook":
        d = json.loads(Path(path).read_text())
        return cls(d["version"], d["instructions"], dict(d["criteria"]), list(d.get("notes", [])))

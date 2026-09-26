"""Short-term (thread-level) memory for one conversation.

Holds the turn transcript, the results of the latest triage in this conversation, and the
"focus" entities (last claim / policy discussed) so follow-ups such as *"and what documents
do they need for it?"* resolve correctly.

In Foundry mode each specialist agent also gets one persistent Agent Service **thread** per
session (IDs stored in ``agent_threads``), so the model-side conversation history lives in
Foundry. The session file is saved after every turn, so ``chat --session <id>`` resumes a
conversation later - including the same Foundry threads.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Turn:
    role: str
    content: str
    ts: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionMemory:
    session_id: str = field(default_factory=lambda: "s-" + uuid.uuid4().hex[:8])
    turns: list[Turn] = field(default_factory=list)
    focus_claim_id: str | None = None
    focus_policy: str | None = None
    last_batch_source: str | None = None
    last_run_id: str | None = None
    assessments: dict[str, dict[str, Any]] = field(default_factory=dict)  # record_id -> assessment dict
    agent_threads: dict[str, str] = field(default_factory=dict)  # agent name -> Foundry thread id

    def add(self, role: str, content: str, **meta: Any) -> None:
        self.turns.append(Turn(role, content, meta=meta))

    def recent(self, n: int = 6) -> list[Turn]:
        return self.turns[-n:]

    # ----------------------------------------------------------- persistence
    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.session_id}.json"
        data = asdict(self)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path

    @classmethod
    def load(cls, directory: Path, session_id: str) -> "SessionMemory":
        path = directory / f"{session_id}.json"
        if not path.exists():
            return cls(session_id=session_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["turns"] = [Turn(**t) for t in data.get("turns", [])]
        return cls(**data)

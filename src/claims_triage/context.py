"""Shared runtime context passed to every tool (the "world" the agents operate on)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import Settings
from .data_loader import ClaimBatch, load_catalog, load_policies
from .knowledge import DecisionPolicy, LocalVectorStore, Rule, load_decision_policy, load_rules
from .memory import LongTermMemory, SessionMemory
from .models import ClaimAssessment, Policy


@dataclass
class TriageContext:
    settings: Settings
    policies: dict[str, Policy]
    catalog: dict[str, list[str]]
    kb: LocalVectorStore
    rules: list[Rule]
    decision_policy: DecisionPolicy
    ltm: LongTermMemory
    session: SessionMemory
    batch: ClaimBatch | None = None
    assessments: dict[str, ClaimAssessment] = field(default_factory=dict)
    scratch: dict[str, Any] = field(default_factory=dict)

    @property
    def today(self) -> date:
        return date.fromisoformat(self.settings.today) if self.settings.today else date.today()

    @classmethod
    def build(cls, settings: Settings, session: SessionMemory | None = None) -> "TriageContext":
        kb = LocalVectorStore(settings.knowledge_dir, cache_path=settings.memory_dir / "kb_index.json")
        return cls(
            settings=settings,
            policies=load_policies(settings.policies_path),
            catalog=load_catalog(settings.catalog_path),
            kb=kb,
            rules=load_rules(kb),
            decision_policy=load_decision_policy(settings.knowledge_dir / "decision_policy.md"),
            ltm=LongTermMemory(settings.memory_dir / "long_term_memory.json", settings.seed_memory_path),
            session=session or SessionMemory(),
        )

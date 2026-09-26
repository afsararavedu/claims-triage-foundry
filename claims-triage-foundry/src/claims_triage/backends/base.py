"""Backend interface: how an agent "run" is executed.

The routine talks only to :class:`AgentBackend`. Two implementations:

* :class:`~claims_triage.backends.foundry_backend.FoundryBackend` - Azure AI Foundry Agent
  Service. Agents, threads, runs, function-tool round-trips and file search are real.
* :class:`~claims_triage.backends.mock_backend.MockBackend` - deterministic offline
  stand-in for the model. It calls the *same* tools through the *same* registry, so
  routing, tool calls, memory, knowledge retrieval, HITL gates, error handling and traces
  behave identically; only the natural-language generation is templated.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..agents import AgentSpec
from ..tools import ToolRegistry
from ..tools.registry import ToolCallRecord


class AgentRunError(Exception):
    """The agent run failed (model error, content filter, service error...)."""


class AgentTimeoutError(AgentRunError):
    """The agent run did not finish within the configured timeout."""


@dataclass
class AgentRunResult:
    agent: str
    text: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    run_id: str | None = None
    thread_id: str | None = None
    status: str = "completed"

    def json(self) -> dict[str, Any] | None:
        """Best-effort extraction of the JSON object an agent was asked to return."""
        return extract_json(self.text)

    def called(self, tool: str) -> bool:
        return any(c.tool == tool and c.ok for c in self.tool_calls)


def extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            value = json.loads(c)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


class AgentBackend(ABC):
    name: str = "base"

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    @abstractmethod
    def setup(self, specs: dict[str, AgentSpec]) -> dict[str, str]:
        """Create (or reuse) the agents. Returns {agent_name: agent_id}."""

    @abstractmethod
    def run(self, agent: str, message: str, ctx: Any) -> AgentRunResult:
        """Send ``message`` to ``agent`` on the session's thread and run it to completion."""

    def teardown(self, delete_remote: bool = False) -> None:  # pragma: no cover - optional
        return None

    def _maybe_inject_agent_fault(self, agent: str) -> None:
        key = f"agent.{agent}"
        remaining = self.registry.fault_injection.get(key, 0)
        if remaining > 0:
            self.registry.fault_injection[key] = remaining - 1
            raise AgentTimeoutError(f"injected timeout for agent '{agent}' (TRIAGE_FAULT_INJECTION)")

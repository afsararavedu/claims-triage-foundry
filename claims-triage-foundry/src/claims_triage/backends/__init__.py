from __future__ import annotations

from ..config import Settings
from ..tools import ToolRegistry
from .base import AgentBackend, AgentRunError, AgentRunResult, AgentTimeoutError, extract_json


def create_backend(settings: Settings, registry: ToolRegistry) -> AgentBackend:
    if settings.backend == "foundry":
        from .foundry_backend import FoundryBackend

        return FoundryBackend(registry, settings)
    from .mock_backend import MockBackend

    return MockBackend(registry)


__all__ = ["create_backend", "AgentBackend", "AgentRunError", "AgentRunResult", "AgentTimeoutError", "extract_json"]

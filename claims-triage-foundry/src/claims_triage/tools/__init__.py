"""All skills (function tools), each with an explicit JSON schema and owning agent(s)."""
from __future__ import annotations

from . import briefing, coverage, intake
from .registry import ToolError, ToolRegistry, ToolSpec, TransientToolError


def build_registry(max_retries: int = 2, fault_injection: str | None = None) -> ToolRegistry:
    reg = ToolRegistry(max_retries=max_retries, fault_injection=ToolRegistry.parse_fault_injection(fault_injection))
    for spec in [*intake.TOOLS, *coverage.TOOLS, *briefing.TOOLS]:
        reg.register(spec)
    return reg


__all__ = ["build_registry", "ToolRegistry", "ToolSpec", "ToolError", "TransientToolError"]

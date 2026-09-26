"""Tool (skill) registry and dispatcher.

Every skill is declared once as a :class:`ToolSpec` with an explicit JSON schema. The same
spec is used to:

* register the function tool on the Foundry agent (``FunctionToolDefinition``),
* validate the arguments the model sends back before running any code,
* log/trace the call and its result,
* retry transient failures, and turn hard failures into a structured error the model
  (or the routine) can reason about instead of crashing the run.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..observability import log, span

try:
    import jsonschema
except Exception:  # pragma: no cover
    jsonschema = None


class ToolError(Exception):
    """A tool failed in a way the caller should see (bad input, not found...)."""


class TransientToolError(ToolError):
    """A tool failed in a way that is worth retrying (timeouts, throttling...)."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., dict[str, Any]]
    agents: tuple[str, ...]  # which agents this skill is registered to

    def openai_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass
class ToolCallRecord:
    tool: str
    agent: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    ok: bool
    attempts: int
    duration_ms: float


@dataclass
class ToolRegistry:
    specs: dict[str, ToolSpec] = field(default_factory=dict)
    max_retries: int = 2
    fault_injection: dict[str, int] = field(default_factory=dict)  # tool -> remaining forced failures
    calls: list[ToolCallRecord] = field(default_factory=list)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self.specs:
            raise ValueError(f"duplicate tool {spec.name}")
        self.specs[spec.name] = spec

    def for_agent(self, agent: str) -> list[ToolSpec]:
        return [s for s in self.specs.values() if agent in s.agents]

    @staticmethod
    def parse_fault_injection(value: str | None) -> dict[str, int]:
        out: dict[str, int] = {}
        for part in (value or "").split(","):
            if ":" in part:
                name, n = part.split(":", 1)
                out[name.strip()] = int(n)
        return out

    def invoke(self, name: str, arguments: str | dict[str, Any] | None, ctx: Any, agent: str) -> dict[str, Any]:
        """Run a tool safely. Never raises: failures come back as ``{"ok": false, ...}``."""
        started = time.perf_counter()
        attempts = 0
        with span("TOOL", name, agent=agent) as sp:
            try:
                spec = self.specs.get(name)
                if spec is None:
                    raise ToolError(f"unknown tool '{name}'")
                if agent not in spec.agents:
                    raise ToolError(f"tool '{name}' is not registered to agent '{agent}'")
                args = self._parse_args(arguments)
                self._validate(spec, args)
                log("TOOL", f"{agent} -> {name}({_short(args)})")
                result: dict[str, Any] | None = None
                while True:
                    attempts += 1
                    try:
                        self._maybe_inject_fault(name)
                        result = spec.handler(ctx, **args)
                        break
                    except TransientToolError as exc:
                        if attempts > self.max_retries:
                            raise ToolError(f"{exc} (gave up after {attempts} attempts)") from exc
                        backoff = 0.2 * (2 ** (attempts - 1))
                        log("TOOL", f"{name} transient failure: {exc}; retry {attempts}/{self.max_retries} in {backoff:.1f}s",
                            logging.WARNING)
                        time.sleep(backoff)
                out = {"ok": True, **(result or {})}
            except ToolError as exc:
                out = {"ok": False, "error": str(exc), "tool": name}
                log("ERROR", f"{name} failed: {exc}", logging.WARNING)
            except Exception as exc:  # defensive: a bug in a tool must not kill the run
                out = {"ok": False, "error": f"internal error: {type(exc).__name__}: {exc}", "tool": name}
                log("ERROR", f"{name} crashed: {type(exc).__name__}: {exc}", logging.ERROR)
            sp.update(ok=out["ok"], attempts=attempts)
        self.calls.append(
            ToolCallRecord(name, agent, _safe_args(arguments), out, out["ok"], attempts,
                           round((time.perf_counter() - started) * 1000, 1))
        )
        return out

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _parse_args(arguments: str | dict[str, Any] | None) -> dict[str, Any]:
        if arguments is None or arguments == "":
            return {}
        if isinstance(arguments, dict):
            return arguments
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ToolError(f"arguments are not valid JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ToolError("arguments must be a JSON object")
        return parsed

    @staticmethod
    def _validate(spec: ToolSpec, args: dict[str, Any]) -> None:
        if jsonschema is not None:
            try:
                jsonschema.validate(args, spec.parameters)
            except jsonschema.ValidationError as exc:
                raise ToolError(f"invalid arguments for {spec.name}: {exc.message}") from exc
        else:  # minimal fallback
            for req in spec.parameters.get("required", []):
                if req not in args:
                    raise ToolError(f"missing required argument '{req}'")

    def _maybe_inject_fault(self, name: str) -> None:
        remaining = self.fault_injection.get(name, 0)
        if remaining > 0:
            self.fault_injection[name] = remaining - 1
            raise TransientToolError("injected fault (TRIAGE_FAULT_INJECTION)")


def _short(args: dict[str, Any]) -> str:
    s = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return s if len(s) < 120 else s[:117] + "..."


def _safe_args(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        v = json.loads(arguments or "{}")
        return v if isinstance(v, dict) else {"_raw": arguments}
    except Exception:
        return {"_raw": str(arguments)}

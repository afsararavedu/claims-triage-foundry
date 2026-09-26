"""Logging and tracing.

Every routine step, agent run and tool call goes through :func:`span`. A span:

1. logs a readable line to the console (``[STEP]``, ``[AGENT]``, ``[TOOL]`` ...),
2. appends a structured event to the in-memory :class:`TraceRecorder` (written to
   ``runs/<run_id>/trace.jsonl`` so a run can be replayed/inspected after the fact),
3. opens an OpenTelemetry span if ``opentelemetry`` is installed. In Foundry mode the
   same OTel pipeline can export to Application Insights so traces show up in the
   Foundry portal's Tracing tab (see ``configure_foundry_tracing``).
"""
from __future__ import annotations

import contextlib
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

try:
    from opentelemetry import trace as _otel_trace

    _TRACER = _otel_trace.get_tracer("claims_triage")
except Exception:  # pragma: no cover - otel optional
    _otel_trace = None
    _TRACER = None

LOG = logging.getLogger("claims_triage")

_COLORS = {
    "STEP": "\033[95m",
    "GATE": "\033[93m",
    "AGENT": "\033[94m",
    "TOOL": "\033[96m",
    "ROUTER": "\033[92m",
    "MEMORY": "\033[35m",
    "KNOWLEDGE": "\033[36m",
    "ERROR": "\033[91m",
    "RESET": "\033[0m",
}


class _ConsoleFormatter(logging.Formatter):
    def __init__(self, color: bool) -> None:
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        kind = getattr(record, "kind", record.levelname)
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        msg = record.getMessage()
        tag = f"[{kind}]"
        if self.color and kind in _COLORS:
            tag = f"{_COLORS[kind]}{tag}{_COLORS['RESET']}"
        if record.levelno >= logging.ERROR and self.color:
            msg = f"{_COLORS['ERROR']}{msg}{_COLORS['RESET']}"
        return f"{ts} {tag:<9} {msg}"


def configure_logging(level: str = "INFO", color: bool | None = None) -> None:
    root = logging.getLogger("claims_triage")
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    if color is None:
        color = sys.stderr.isatty()
    handler.setFormatter(_ConsoleFormatter(color))
    root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False
    # Azure SDK HTTP logging is very chatty; keep it at WARNING unless debugging.
    logging.getLogger("azure").setLevel(logging.WARNING)


@dataclass
class TraceRecorder:
    """Collects structured events for one run, flushed to ``trace.jsonl``."""

    run_id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, kind: str, name: str, **attrs: Any) -> None:
        self.events.append({"ts": time.time(), "run_id": self.run_id, "kind": kind, "name": name, **attrs})

    def flush(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "trace.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for event in self.events:
                fh.write(json.dumps(event, default=str) + "\n")
        return path


_current_recorder: TraceRecorder | None = None


def set_recorder(recorder: TraceRecorder | None) -> None:
    global _current_recorder
    _current_recorder = recorder


def get_recorder() -> TraceRecorder | None:
    return _current_recorder


def log(kind: str, message: str, level: int = logging.INFO, **attrs: Any) -> None:
    LOG.log(level, message, extra={"kind": kind})
    if _current_recorder is not None:
        _current_recorder.record(kind, message, **attrs)


@contextlib.contextmanager
def span(kind: str, name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
    """Context manager: timed, logged, recorded and (optionally) OTel-traced unit of work.

    The yielded dict can be mutated to attach result attributes to the span.
    """
    started = time.perf_counter()
    result_attrs: dict[str, Any] = {}
    otel_cm = _TRACER.start_as_current_span(f"{kind.lower()}:{name}") if _TRACER else contextlib.nullcontext()
    with otel_cm as otel_span:
        if otel_span is not None:
            for k, v in attrs.items():
                with contextlib.suppress(Exception):
                    otel_span.set_attribute(f"triage.{k}", v if isinstance(v, (str, int, float, bool)) else str(v))
        status = "ok"
        try:
            yield result_attrs
        except Exception as exc:
            status = "error"
            result_attrs.setdefault("error", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            if _current_recorder is not None:
                _current_recorder.record(
                    kind, name, status=status, duration_ms=duration_ms, **attrs, **result_attrs
                )
            if otel_span is not None:
                for k, v in result_attrs.items():
                    with contextlib.suppress(Exception):
                        otel_span.set_attribute(
                            f"triage.{k}", v if isinstance(v, (str, int, float, bool)) else str(v)
                        )


def configure_console_otel() -> bool:
    """Export OTel spans to the console (useful locally). Returns True if enabled."""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)
        _rebind_tracer()
        return True
    except Exception as exc:  # pragma: no cover
        log("ERROR", f"Could not enable console OpenTelemetry: {exc}", logging.WARNING)
        return False


def configure_foundry_tracing(endpoint: str, credential: Any) -> bool:
    """Send OTel traces to the Application Insights resource connected to the Foundry project.

    Requires ``azure-ai-projects`` and ``azure-monitor-opentelemetry``. Also enables the
    Azure AI Agents instrumentor so SDK calls (create_agent, runs, tool calls) are traced.
    """
    try:
        from azure.ai.projects import AIProjectClient
        from azure.monitor.opentelemetry import configure_azure_monitor

        with AIProjectClient(endpoint=endpoint, credential=credential) as project:
            conn = project.telemetry.get_application_insights_connection_string()
        configure_azure_monitor(connection_string=conn)
        try:
            from azure.ai.agents.telemetry import AIAgentsInstrumentor

            AIAgentsInstrumentor().instrument()
        except Exception:  # pragma: no cover
            pass
        _rebind_tracer()
        log("STEP", "Foundry tracing enabled (Application Insights).")
        return True
    except Exception as exc:  # pragma: no cover - depends on Azure setup
        log("ERROR", f"Foundry tracing not enabled ({exc}); continuing with console logs.", logging.WARNING)
        return False


def _rebind_tracer() -> None:
    global _TRACER
    if _otel_trace is not None:
        _TRACER = _otel_trace.get_tracer("claims_triage")

"""Runtime configuration, loaded from environment variables / a .env file.

Two backends are supported:

* ``foundry`` - real Azure AI Foundry Agent Service via the ``azure-ai-agents`` SDK.
* ``mock``    - a deterministic local stand-in for the LLM, so the whole system runs
                offline (no Azure subscription, no quota). The agent definitions,
                tool schemas, tool dispatch, routine, memory and knowledge layers are
                identical in both modes; only the "model" is replaced.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional dependency
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass
class Settings:
    backend: str = field(default_factory=lambda: (_env("TRIAGE_BACKEND", "mock") or "mock").lower())

    # Azure AI Foundry
    project_endpoint: str | None = field(default_factory=lambda: _env("AZURE_AI_PROJECT_ENDPOINT"))
    model_deployment: str = field(default_factory=lambda: _env("AZURE_AI_MODEL_DEPLOYMENT", "gpt-4o-mini"))
    agent_name_prefix: str = field(default_factory=lambda: _env("TRIAGE_AGENT_PREFIX", "claims-triage"))
    enable_foundry_tracing: bool = field(
        default_factory=lambda: (_env("TRIAGE_FOUNDRY_TRACING", "false") or "false").lower() == "true"
    )

    # Behaviour
    run_timeout_s: float = field(default_factory=lambda: float(_env("TRIAGE_RUN_TIMEOUT_S", "90")))
    tool_max_retries: int = field(default_factory=lambda: int(_env("TRIAGE_TOOL_MAX_RETRIES", "2")))
    # e.g. "assess_fraud_indicators:1" -> that tool fails transiently once (demo of retry)
    fault_injection: str | None = field(default_factory=lambda: _env("TRIAGE_FAULT_INJECTION"))
    today: str | None = field(default_factory=lambda: _env("TRIAGE_TODAY"))  # freeze "today" for demos/tests

    # Paths
    data_dir: Path = field(default_factory=lambda: Path(_env("TRIAGE_DATA_DIR", str(PROJECT_ROOT / "data"))))
    knowledge_dir: Path = field(
        default_factory=lambda: Path(_env("TRIAGE_KNOWLEDGE_DIR", str(PROJECT_ROOT / "knowledge")))
    )
    memory_dir: Path = field(default_factory=lambda: Path(_env("TRIAGE_MEMORY_DIR", str(PROJECT_ROOT / ".memory"))))
    runs_dir: Path = field(default_factory=lambda: Path(_env("TRIAGE_RUNS_DIR", str(PROJECT_ROOT / "runs"))))
    log_level: str = field(default_factory=lambda: _env("TRIAGE_LOG_LEVEL", "INFO") or "INFO")

    @property
    def policies_path(self) -> Path:
        return self.data_dir / "policy_coverage.json"

    @property
    def catalog_path(self) -> Path:
        return self.data_dir / "coverage_catalog.json"

    @property
    def seed_memory_path(self) -> Path:
        return self.data_dir / "seed_memory.json"

    @property
    def foundry_state_path(self) -> Path:
        """Where created Foundry agent/vector-store IDs are cached between runs."""
        return self.memory_dir / "foundry_state.json"

    def validate(self) -> None:
        if self.backend not in {"mock", "foundry"}:
            raise ValueError(f"TRIAGE_BACKEND must be 'mock' or 'foundry', got {self.backend!r}")
        if self.backend == "foundry" and not self.project_endpoint:
            raise ValueError(
                "TRIAGE_BACKEND=foundry requires AZURE_AI_PROJECT_ENDPOINT "
                "(e.g. https://<resource>.services.ai.azure.com/api/projects/<project>)."
            )
        if self.backend == "foundry" and "<" in (self.project_endpoint or ""):
            raise ValueError("AZURE_AI_PROJECT_ENDPOINT still contains the placeholder from .env.example - "
                             "paste your project's endpoint from the Foundry portal (project Overview page).")

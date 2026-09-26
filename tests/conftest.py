import shutil
from pathlib import Path

import pytest

from claims_triage.config import PROJECT_ROOT, Settings
from claims_triage.observability import configure_logging, set_recorder

configure_logging("WARNING", color=False)


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("TRIAGE_BACKEND", "mock")
    monkeypatch.setenv("TRIAGE_TODAY", "2026-09-24")
    monkeypatch.setenv("TRIAGE_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRIAGE_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("TRIAGE_FAULT_INJECTION", raising=False)
    set_recorder(None)
    return Settings()


@pytest.fixture
def kb_copy(tmp_path, monkeypatch) -> Path:
    """A private copy of knowledge/ that a test may edit."""
    dst = tmp_path / "knowledge"
    shutil.copytree(PROJECT_ROOT / "knowledge", dst)
    monkeypatch.setenv("TRIAGE_KNOWLEDGE_DIR", str(dst))
    return dst


def data(name: str) -> str:
    return str(PROJECT_ROOT / "data" / name)

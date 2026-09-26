"""Exercises the real FoundryBackend code paths against a fake AgentsClient.

Uses the genuine azure-ai-agents model classes (SubmitToolOutputsAction,
RequiredFunctionToolCall, FunctionToolDefinition ...) so the tool-call loop, tool
registration and timeout/cancel handling are verified without an Azure subscription.
"""
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("azure.ai.agents")
from azure.ai.agents import models as m  # noqa: E402

from claims_triage.agents import AGENT_SPECS  # noqa: E402
from claims_triage.backends import AgentTimeoutError  # noqa: E402
from claims_triage.backends.foundry_backend import FoundryBackend  # noqa: E402
from claims_triage.context import TriageContext  # noqa: E402
from claims_triage.tools import build_registry  # noqa: E402
from conftest import data  # noqa: E402


class FakeClient:
    def __init__(self, hang=False):
        self.hang = hang
        self.created, self.submitted, self.cancelled = [], [], []
        self._n = 0
        self.threads = SimpleNamespace(create=lambda: SimpleNamespace(id="thread_1"))
        self.messages = SimpleNamespace(
            create=lambda **kw: None,
            get_last_message_text_by_role=lambda **kw: SimpleNamespace(
                text=SimpleNamespace(value='{"step":"ingest","claim_count":4,"flagged":[],"summary":"ok"}【4:0†source】')),
        )
        self.runs = SimpleNamespace(create=self._create, get=self._get, submit_tool_outputs=self._submit,
                                    cancel=lambda **kw: self.cancelled.append(kw))
        self.files = SimpleNamespace(upload_and_poll=lambda **kw: SimpleNamespace(id=f"file_{kw['file_path'][-8:]}"))
        self.vector_stores = SimpleNamespace(create_and_poll=lambda **kw: SimpleNamespace(id="vs_1"),
                                             get=lambda _id: SimpleNamespace(id=_id), delete=lambda _id: None)

    def create_agent(self, **kw):
        self.created.append(kw)
        self._n += 1
        return SimpleNamespace(id=f"asst_{self._n}")

    def get_agent(self, _id):
        raise KeyError(_id)

    def _create(self, **kw):
        call = m.RequiredFunctionToolCall(id="call_1", function=m.RequiredFunctionToolCallDetails(
            name="load_claims_batch", arguments=json.dumps({"source_path": data("claims.json")})))
        action = m.SubmitToolOutputsAction(submit_tool_outputs=m.SubmitToolOutputsDetails(tool_calls=[call]))
        return SimpleNamespace(id="run_1", status="in_progress" if self.hang else "requires_action",
                               required_action=None if self.hang else action)

    def _get(self, **kw):
        return SimpleNamespace(id="run_1", status="in_progress", required_action=None)

    def _submit(self, **kw):
        self.submitted.append(kw)
        return SimpleNamespace(id="run_1", status="completed", required_action=None)


def _backend(settings, client):
    b = FoundryBackend.__new__(FoundryBackend)
    b.registry, b.settings, b.client = build_registry(), settings, client
    b.agent_ids, b.state_path = {}, settings.foundry_state_path
    b.state = {"agents": {}, "vector_store_id": None, "file_ids": [], "kb_hash": None}
    return b


def test_setup_registers_function_tools_and_file_search(settings):
    client = FakeClient()
    ids = _backend(settings, client).setup(AGENT_SPECS)
    assert set(ids) == set(AGENT_SPECS)
    cov = next(c for c in client.created if c["name"].endswith("anomaly_coverage"))
    kinds = [t["type"] for t in cov["tools"]]
    assert kinds.count("function") == 3 and "file_search" in kinds
    assert cov["tool_resources"]["file_search"]["vector_store_ids"] == ["vs_1"]
    fn = next(t for t in cov["tools"] if t["type"] == "function")
    assert fn["function"]["parameters"]["required"] == ["claim_id"]


def test_run_executes_requested_tool_and_submits_output(settings):
    client = FakeClient()
    b = _backend(settings, client)
    b.setup(AGENT_SPECS)
    ctx = TriageContext.build(settings)
    res = b.run("intake_validation", "ingest", ctx)
    assert res.called("load_claims_batch") and ctx.batch is not None
    out = client.submitted[0]["tool_outputs"][0]
    assert out["tool_call_id"] == "call_1" and json.loads(out["output"])["claim_count"] == 4
    assert res.json()["claim_count"] == 4 and "【" not in res.text  # citation markers stripped
    assert ctx.session.agent_threads["intake_validation"] == "thread_1"


def test_run_timeout_cancels(settings):
    settings.run_timeout_s = 0.01
    client = FakeClient(hang=True)
    b = _backend(settings, client)
    b.setup(AGENT_SPECS)
    with pytest.raises(AgentTimeoutError):
        b.run("intake_validation", "ingest", TriageContext.build(settings))
    assert client.cancelled

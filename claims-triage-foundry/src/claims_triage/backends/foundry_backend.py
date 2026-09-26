"""Azure AI Foundry Agent Service backend (``azure-ai-agents`` SDK).

What happens here (all in code, nothing clicked in the portal):

1. ``setup``   - uploads ``knowledge/*.md`` to the project, creates a Foundry **vector store**,
                 and creates/updates the 4 agents with their instructions, JSON-schema
                 function tools and (for the Anomaly & Coverage agent) the ``file_search``
                 tool bound to that vector store. IDs are cached in
                 ``.memory/foundry_state.json`` and agents are only updated when their
                 definition hash changes (idempotent re-runs).
2. ``run``     - posts the message on the session's **thread** for that agent (one thread
                 per agent per session = short-term memory kept by Foundry), starts a run
                 and drives the tool-call loop manually: when the run is
                 ``requires_action`` we execute the requested function tools through the
                 ToolRegistry (validation, retries, logging) and submit the outputs.
                 A wall-clock timeout cancels runs that hang.
3. ``teardown``- deletes agents, vector store and uploaded files.

We drive the tool loop manually (instead of ``enable_auto_function_calls``) so every tool
call is validated, retried, logged and traced by our registry.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from ..agents import AgentSpec
from ..config import Settings
from ..observability import log, span
from ..tools import ToolRegistry
from .base import AgentBackend, AgentRunError, AgentRunResult, AgentTimeoutError

_CITATION = re.compile(r"【[^】]*】")


class FoundryBackend(AgentBackend):
    name = "foundry"

    def __init__(self, registry: ToolRegistry, settings: Settings) -> None:
        super().__init__(registry)
        self.settings = settings
        try:
            from azure.ai.agents import AgentsClient
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install the Azure SDKs: pip install -r requirements.txt") from exc
        self.credential = DefaultAzureCredential()  # az login / VS Code / env service principal / managed identity
        self.client = AgentsClient(endpoint=settings.project_endpoint, credential=self.credential)
        self.agent_ids: dict[str, str] = {}
        self.state_path: Path = settings.foundry_state_path
        self.state: dict[str, Any] = self._load_state()

    # ------------------------------------------------------------- state
    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"agents": {}, "vector_store_id": None, "file_ids": [], "kb_hash": None}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    # ------------------------------------------------------------- setup
    def _ensure_vector_store(self) -> str:
        from azure.ai.agents.models import FilePurpose

        files = sorted(self.settings.knowledge_dir.glob("*.md"))
        kb_hash = hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()
        if self.state.get("vector_store_id") and self.state.get("kb_hash") == kb_hash:
            try:
                self.client.vector_stores.get(self.state["vector_store_id"])
                return self.state["vector_store_id"]
            except Exception:
                log("STEP", "cached vector store not found; recreating")
        with span("SETUP", "foundry_vector_store", files=len(files)):
            file_ids = []
            for path in files:
                info = self.client.files.upload_and_poll(file_path=str(path), purpose=FilePurpose.AGENTS)
                file_ids.append(info.id)
                log("KNOWLEDGE", f"uploaded {path.name} -> {info.id}")
            vs = self.client.vector_stores.create_and_poll(
                file_ids=file_ids, name=f"{self.settings.agent_name_prefix}-underwriting-kb"
            )
            log("KNOWLEDGE", f"vector store ready: {vs.id} ({len(file_ids)} files)")
        old = self.state.get("vector_store_id")
        self.state.update(vector_store_id=vs.id, file_ids=file_ids, kb_hash=kb_hash)
        self._save_state()
        if old and old != vs.id:
            self._safe(lambda: self.client.vector_stores.delete(old))
        return vs.id

    def _tool_definitions(self, spec: AgentSpec, vector_store_id: str | None):
        from azure.ai.agents.models import FileSearchTool, FunctionDefinition, FunctionToolDefinition

        tools = [
            FunctionToolDefinition(
                function=FunctionDefinition(name=t.name, description=t.description, parameters=t.parameters)
            )
            for t in self.registry.for_agent(spec.name)
        ]
        resources = None
        if spec.uses_file_search and vector_store_id:
            fs = FileSearchTool(vector_store_ids=[vector_store_id])
            tools.extend(fs.definitions)
            resources = fs.resources
        return tools, resources

    def setup(self, specs: dict[str, AgentSpec]) -> dict[str, str]:
        vs_id = self._ensure_vector_store() if any(s.uses_file_search for s in specs.values()) else None
        for spec in specs.values():
            tools, resources = self._tool_definitions(spec, vs_id)
            full_name = f"{self.settings.agent_name_prefix}-{spec.name}"
            def_hash = hashlib.sha256(
                json.dumps(
                    [self.settings.model_deployment, spec.instructions, spec.temperature, vs_id,
                     [t.openai_schema() for t in self.registry.for_agent(spec.name)]],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            cached = self.state["agents"].get(spec.name, {})
            with span("SETUP", f"agent:{spec.name}") as sp:
                agent_id = cached.get("id")
                if agent_id and self._exists(agent_id):
                    if cached.get("hash") != def_hash:
                        self.client.update_agent(
                            agent_id, model=self.settings.model_deployment, name=full_name,
                            instructions=spec.instructions, tools=tools, tool_resources=resources,
                            temperature=spec.temperature,
                        )
                        sp["op"] = "updated"
                    else:
                        sp["op"] = "reused"
                else:
                    agent = self.client.create_agent(
                        model=self.settings.model_deployment, name=full_name, description=spec.display_name,
                        instructions=spec.instructions, tools=tools, tool_resources=resources,
                        temperature=spec.temperature, metadata={"app": "claims-triage", "role": spec.name},
                    )
                    agent_id = agent.id
                    sp["op"] = "created"
                log("AGENT", f"{spec.display_name}: {sp['op']} {agent_id} "
                             f"(tools: {[t.name for t in self.registry.for_agent(spec.name)]}"
                             f"{' + file_search' if spec.uses_file_search else ''})")
            self.state["agents"][spec.name] = {"id": agent_id, "hash": def_hash}
            self.agent_ids[spec.name] = agent_id
        self._save_state()
        return dict(self.agent_ids)

    def _exists(self, agent_id: str) -> bool:
        try:
            self.client.get_agent(agent_id)
            return True
        except Exception:
            return False

    # --------------------------------------------------------------- run
    def run(self, agent: str, message: str, ctx: Any) -> AgentRunResult:
        from azure.ai.agents.models import (
            MessageRole,
            RequiredFunctionToolCall,
            RunStatus,
            SubmitToolOutputsAction,
            ToolOutput,
        )

        self._maybe_inject_agent_fault(agent)
        agent_id = self.agent_ids.get(agent)
        if not agent_id:
            raise AgentRunError(f"agent '{agent}' not set up")
        session = ctx.session
        thread_id = session.agent_threads.get(agent)
        if not thread_id:
            thread_id = self._retry(lambda: self.client.threads.create()).id
            session.agent_threads[agent] = thread_id
        first_call = len(self.registry.calls)

        self._retry(lambda: self.client.messages.create(thread_id=thread_id, role="user", content=message))
        run = self._retry(lambda: self.client.runs.create(thread_id=thread_id, agent_id=agent_id))
        deadline = time.monotonic() + self.settings.run_timeout_s
        active = {RunStatus.QUEUED, RunStatus.IN_PROGRESS, RunStatus.REQUIRES_ACTION}
        while run.status in active:
            if time.monotonic() > deadline:
                self._safe(lambda: self.client.runs.cancel(thread_id=thread_id, run_id=run.id))
                raise AgentTimeoutError(f"{agent} run {run.id} exceeded {self.settings.run_timeout_s}s")
            if run.status == RunStatus.REQUIRES_ACTION and isinstance(run.required_action, SubmitToolOutputsAction):
                outputs = []
                for call in run.required_action.submit_tool_outputs.tool_calls:
                    if isinstance(call, RequiredFunctionToolCall):
                        result = self.registry.invoke(call.function.name, call.function.arguments, ctx, agent)
                        outputs.append(ToolOutput(tool_call_id=call.id, output=json.dumps(result, default=str)))
                run = self._retry(
                    lambda: self.client.runs.submit_tool_outputs(thread_id=thread_id, run_id=run.id, tool_outputs=outputs)
                )
                continue
            time.sleep(0.8)
            run = self._retry(lambda: self.client.runs.get(thread_id=thread_id, run_id=run.id))

        if run.status != RunStatus.COMPLETED:
            err = getattr(run, "last_error", None)
            raise AgentRunError(f"{agent} run {run.id} ended with status {run.status}: {err}")
        last = self.client.messages.get_last_message_text_by_role(thread_id=thread_id, role=MessageRole.AGENT)
        text = _CITATION.sub("", last.text.value) if last else ""
        return AgentRunResult(agent, text.strip(), self.registry.calls[first_call:], run.id, thread_id, "completed")

    # ----------------------------------------------------------- helpers
    def _retry(self, fn, attempts: int = 3):
        """Retry throttling / transient service errors with exponential backoff."""
        from azure.core.exceptions import HttpResponseError, ServiceRequestError

        for i in range(attempts):
            try:
                return fn()
            except (HttpResponseError, ServiceRequestError) as exc:
                status = getattr(exc, "status_code", None)
                if i == attempts - 1 or (status is not None and status not in (408, 429, 500, 502, 503, 504)):
                    raise AgentRunError(f"Foundry call failed: {exc}") from exc
                wait = 2 ** i
                log("AGENT", f"transient Foundry error ({status}); retrying in {wait}s")
                time.sleep(wait)

    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except Exception:
            pass

    def teardown(self, delete_remote: bool = False) -> None:
        if not delete_remote:
            return
        for name, info in list(self.state.get("agents", {}).items()):
            self._safe(lambda: self.client.delete_agent(info["id"]))
            log("AGENT", f"deleted agent {name} ({info['id']})")
        if self.state.get("vector_store_id"):
            self._safe(lambda: self.client.vector_stores.delete(self.state["vector_store_id"]))
        for fid in self.state.get("file_ids", []):
            self._safe(lambda: self.client.files.delete(fid))
        self.state = {"agents": {}, "vector_store_id": None, "file_ids": [], "kb_hash": None}
        self._save_state()

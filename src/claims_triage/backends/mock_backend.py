"""Deterministic offline stand-in for the Foundry-hosted model.

Why it exists: the assignment allows mocking the LLM when Foundry quota/access is not
available, as long as the SDK integration stays intact. This backend keeps every other
layer real - agents are defined from the same ``AgentSpec`` objects, the model's
"decisions" are expressed as *tool calls* that go through the same ``ToolRegistry``
(schema validation, retries, logging, tracing), and outputs follow the same JSON
contracts the real agents are instructed to produce.

What is mocked: (1) which tool to call next - a fixed plan per task type instead of model
reasoning, and (2) the prose - templates in ``narrative.py`` instead of generated text.
"""
from __future__ import annotations

import itertools
import json
from typing import Any

from ..agent_names import BRIEFING, COVERAGE, INTAKE, SUPERVISOR
from ..agents import AgentSpec
from ..observability import log
from . import narrative
from .base import AgentBackend, AgentRunError, AgentRunResult, extract_json

_ids = itertools.count(1)


class MockBackend(AgentBackend):
    name = "mock"

    def setup(self, specs: dict[str, AgentSpec]) -> dict[str, str]:
        ids = {}
        for spec in specs.values():
            ids[spec.name] = f"mock-agent-{spec.name}"
            tools = [t.name for t in self.registry.for_agent(spec.name)]
            log("AGENT", f"{spec.display_name}: registered (mock) with tools {tools}"
                         f"{' + file_search(local vector store)' if spec.uses_file_search else ''}")
        self.agent_ids = ids
        return ids

    def run(self, agent: str, message: str, ctx: Any) -> AgentRunResult:
        self._maybe_inject_agent_fault(agent)
        thread_id = ctx.session.agent_threads.setdefault(agent, f"mock-thread-{ctx.session.session_id}-{agent}")
        first = len(self.registry.calls)
        payload = extract_json(message) or {}
        task = payload.get("task")
        call = lambda tool, **args: self.registry.invoke(tool, args, ctx, agent)  # noqa: E731

        if agent == INTAKE and task == "ingest":
            r = call("load_claims_batch", source_path=payload["source_path"])
            text = json.dumps({"step": "ingest", "claim_count": r.get("claim_count", 0), "flagged": [],
                               "summary": (f"Loaded {r['claim_count']} claims from {r['source']}"
                                           + (f"; {len(r['row_errors'])} unparseable rows skipped" if r.get("row_errors") else "")
                                           ) if r["ok"] else f"Could not load the batch: {r['error']}"})
        elif agent == INTAKE and task == "validate":
            r = call("validate_claims")
            flagged = r.get("flagged", [])
            text = json.dumps({"step": "validate", "claim_count": r.get("checked", 0), "flagged": flagged,
                               "summary": f"{len(flagged)} of {r.get('checked', 0)} claims have intake issues: "
                                          f"{', '.join(flagged) or 'none'}." if r["ok"] else f"Validation failed: {r['error']}"})
        elif agent == COVERAGE and task == "assess_claim":
            cid = payload["claim_id"]
            cov = call("check_policy_coverage", claim_id=cid)
            risk = call("assess_fraud_indicators", claim_id=cid)
            text = json.dumps({"claim_id": cid, "coverage_status": cov.get("status", "error"),
                               "matched_rules": [m["rule_id"] for m in risk.get("matched_rules", [])],
                               "summary": narrative.coverage_summary(cov, risk) if cov["ok"] and risk["ok"]
                               else f"Check failed: {cov.get('error') or risk.get('error')}"})
        elif agent == COVERAGE and task == "knowledge_question":
            r = call("search_underwriting_knowledge", query=payload["question"], top_k=3)
            text = narrative.knowledge_answer(payload["question"], r.get("results", []))
        elif agent == BRIEFING and task == "brief_claim":
            cid = payload["claim_id"]
            hist = call("get_policyholder_history", policy_number=payload["policy_number"], exclude_claim_id=cid) \
                if payload.get("policy_number", "").startswith("POL-") else {"ok": True}
            rec = call("recommend_next_action", claim_id=cid)
            if not rec["ok"]:
                raise AgentRunError(f"recommend_next_action failed: {rec['error']}")
            a = ctx.assessments[rec["record_id"]].to_dict()
            text = json.dumps({"claim_id": cid, "recommended_action": rec["recommended_action"],
                               "briefing": narrative.brief_text(a, hist if hist.get("ok") else {}, rec)})
        elif agent == BRIEFING and task == "explain_claim":
            rec = call("get_claim_triage_record", claim_id=payload["claim_id"])
            if not rec["ok"]:
                text = f"I have no triage record for {payload['claim_id']} yet. Run a triage on the batch that contains it first."
            else:
                record = rec["record"]
                codes = [r["code"] for r in record.get("risk_findings", [])] or record.get("flags", [])
                codes += [f["code"] for f in record.get("coverage", {}).get("findings", [])]
                codes += [v["code"] for v in record.get("validation", [])]
                query = " ".join(dict.fromkeys(codes)) if codes else f"{record.get('claim_type', '')} auto approve"
                hits = call("search_underwriting_knowledge", query=query, top_k=2).get("results", [])
                text = narrative.explain_text(record, rec["origin"], hits, payload.get("question", ""))
        elif agent == BRIEFING and task == "policy_history":
            h = call("get_policyholder_history", policy_number=payload["policy_number"])
            text = narrative.history_text(h) if h["ok"] else f"Could not read history: {h['error']}"
        elif agent == SUPERVISOR:
            text = self._supervisor(payload, call)
        else:
            raise AgentRunError(f"mock backend has no plan for agent={agent} task={task}")
        return AgentRunResult(agent, text, self.registry.calls[first:], f"mock-run-{next(_ids)}", thread_id)

    @staticmethod
    def _supervisor(payload: dict[str, Any], call) -> str:
        msg = (payload.get("message") or "").lower()
        if payload.get("task") == "remember_note":
            r = call("remember_adjuster_note", policy_number=payload["policy_number"], note=payload["note"])
            return f"Noted for {payload['policy_number']}: \"{payload['note']}\"" if r["ok"] else f"Could not save: {r['error']}"
        if any(w in msg for w in ("summary", "overview", "status", "results", "how many")):
            r = call("get_session_overview")
            if not r.get("claims"):
                return "No triage has been run in this conversation yet. Try: triage data/claims.json"
            rows = "\n".join(f"- {c['claim_id']}: {c.get('final_action') or c.get('recommended_action')}" for c in r["claims"])
            return f"Latest run ({r.get('batch_source')}):\n{rows}\nCounts: {r['counts']}"
        return ("I can: triage a batch (\"triage data/claims.json\"), explain a claim (\"why was C2031 flagged?\"), "
                "show a policy's history (\"history for POL-5521\"), answer rule questions (\"what is the rule for theft?\"), "
                "summarise the latest run (\"summary\"), or remember a note (\"remember for POL-5521: ...\").")

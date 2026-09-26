"""Supervisor: multi-turn conversation, request routing, and ownership of the routine.

Routing is *hybrid*:

1. A deterministic router (regexes over the request + the session's focus entities)
   handles the well-understood intents. This is fast, free, testable and auditable.
2. Anything it cannot classify goes to the Supervisor **agent** (LLM) which answers
   using its read-only tools (overview, triage record, history, knowledge search).

| intent            | handled by                                  |
|-------------------|---------------------------------------------|
| triage_batch      | TriageRoutine (Intake -> Coverage -> Briefing, 2 HITL gates) |
| explain_claim     | Adjuster Briefing Agent (+ knowledge search) |
| policy_history    | Adjuster Briefing Agent (long-term memory)   |
| knowledge_question| Anomaly & Coverage Agent (knowledge base)    |
| remember_note     | Supervisor Agent -> remember_adjuster_note   |
| overview / other  | Supervisor Agent                             |
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent_names import BRIEFING, COVERAGE, SUPERVISOR
from ..agents import AGENT_SPECS
from ..backends import AgentBackend, AgentRunError, create_backend
from ..backends.narrative import ACTION_LABEL
from ..config import Settings
from ..context import TriageContext
from ..data_loader import normalize_claim_id
from ..memory import SessionMemory
from ..observability import TraceRecorder, log, set_recorder, span
from ..tools import build_registry
from .hitl import HumanGate
from .routine import TriageRoutine, task_message

CLAIM_RE = re.compile(r"\b[cC]-?\d{3,5}(?:~\d)?\b")
POLICY_RE = re.compile(r"\bPOL-\d{4}\b", re.I)
FILE_RE = re.compile(r"[\w./\\:-]+\.(?:json|csv)\b", re.I)
PRONOUN_CLAIM = re.compile(r"\b(it|that claim|this claim|that one|the claim)\b", re.I)
PRONOUN_POLICY = re.compile(r"\b(that policy|this policy|the policyholder|this policyholder|that policyholder|them|they)\b", re.I)


@dataclass
class Route:
    intent: str
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


class Supervisor:
    def __init__(self, settings: Settings | None = None, gate: HumanGate | None = None,
                 session_id: str | None = None, backend: AgentBackend | None = None) -> None:
        self.settings = settings or Settings()
        self.settings.validate()
        sessions_dir = self.settings.memory_dir / "sessions"
        session = SessionMemory.load(sessions_dir, session_id) if session_id else SessionMemory()
        self.registry = build_registry(self.settings.tool_max_retries, self.settings.fault_injection)
        self.ctx = TriageContext.build(self.settings, session)
        self.backend = backend or create_backend(self.settings, self.registry)
        self.backend.registry = self.registry
        self.gate = gate or HumanGate("interactive")
        self.recorder = TraceRecorder(run_id=f"session-{session.session_id}")
        set_recorder(self.recorder)
        with span("SETUP", "agents", backend=self.backend.name):
            self.agent_ids = self.backend.setup(AGENT_SPECS)
        self._restore_batch()

    @property
    def session(self) -> SessionMemory:
        return self.ctx.session

    def _restore_batch(self) -> None:
        """On a resumed session, reload the last batch so claim-level tools work again."""
        src = self.session.last_batch_source
        if src and Path(src).exists() and self.ctx.batch is None:
            self.registry.invoke("load_claims_batch", {"source_path": src}, self.ctx, "intake_validation")
            from ..models import ClaimAssessment, Finding

            for rid, d in self.session.assessments.items():
                if rid in self.ctx.assessments:
                    a: ClaimAssessment = self.ctx.assessments[rid]
                    a.validation = [Finding(**f) for f in d.get("validation", [])]
                    a.coverage = d.get("coverage", {})
                    a.risk_findings = [Finding(**f) for f in d.get("risk_findings", [])]
                    for k in ("features", "history", "recommended_action", "decision_basis", "briefing",
                              "final_action", "adjuster_note"):
                        setattr(a, k, d.get(k, getattr(a, k)))
            log("MEMORY", f"resumed session {self.session.session_id}: {len(self.session.turns)} turns, "
                          f"batch {Path(src).name}, focus claim {self.session.focus_claim_id}")

    # ------------------------------------------------------------ routing
    def route(self, text: str) -> Route:
        t = text.strip()
        low = t.lower()
        claims = [normalize_claim_id(c) for c in CLAIM_RE.findall(t)]
        policies = [p.upper() for p in POLICY_RE.findall(t)]
        files = FILE_RE.findall(t)
        if re.search(r"\b(triage|process|run|ingest)\b", low) and (files or "batch" in low or "claims" in low):
            return Route("triage_batch", {"source_path": files[0] if files else "data/claims.json"}, "triage keyword")
        if re.search(r"\b(remember|note that|make a note|save a note)\b", low):
            pol = policies[0] if policies else self.session.focus_policy
            note = re.sub(r"^.*?(remember|note that|make a note|save a note)\s*(for\s+POL-\d{4})?\s*[:,-]?\s*", "", t,
                          flags=re.I).strip()
            if pol and note:
                return Route("remember_note", {"policy_number": pol, "note": note}, "explicit remember request")
        if policies and re.search(r"\b(history|prior|previous|past|before|earlier)\b", low):
            return Route("policy_history", {"policy_number": policies[0]}, "policy + history keyword")
        if claims:
            return Route("explain_claim", {"claim_id": claims[0], "question": t}, "claim id mentioned")
        if re.search(r"\b(rule|rules|threshold|underwriting|fraud|indicator|pattern|policy on|guideline)\b", low):
            return Route("knowledge_question", {"question": t}, "knowledge keyword")
        if self.session.focus_policy and PRONOUN_POLICY.search(t) and re.search(r"\b(history|prior|previous|before)\b", low):
            return Route("policy_history", {"policy_number": self.session.focus_policy}, "pronoun -> focus policy")
        if self.session.focus_claim_id and (PRONOUN_CLAIM.search(t) or re.search(r"\b(why|documents?|next step)\b", low)):
            return Route("explain_claim", {"claim_id": self.session.focus_claim_id, "question": t},
                         f"follow-up resolved to focus claim {self.session.focus_claim_id}")
        return Route("supervisor", {"message": t}, "no deterministic match -> supervisor agent")

    # ------------------------------------------------------------ handling
    def handle(self, text: str) -> str:
        self.session.add("user", text)
        route = self.route(text)
        log("ROUTER", f"intent={route.intent} args={route.args} ({route.reason})")
        try:
            with span("ROUTER", route.intent, reason=route.reason):
                reply = getattr(self, f"_do_{route.intent}")(**route.args)
        except AgentRunError as exc:
            reply = f"Sorry - the {route.intent.replace('_', ' ')} request failed ({exc}). Please try again."
        self.session.add("assistant", reply, intent=route.intent)
        self.session.save(self.settings.memory_dir / "sessions")
        self.recorder.flush(self.settings.runs_dir / self.recorder.run_id)
        return reply

    def _ask(self, agent: str, instruction: str, payload: dict[str, Any]) -> str:
        res = self.backend.run(agent, task_message(instruction, payload), self.ctx)
        return res.text

    def _do_triage_batch(self, source_path: str) -> str:
        routine = TriageRoutine(self.ctx, self.backend, self.registry, self.gate)
        r = routine.run(source_path)
        self.session.last_run_id = r.run_id
        if r.state.value != "DONE":
            return f"Triage run {r.run_id} ended in {r.state.value}: {r.error}. Nothing was written to long-term memory."
        lines = [f"Triage run {r.run_id} complete ({len(r.assessments)} claims)."]
        for a in r.assessments:
            fa = a.get("final_action")
            tag = "" if fa == a.get("recommended_action") else f" (adjuster override from {a.get('recommended_action')})"
            lines.append(f"  - {a['record_id']:<9} {ACTION_LABEL.get(fa, fa)}{tag}")
        if r.degraded:
            lines.append(f"  Fallbacks used for: {', '.join(r.degraded)}")
        lines.append(f"Report: {r.report_path}")
        flagged = [a for a in r.assessments if a.get("final_action") != "auto_approve"]
        if flagged:
            self.session.focus_claim_id = flagged[0]["record_id"]
            self.session.focus_policy = flagged[0]["policy_number"]
        return "\n".join(lines)

    def _do_explain_claim(self, claim_id: str, question: str) -> str:
        self.session.focus_claim_id = claim_id
        rec = self.ctx.assessments.get(claim_id) or next(
            (a for a in self.ctx.assessments.values() if normalize_claim_id(a.claim_id) == claim_id), None)
        if rec:
            self.session.focus_policy = rec.policy_number
        else:
            stored = self.ctx.ltm.get_claim(claim_id)
            if stored:
                self.session.focus_policy = stored.get("policy_number")
        return self._ask(BRIEFING, f"Explain the triage outcome for claim {claim_id}. The adjuster asked: {question}",
                         {"task": "explain_claim", "claim_id": claim_id, "question": question})

    def _do_policy_history(self, policy_number: str) -> str:
        self.session.focus_policy = policy_number
        return self._ask(BRIEFING, f"Summarise the history on file for {policy_number}.",
                         {"task": "policy_history", "policy_number": policy_number})

    def _do_knowledge_question(self, question: str) -> str:
        return self._ask(COVERAGE, f"Answer from the underwriting knowledge base: {question}",
                         {"task": "knowledge_question", "question": question})

    def _do_remember_note(self, policy_number: str, note: str) -> str:
        self.session.focus_policy = policy_number
        return self._ask(SUPERVISOR, f"The adjuster explicitly asked you to remember this for {policy_number}: {note}",
                         {"task": "remember_note", "policy_number": policy_number, "note": note})

    def _do_supervisor(self, message: str) -> str:
        recent = [f"{t.role}: {t.content[:200]}" for t in self.session.recent(4)]
        return self._ask(SUPERVISOR, message, {"task": "chat", "message": message,
                                               "focus_claim_id": self.session.focus_claim_id,
                                               "focus_policy": self.session.focus_policy, "recent_turns": recent})

    def close(self, delete_remote: bool = False) -> None:
        self.session.save(self.settings.memory_dir / "sessions")
        self.backend.teardown(delete_remote)
        set_recorder(None)

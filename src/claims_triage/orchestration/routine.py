"""The triage routine: an explicit, inspectable state machine.

    START -> INTAKE -> VALIDATE -> GATE_VALIDATION -> COVERAGE -> BRIEFING -> GATE_REVIEW -> PERSIST -> DONE
                 \\          \\             \\                                        \\
                  +-> FAILED  +-> FAILED     +-> ABORTED                               +-> ABORTED

The LLM agents do the work *inside* a step (choose/call tools, write prose); the routine
decides the order of steps, checks each step's post-conditions (checkpoints), applies
guardrails, and owns the human gates and all writes to long-term memory. Every transition
is validated against ``TRANSITIONS`` and logged, so a run can be audited from its trace.

Failure handling (per claim, so one bad claim never sinks the batch):
* agent run fails/times out  -> retry once -> deterministic fallback (tools called directly,
  templated narrative), claim marked ``degraded``;
* agent skipped a required tool -> the routine calls it (checkpoint repair), logged;
* a tool itself fails            -> a ``tool_failure`` finding is added, which the decision
  policy maps to ``request_more_documentation`` (never to auto-approve);
* malformed input file           -> run ends in FAILED with a clear message, nothing written.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from ..agent_names import BRIEFING, COVERAGE, INTAKE
from ..backends import AgentBackend, AgentRunError, AgentRunResult
from ..backends import narrative
from ..context import TriageContext
from ..models import Finding
from ..observability import TraceRecorder, get_recorder, log, set_recorder, span
from ..tools import ToolRegistry
from .hitl import HumanGate


class State(str, Enum):
    START = "START"
    INTAKE = "INTAKE"
    VALIDATE = "VALIDATE"
    GATE_VALIDATION = "GATE_VALIDATION"
    COVERAGE = "COVERAGE"
    BRIEFING = "BRIEFING"
    GATE_REVIEW = "GATE_REVIEW"
    PERSIST = "PERSIST"
    DONE = "DONE"
    ABORTED = "ABORTED"
    FAILED = "FAILED"


TRANSITIONS: dict[State, set[State]] = {
    State.START: {State.INTAKE},
    State.INTAKE: {State.VALIDATE, State.FAILED},
    State.VALIDATE: {State.GATE_VALIDATION, State.FAILED},
    State.GATE_VALIDATION: {State.COVERAGE, State.ABORTED},
    State.COVERAGE: {State.BRIEFING, State.FAILED},
    State.BRIEFING: {State.GATE_REVIEW, State.FAILED},
    State.GATE_REVIEW: {State.PERSIST, State.ABORTED},
    State.PERSIST: {State.DONE},
}
TERMINAL = {State.DONE, State.ABORTED, State.FAILED}


def task_message(instruction: str, payload: dict[str, Any]) -> str:
    """Specialist messages = a plain-language instruction + a JSON task block."""
    return f"{instruction}\n\n```json\n{json.dumps(payload)}\n```"


@dataclass
class TriageResult:
    run_id: str
    state: State
    transitions: list[str] = field(default_factory=list)
    assessments: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    error: str | None = None
    report_path: Path | None = None
    trace_path: Path | None = None


class TriageRoutine:
    def __init__(self, ctx: TriageContext, backend: AgentBackend, registry: ToolRegistry, gate: HumanGate) -> None:
        self.ctx, self.backend, self.registry, self.gate = ctx, backend, registry, gate
        self.state = State.START
        self.result: TriageResult | None = None

    # -------------------------------------------------------------- driver
    def run(self, source_path: str) -> TriageResult:
        parent_recorder = get_recorder()  # e.g. the chat session's recorder
        recorder = TraceRecorder()
        set_recorder(recorder)
        self.state = State.START
        self.result = TriageResult(run_id=recorder.run_id, state=State.START)
        self.ctx.scratch = {"source_path": source_path}
        steps: dict[State, Callable[[], State]] = {
            State.INTAKE: self._intake,
            State.VALIDATE: self._validate,
            State.GATE_VALIDATION: self._gate_validation,
            State.COVERAGE: self._coverage,
            State.BRIEFING: self._briefing,
            State.GATE_REVIEW: self._gate_review,
            State.PERSIST: self._persist,
        }
        log("STEP", f"=== triage run {recorder.run_id} on {source_path} (backend={self.backend.name}) ===")
        self._transition(State.INTAKE)
        while self.state not in TERMINAL:
            current = self.state
            try:
                with span("STEP", current.value):
                    nxt = steps[current]()
            except Exception as exc:  # unexpected bug: fail the run cleanly, keep the trace
                log("ERROR", f"step {current.value} crashed: {type(exc).__name__}: {exc}", logging.ERROR)
                self.result.error = f"{current.value}: {type(exc).__name__}: {exc}"
                nxt = State.FAILED if State.FAILED in TRANSITIONS.get(current, set()) else State.ABORTED
            self._transition(nxt)
        self.result.state = self.state
        self.result.assessments = [a.to_dict() for a in self.ctx.assessments.values()]
        run_dir = self.ctx.settings.runs_dir / recorder.run_id
        self.result.trace_path = recorder.flush(run_dir)
        self.result.report_path = self._write_report(run_dir)
        set_recorder(parent_recorder)
        if parent_recorder is not None:
            parent_recorder.events.extend(recorder.events)
        log("STEP", f"=== run {recorder.run_id} finished: {self.state.value} (trace: {self.result.trace_path}) ===")
        return self.result

    def _transition(self, to: State) -> None:
        allowed = TRANSITIONS.get(self.state, set())
        if to not in allowed:
            raise RuntimeError(f"illegal transition {self.state.value} -> {to.value}")
        log("STEP", f"{self.state.value} -> {to.value}", transition_from=self.state.value, transition_to=to.value)
        self.result.transitions.append(f"{self.state.value}->{to.value}")
        self.state = to

    # ------------------------------------------------------ agent helper
    def _run_agent(self, agent: str, message: str, label: str) -> AgentRunResult | None:
        """Run an agent with one retry. Returns None if it still fails (caller falls back)."""
        for attempt in (1, 2):
            try:
                with span("AGENT", agent, label=label, attempt=attempt) as sp:
                    res = self.backend.run(agent, message, self.ctx)
                    sp["tool_calls"] = [c.tool for c in res.tool_calls]
                log("AGENT", f"{agent} [{label}] done; tools used: {[c.tool for c in res.tool_calls] or 'none'}")
                return res
            except AgentRunError as exc:
                log("ERROR", f"{agent} [{label}] attempt {attempt} failed: {exc}", logging.WARNING)
        return None

    def _degrade(self, record_id: str, why: str) -> None:
        if record_id not in self.result.degraded:
            self.result.degraded.append(record_id)
        log("STEP", f"{record_id}: degraded mode - {why}", logging.WARNING)

    # --------------------------------------------------------------- steps
    def _intake(self) -> State:
        src = self.ctx.scratch["source_path"]
        res = self._run_agent(INTAKE, task_message(
            "Ingest this batch of claim submissions.", {"task": "ingest", "source_path": src}), "ingest")
        if self.ctx.batch is None:  # checkpoint: a batch must be loaded
            if res is not None:
                failed = [c.result for c in res.tool_calls if c.tool == "load_claims_batch" and not c.ok]
                if failed:  # the tool ran and reported a real input problem -> stop cleanly
                    self.result.error = failed[-1]["error"]
                    log("ERROR", f"intake failed: {self.result.error}", logging.ERROR)
                    return State.FAILED
            log("STEP", "checkpoint repair: intake agent did not load the batch; calling tool directly")
            out = self.registry.invoke("load_claims_batch", {"source_path": src}, self.ctx, INTAKE)
            if not out["ok"]:
                self.result.error = out["error"]
                return State.FAILED
        if not self.ctx.batch.claims:
            self.result.error = "the batch contains no parseable claims"
            return State.FAILED
        self.ctx.session.last_batch_source = self.ctx.batch.source
        for err in self.ctx.batch.row_errors:
            log("STEP", f"row {err['row']} skipped: {err['error']}", logging.WARNING)
        log("STEP", f"checkpoint ok: {len(self.ctx.batch.claims)} claims loaded")
        return State.VALIDATE

    def _validate(self) -> State:
        self.ctx.scratch.pop("validated", None)
        res = self._run_agent(INTAKE, task_message("Validate every claim in the loaded batch.", {"task": "validate"}),
                              "validate")
        if res is None or not res.called("validate_claims"):
            log("STEP", "checkpoint repair: validate_claims not executed by agent; calling tool directly")
            out = self.registry.invoke("validate_claims", {}, self.ctx, INTAKE)
            if not out["ok"]:
                self.result.error = out["error"]
                return State.FAILED
        n_flagged = sum(1 for a in self.ctx.assessments.values() if a.validation)
        log("STEP", f"checkpoint ok: {len(self.ctx.assessments)} validated, {n_flagged} with issues")
        return State.GATE_VALIDATION

    def _gate_validation(self) -> State:
        rows = [{"record_id": a.record_id, "issues": [f.code for f in a.validation]} for a in self.ctx.assessments.values()]
        with span("GATE", "validation_review") as sp:
            decision = self.gate.review_validation(rows)
            sp.update(proceed=decision.proceed, reason=decision.reason, excluded=decision.exclude)
        log("GATE", f"gate 1 decision: {decision.reason}")
        if not decision.proceed:
            self.result.error = decision.reason
            return State.ABORTED
        for rid in decision.exclude:
            self.ctx.assessments.pop(rid, None)
            self.result.excluded.append(rid)
        return State.COVERAGE

    def _coverage(self) -> State:
        for rid, a in self.ctx.assessments.items():
            res = self._run_agent(COVERAGE, task_message(
                f"Check coverage and fraud indicators for claim {rid}.", {"task": "assess_claim", "claim_id": rid}),
                f"assess {rid}")
            if res is None:
                self._degrade(rid, "coverage agent unavailable; running checks directly")
            # checkpoint: both checks must have produced results for this claim
            for tool, done in (("check_policy_coverage", bool(a.coverage)), ("assess_fraud_indicators", bool(a.features))):
                if done:
                    continue
                if res is not None:
                    log("STEP", f"checkpoint repair: {tool} missing for {rid}; calling directly")
                out = self.registry.invoke(tool, {"claim_id": rid}, self.ctx, COVERAGE)
                if not out["ok"]:
                    a.risk_findings.append(Finding("tool_failure", f"{tool} failed: {out['error']}", "system", "medium",
                                                   self.ctx.decision_policy.finding_actions.get("tool_failure")))
                    self._degrade(rid, f"{tool} failed")
        return State.BRIEFING

    def _briefing(self) -> State:
        for rid, a in self.ctx.assessments.items():
            res = self._run_agent(BRIEFING, task_message(
                f"Write the adjuster briefing for claim {rid}.",
                {"task": "brief_claim", "claim_id": rid, "policy_number": a.policy_number}), f"brief {rid}")
            if a.recommended_action is None:  # checkpoint: decision policy must have been applied
                if res is not None:
                    log("STEP", f"checkpoint repair: recommend_next_action missing for {rid}; calling directly")
                self.registry.invoke("recommend_next_action", {"claim_id": rid}, self.ctx, BRIEFING)
            body = res.json() if res else None
            if body and body.get("briefing"):
                claimed = body.get("recommended_action")
                if claimed and claimed != a.recommended_action:  # guardrail: the LLM may not change the decision
                    log("STEP", f"guardrail: {rid} briefing said {claimed}, policy says {a.recommended_action}; "
                                "keeping policy decision", logging.WARNING)
                a.briefing = body["briefing"]
            elif res and res.text and not body:
                a.briefing = res.text  # prose instead of JSON - still useful
            else:
                if res is None:
                    self._degrade(rid, "briefing agent unavailable; using templated briefing")
                hist = self.registry.invoke("get_policyholder_history",
                                            {"policy_number": a.policy_number, "exclude_claim_id": a.claim_id},
                                            self.ctx, BRIEFING) if a.policy_number.startswith("POL-") else {}
                a.briefing = narrative.brief_text(a.to_dict(), hist if hist.get("ok") else {},
                                                  {"recommended_action": a.recommended_action, "basis": a.decision_basis})
        return State.GATE_REVIEW

    def _gate_review(self) -> State:
        rows = []
        for a in self.ctx.assessments.values():
            codes = [f.code for f in a.all_findings()]
            rows.append({"record_id": a.record_id, "action": a.recommended_action,
                         "headline": ", ".join(dict.fromkeys(codes)) or "clean"})
        with span("GATE", "adjuster_review") as sp:
            decision = self.gate.review_recommendations(rows)
            sp.update(proceed=decision.proceed, reason=decision.reason,
                      overrides={k: v[0] for k, v in decision.overrides.items()})
        log("GATE", f"gate 2 decision: {decision.reason}")
        if not decision.proceed:
            self.result.error = decision.reason
            return State.ABORTED
        for rid, a in self.ctx.assessments.items():
            if rid in decision.overrides:
                a.final_action, a.adjuster_note = decision.overrides[rid]
                log("GATE", f"{rid}: adjuster override {a.recommended_action} -> {a.final_action}")
            else:
                a.final_action = a.recommended_action
        return State.PERSIST

    def _persist(self) -> State:
        for a in self.ctx.assessments.values():
            if a.record_id != a.claim_id:  # duplicate rows (C-2037~2) are not separate claims in history
                continue
            self.ctx.ltm.record_outcome({
                "claim_id": a.claim_id, "policy_number": a.policy_number, "claim_type": a.claim_type,
                "loss_date": a.loss_date, "claim_amount": a.claim_amount,
                "recommended_action": a.recommended_action, "final_action": a.final_action,
                "flags": list(dict.fromkeys(f.code for f in a.all_findings())),
                "summary": f"{a.description}; findings: {', '.join(dict.fromkeys(f.code for f in a.all_findings())) or 'none'}",
                "adjuster_note": a.adjuster_note,
                "run_id": self.result.run_id,
            })
        s = self.ctx.session
        s.assessments = {rid: a.to_dict() for rid, a in self.ctx.assessments.items()}
        s.last_run_id = self.result.run_id
        return State.DONE

    # -------------------------------------------------------------- report
    def _write_report(self, run_dir: Path) -> Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "assessments.json").write_text(json.dumps(self.result.assessments, indent=2, default=str),
                                                  encoding="utf-8")
        r = self.result
        lines = [f"# Claims Triage Report - run {r.run_id}", "",
                 f"- Source: `{self.ctx.scratch.get('source_path')}`",
                 f"- Backend: `{self.backend.name}`",
                 f"- Final state: **{r.state.value}**" + (f" ({r.error})" if r.error else ""),
                 f"- Routine: {' | '.join(r.transitions)}",
                 f"- Degraded (fallback used): {', '.join(r.degraded) or 'none'}",
                 f"- Excluded at gate 1: {', '.join(r.excluded) or 'none'}", "",
                 "| Claim | Policy | Type | Amount | Recommended | Final (adjuster) | Findings |",
                 "|---|---|---|---|---|---|---|"]
        for a in r.assessments:
            codes = [f["code"] for f in a["validation"]] + [f["code"] for f in a["coverage"].get("findings", [])] + \
                    [f["code"] for f in a["risk_findings"]]
            lines.append(f"| {a['record_id']} | {a['policy_number']} | {a['claim_type']} | {a['claim_amount']} | "
                         f"{a.get('recommended_action') or '-'} | {a.get('final_action') or '-'} | "
                         f"{', '.join(dict.fromkeys(codes)) or 'none'} |")
        lines += ["", "## Adjuster briefings", ""]
        for a in r.assessments:
            lines += [f"### {a['record_id']}", "", a.get("briefing") or "_not briefed_", ""]
            if a.get("adjuster_note"):
                lines += [f"> Adjuster override note: {a['adjuster_note']}", ""]
        tool_counts: dict[str, int] = {}
        for c in self.registry.calls:
            tool_counts[c.tool] = tool_counts.get(c.tool, 0) + 1
        lines += ["## Tool calls", "", *[f"- `{k}` x{v}" for k, v in sorted(tool_counts.items())], ""]
        path = run_dir / "triage_report.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

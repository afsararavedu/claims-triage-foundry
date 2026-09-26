import json

import pytest

from claims_triage.backends import create_backend
from claims_triage.context import TriageContext
from claims_triage.orchestration import HumanGate, State, TriageRoutine
from claims_triage.tools import build_registry
from conftest import data

EXPECTED_PATH = ["START->INTAKE", "INTAKE->VALIDATE", "VALIDATE->GATE_VALIDATION", "GATE_VALIDATION->COVERAGE",
                 "COVERAGE->BRIEFING", "BRIEFING->GATE_REVIEW", "GATE_REVIEW->PERSIST", "PERSIST->DONE"]


def _routine(settings, gate=None, faults=None):
    reg = build_registry(fault_injection=faults)
    ctx = TriageContext.build(settings)
    backend = create_backend(settings, reg)
    from claims_triage.agents import AGENT_SPECS

    backend.setup(AGENT_SPECS)
    return TriageRoutine(ctx, backend, reg, gate or HumanGate("auto")), ctx


def test_full_cycle_follows_the_routine(settings):
    routine, ctx = _routine(settings)
    r = routine.run(data("claims.json"))
    assert r.state == State.DONE
    assert r.transitions == EXPECTED_PATH
    actions = {a["record_id"]: a["final_action"] for a in r.assessments}
    assert actions == {"C-2031": "route_to_investigator", "C-2032": "route_to_investigator",
                       "C-2033": "route_to_investigator", "C-2034": "request_more_documentation"}
    brief = next(a for a in r.assessments if a["record_id"] == "C-2031")["briefing"]
    assert "similar water damage claim C-1804" in brief  # long-term memory referenced
    assert r.report_path.exists() and r.trace_path.exists()
    kinds = {json.loads(l)["kind"] for l in r.trace_path.read_text().splitlines()}
    assert {"STEP", "AGENT", "TOOL", "GATE"} <= kinds
    assert ctx.ltm.get_claim("C-2033")["final_action"] == "route_to_investigator"


def test_abort_at_gate_one_writes_nothing(settings):
    routine, ctx = _routine(settings, HumanGate("scripted", script=["a"], output_fn=lambda *_: None))
    before = ctx.ltm.stats["claims_history"]
    r = routine.run(data("claims.json"))
    assert r.state == State.ABORTED and r.transitions[-1] == "GATE_VALIDATION->ABORTED"
    assert ctx.ltm.stats["claims_history"] == before


def test_gate_two_override_and_exclusion(settings):
    gate = HumanGate("scripted", script=["x C-2034", "o C-2032 approve verified with plumber invoice", "a"],
                     output_fn=lambda *_: None)
    routine, ctx = _routine(settings, gate)
    r = routine.run(data("claims.json"))
    assert r.excluded == ["C-2034"] and "C-2034" not in {a["record_id"] for a in r.assessments}
    c2032 = next(a for a in r.assessments if a["record_id"] == "C-2032")
    assert c2032["recommended_action"] == "route_to_investigator" and c2032["final_action"] == "auto_approve"
    assert ctx.ltm.get_claim("C-2032")["adjuster_note"] == "verified with plumber invoice"


def test_transient_tool_failure_is_retried(settings):
    routine, _ = _routine(settings, faults="assess_fraud_indicators:1")
    r = routine.run(data("claims.json"))
    assert r.state == State.DONE and not r.degraded
    call = next(c for c in routine.registry.calls if c.tool == "assess_fraud_indicators")
    assert call.ok and call.attempts == 2


def test_persistent_tool_failure_degrades_one_claim_only(settings):
    routine, _ = _routine(settings, faults="check_policy_coverage:10")  # fails on every retry for ~3 claims
    r = routine.run(data("claims.json"))
    assert r.state == State.DONE
    bad = [a for a in r.assessments if any(f["code"] == "tool_failure" for f in a["risk_findings"])]
    assert bad and all(a["recommended_action"] != "auto_approve" for a in bad)


def test_agent_timeout_falls_back_to_deterministic_briefing(settings):
    routine, _ = _routine(settings, faults="agent.adjuster_briefing:2")
    r = routine.run(data("claims.json"))
    assert r.state == State.DONE and r.degraded == ["C-2031"]
    first = next(a for a in r.assessments if a["record_id"] == "C-2031")
    assert first["briefing"] and first["recommended_action"] == "route_to_investigator"


def test_malformed_file_fails_cleanly(settings):
    routine, ctx = _routine(settings)
    r = routine.run(data("claims_malformed.json"))
    assert r.state == State.FAILED and "not valid JSON" in r.error
    assert r.report_path.exists()


def test_illegal_transition_is_rejected(settings):
    routine, _ = _routine(settings)
    routine.result = type("R", (), {"transitions": []})()
    routine.state = State.INTAKE
    with pytest.raises(RuntimeError):
        routine._transition(State.PERSIST)

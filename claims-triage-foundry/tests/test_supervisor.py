from claims_triage.config import Settings
from claims_triage.orchestration import HumanGate, Supervisor


def _sup(session_id=None):
    return Supervisor(Settings(), gate=HumanGate("auto", output_fn=lambda *_: None), session_id=session_id)


def test_routing_table(settings):
    sup = _sup()
    assert sup.route("triage this batch of claims").intent == "triage_batch"
    assert sup.route("please triage data/claims_extended.json").args == {"source_path": "data/claims_extended.json"}
    r = sup.route("why was claim C2031 flagged?")
    assert r.intent == "explain_claim" and r.args["claim_id"] == "C-2031"
    assert sup.route("history for POL-5521 please").intent == "policy_history"
    assert sup.route("what is the underwriting rule for theft?").intent == "knowledge_question"
    assert sup.route("remember for POL-5521: new pipes fitted").args["note"] == "new pipes fitted"
    assert sup.route("hello there").intent == "supervisor"


def test_multi_turn_context_and_resume(settings):
    sup = _sup()
    out = sup.handle("triage data/claims.json")
    assert "complete" in out
    assert "UW-004" in sup.handle("why was claim C2031 flagged?")
    follow = sup.route("what documents do we need for it?")
    assert follow.intent == "explain_claim" and follow.args["claim_id"] == "C-2031"
    assert "Documents to request" in sup.handle("what documents do we need for it?")
    sid = sup.session.session_id
    sup.close()
    resumed = _sup(session_id=sid)  # new process, same conversation
    assert resumed.session.focus_claim_id == "C-2031" and len(resumed.session.turns) == 6
    assert "UW-004" in resumed.handle("why was it flagged?")


def test_long_term_memory_across_separate_runs(settings):
    first = _sup()
    first.handle("triage data/claims.json")
    first.close()
    second = _sup()  # separate session: only long-term memory connects them
    second.handle("triage data/claims_batch2.json")
    brief = second.ctx.assessments["C-2050"].briefing
    assert "C-2031" in brief and "C-2032" in brief
    assert second.ctx.assessments["C-2051"].recommended_action == "auto_approve"

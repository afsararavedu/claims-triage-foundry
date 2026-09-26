from claims_triage.context import TriageContext
from claims_triage.knowledge import LocalVectorStore
from claims_triage.knowledge.rules import Rule, evaluate
from claims_triage.tools import build_registry
from conftest import data


def _assess(settings, file="claims.json"):
    ctx = TriageContext.build(settings)
    reg = build_registry()
    reg.invoke("load_claims_batch", {"source_path": data(file)}, ctx, "intake_validation")
    reg.invoke("validate_claims", {}, ctx, "intake_validation")
    out = {}
    for rid in ctx.assessments:
        cov = reg.invoke("check_policy_coverage", {"claim_id": rid}, ctx, "anomaly_coverage")
        risk = reg.invoke("assess_fraud_indicators", {"claim_id": rid}, ctx, "anomaly_coverage")
        rec = reg.invoke("recommend_next_action", {"claim_id": rid}, ctx, "adjuster_briefing")
        out[rid] = (cov["status"], sorted(m["rule_id"] for m in risk["matched_rules"]), rec["recommended_action"])
    return out


def test_sample_batch_outcomes(settings):
    r = _assess(settings)
    assert r["C-2031"] == ("covered", ["UW-004"], "route_to_investigator")  # repeat water damage (memory + batch)
    assert r["C-2033"][1] == ["UW-001", "UW-002", "UW-005", "UW-006", "UW-008"]
    assert r["C-2033"][2] == "route_to_investigator"
    assert r["C-2034"][0] == "unverifiable" and r["C-2034"][2] == "request_more_documentation"


def test_extended_batch_coverage_failures(settings):
    r = _assess(settings, "claims_extended.json")
    assert r["C-2038"][0] == "not_covered"  # collision on a homeowners policy
    assert r["C-2039"][0] == "not_covered"  # over the limit
    assert r["C-2035"] == ("covered", [], "auto_approve")
    assert r["C-2040"] == ("covered", ["UW-003"], "request_more_documentation")  # late reporting


def test_rules_come_from_knowledge_not_code(settings, kb_copy):
    """Editing a threshold in the markdown changes behaviour with no code change."""
    path = kb_copy / "underwriting_rules.md"
    path.write_text(path.read_text().replace("`days_since_inception <= 7`", "`days_since_inception <= 1`"))
    from claims_triage.config import Settings

    r = _assess(Settings())
    assert "UW-001" not in r["C-2033"][1]  # 3 days after inception no longer matches


def test_condition_parser_is_safe():
    rule = Rule("X-1", "t", "__import__('os').system('echo hi') > 1", "high", None, "", "f.md")
    res = evaluate(rule, {"claim_amount": 5})
    assert not res.matched and not res.evaluable
    rule = Rule("X-2", "t", "claim_amount >= 10 AND claim_type == theft", "high", None, "", "f.md")
    assert evaluate(rule, {"claim_amount": 10, "claim_type": "theft"}).matched
    assert not evaluate(rule, {"claim_amount": None, "claim_type": "theft"}).evaluable


def test_vector_store_retrieval_and_cache(settings, tmp_path):
    cache = tmp_path / "kb.json"
    store = LocalVectorStore(settings.knowledge_dir, cache)
    assert store.search("second water damage claim on the same policy")[0]["chunk_id"] == "UW-004"
    assert store.search("theft police report jewelry")[0]["chunk_id"] in {"UW-005", "FI-01"}
    assert cache.exists()
    again = LocalVectorStore(settings.knowledge_dir, cache)  # loaded from cache
    assert [c.chunk_id for c in again.chunks] == [c.chunk_id for c in store.chunks]

from claims_triage.context import TriageContext
from claims_triage.tools import build_registry
from conftest import data


def _validate(settings, file):
    ctx = TriageContext.build(settings)
    reg = build_registry()
    assert reg.invoke("load_claims_batch", {"source_path": data(file)}, ctx, "intake_validation")["ok"]
    out = reg.invoke("validate_claims", {}, ctx, "intake_validation")
    return {r["record_id"]: sorted({i["code"] for i in r["issues"]}) for r in out["results"]}


def test_sample_batch_flags_missing_loss_date_and_amount(settings):
    issues = _validate(settings, "claims.json")
    assert issues["C-2031"] == [] and issues["C-2032"] == [] and issues["C-2033"] == []
    assert issues["C-2034"] == ["invalid_amount", "missing_required_field"]


def test_extended_batch_covers_every_validation_rule(settings):
    issues = _validate(settings, "claims_extended.json")
    assert "invalid_policy_number" in issues["C-2036"]
    assert "date_inconsistency" in issues["C-2037"]
    assert "duplicate_claim_id" in issues["C-2037"] and "duplicate_claim_id" in issues["C-2037~2"]
    assert issues["C-2041"] == ["invalid_amount"]  # "twelve thousand"
    assert issues["C-2035"] == []


def test_csv_input_matches_json(settings):
    assert _validate(settings, "claims.csv") == _validate(settings, "claims.json")


def test_tool_rejects_bad_arguments_without_crashing(settings):
    ctx = TriageContext.build(settings)
    reg = build_registry()
    out = reg.invoke("validate_claims", '{"record_ids": 5}', ctx, "intake_validation")
    assert out["ok"] is False and "invalid arguments" in out["error"]
    out = reg.invoke("validate_claims", "{not json", ctx, "intake_validation")
    assert out["ok"] is False and "not valid JSON" in out["error"]
    out = reg.invoke("check_policy_coverage", {"claim_id": "C-1"}, ctx, "intake_validation")
    assert out["ok"] is False and "not registered" in out["error"]  # tool bound to the wrong agent

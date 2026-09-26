"""Skills of the Anomaly & Coverage Agent.

* ``check_policy_coverage``      - deterministic check against the coverage record.
* ``assess_fraud_indicators``    - computes claim features and evaluates the underwriting
                                   rules *retrieved from the knowledge base* (not the prompt).
* ``search_underwriting_knowledge`` - semantic retrieval over the knowledge base, used for
                                   grounded explanations and ad-hoc rule questions.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..agent_names import BRIEFING, COVERAGE, SUPERVISOR
from ..context import TriageContext
from ..data_loader import normalize_claim_id
from ..knowledge.rules import evaluate
from ..models import Finding, parse_date
from ..observability import log
from .common import resolve_claim
from .registry import ToolSpec


def check_policy_coverage(ctx: TriageContext, claim_id: str) -> dict[str, Any]:
    claim, assessment = resolve_claim(ctx, claim_id)
    policy = ctx.policies.get(claim.policy_number)
    findings: list[Finding] = []
    checks: dict[str, Any] = {}
    if policy is None:
        findings.append(Finding("coverage_unverifiable", f"no coverage record for policy '{claim.policy_number}'",
                                "coverage", "medium"))
        status = "unverifiable"
    else:
        covered_perils = ctx.catalog.get(policy.coverage_type, [])
        peril_ok = claim.claim_type in covered_perils
        checks["peril"] = {"claim_type": claim.claim_type, "coverage_type": policy.coverage_type, "covered": peril_ok}
        if not peril_ok:
            findings.append(Finding("peril_not_covered",
                                    f"'{claim.claim_type}' is not covered by a {policy.coverage_type} policy "
                                    f"(covers: {', '.join(covered_perils)})", "coverage", "high"))
        loss = claim.loss_date
        period = {"active_from": str(policy.active_from), "active_to": str(policy.active_to), "loss_date": str(loss) if loss else None}
        if loss is None:
            period["within_period"] = None
            note = ""
            if claim.report_date and claim.report_date > policy.active_to:
                note = f"; note the report date {claim.report_date} is after the policy expired on {policy.active_to}"
            findings.append(Finding("coverage_unverifiable", f"loss date missing - cannot confirm the loss is within "
                                    f"the policy period {policy.active_from}..{policy.active_to}{note}", "coverage", "medium"))
        else:
            within = policy.active_from <= loss <= policy.active_to
            period["within_period"] = within
            if not within:
                findings.append(Finding("loss_outside_policy_period",
                                        f"loss date {loss} is outside the policy period "
                                        f"{policy.active_from}..{policy.active_to}", "coverage", "high"))
        checks["period"] = period
        amount = claim.amount
        limit_check = {"claim_amount": amount, "policy_limit": policy.policy_limit}
        if amount is None or amount <= 0:
            limit_check["within_limit"] = None
            findings.append(Finding("coverage_unverifiable", "claim amount missing/invalid - cannot check the policy limit",
                                    "coverage", "medium"))
        else:
            limit_check["within_limit"] = amount <= policy.policy_limit
            if amount > policy.policy_limit:
                findings.append(Finding("exceeds_policy_limit",
                                        f"claim amount {amount:,.0f} exceeds the policy limit {policy.policy_limit:,.0f}",
                                        "coverage", "high"))
        checks["limit"] = limit_check
        if any(f.code != "coverage_unverifiable" for f in findings):
            status = "not_covered"
        elif findings:
            status = "unverifiable"
        else:
            status = "covered"
    for f in findings:
        f.action = ctx.decision_policy.finding_actions.get(f.code)
    assessment.coverage = {"status": status, "checks": checks, "findings": [f.to_dict() for f in findings]}
    return {"record_id": claim.record_id, **assessment.coverage}


def compute_features(ctx: TriageContext, record_id: str) -> dict[str, Any]:
    claim, _ = resolve_claim(ctx, record_id)
    policy = ctx.policies.get(claim.policy_number)
    loss, report, amount = claim.loss_date, claim.report_date, claim.amount
    features: dict[str, Any] = {
        "claim_type": claim.claim_type or None,
        "claim_amount": amount if amount and amount > 0 else None,
        "report_lag_days": (report - loss).days if loss and report else None,
        "days_since_inception": (loss - policy.active_from).days if policy and loss else None,
        "days_until_expiry": (policy.active_to - loss).days if policy and loss else None,
        "amount_to_limit_ratio": round(amount / policy.policy_limit, 4) if policy and amount and amount > 0 else None,
        "is_round_amount": (1 if amount and amount > 0 and amount % 1000 == 0 else 0) if amount else None,
    }
    priors = prior_claims(ctx, claim.policy_number, claim.claim_id, loss)
    features["prior_claims_12m"] = len(priors) if loss else None
    features["prior_similar_claims_12m"] = (
        len([p for p in priors if p["claim_type"] == claim.claim_type]) if loss else None
    )
    features["_prior_claims"] = priors
    return features


def prior_claims(ctx: TriageContext, policy_number: str, claim_id: str, loss) -> list[dict[str, Any]]:
    """Earlier claims on the same policy within 365 days before ``loss``.

    Sources: long-term memory (previous sessions) + earlier claims in the current batch.
    """
    if not loss or not policy_number:
        return []
    window_start = loss - timedelta(days=365)
    me = normalize_claim_id(claim_id)
    out: dict[str, dict[str, Any]] = {}
    for rec in ctx.ltm.history_for_policy(policy_number, exclude_claim_id=claim_id):
        d = parse_date(rec.get("loss_date"))
        if d and window_start <= d < loss:
            out[normalize_claim_id(rec["claim_id"])] = {
                "claim_id": rec["claim_id"], "claim_type": rec.get("claim_type"), "loss_date": str(d),
                "claim_amount": rec.get("claim_amount"), "outcome": rec.get("final_action") or rec.get("recommended_action"),
                "flags": rec.get("flags", []), "summary": rec.get("summary"), "origin": "long_term_memory",
            }
    if ctx.batch:
        for other in ctx.batch.claims:
            key = normalize_claim_id(other.claim_id)
            if key == me or other.policy_number != policy_number or key in out:
                continue
            d = other.loss_date
            if d and window_start <= d < loss:
                out[key] = {"claim_id": other.claim_id, "claim_type": other.claim_type, "loss_date": str(d),
                            "claim_amount": other.amount, "outcome": "in current batch", "origin": "current_batch"}
    return sorted(out.values(), key=lambda r: r["loss_date"])


def assess_fraud_indicators(ctx: TriageContext, claim_id: str) -> dict[str, Any]:
    claim, assessment = resolve_claim(ctx, claim_id)
    features = compute_features(ctx, claim.record_id)
    priors = features.pop("_prior_claims")
    matched, not_evaluable = [], []
    findings: list[Finding] = []
    for rule in ctx.rules:  # rules were parsed from the knowledge base, not from code/prompt
        res = evaluate(rule, features)
        if res.matched:
            matched.append({"rule_id": rule.rule_id, "title": rule.title, "severity": rule.severity,
                            "action": rule.action, "evidence": res.detail, "citation": rule.citation})
            findings.append(Finding(rule.rule_id, f"{rule.title}: {res.detail}", "knowledge", rule.severity,
                                    rule.action, rule.citation))
        elif not res.evaluable:
            not_evaluable.append({"rule_id": rule.rule_id, "reason": res.detail})
    # Retrieve narrative fraud patterns related to what matched, for grounded explanations.
    query = " ".join([claim.claim_type.replace("_", " "), claim.description] + [m["rule_id"] for m in matched])
    patterns = ctx.kb.search(query, top_k=2, source="fraud_indicators.md")
    log("KNOWLEDGE", f"{claim.record_id}: {len(matched)} rule(s) matched "
                     f"{[m['rule_id'] for m in matched]}; patterns {[p['chunk_id'] for p in patterns]}")
    assessment.features = features
    assessment.risk_findings = findings
    assessment.history = priors
    return {
        "record_id": claim.record_id,
        "features": features,
        "matched_rules": matched,
        "rules_not_evaluable": not_evaluable,
        "prior_claims_12m": priors,
        "related_patterns": [{"chunk_id": p["chunk_id"], "title": p["title"], "source": p["source"]} for p in patterns],
    }


def search_underwriting_knowledge(ctx: TriageContext, query: str, top_k: int = 3) -> dict[str, Any]:
    hits = ctx.kb.search(query, top_k=top_k)
    log("KNOWLEDGE", f"search {query!r} -> {[h['chunk_id'] for h in hits]}")
    return {"query": query, "results": hits}


TOOLS = [
    ToolSpec(
        name="check_policy_coverage",
        description="Cross-check one claim against the policyholder's coverage record: is the claim type (peril) "
                    "covered by the policy's coverage type, is the loss date inside the active policy period, and is "
                    "the amount within the policy limit.",
        parameters={
            "type": "object",
            "properties": {"claim_id": {"type": "string", "description": "Claim id or record id, e.g. C-2031"}},
            "required": ["claim_id"],
            "additionalProperties": False,
        },
        handler=check_policy_coverage,
        agents=(COVERAGE,),
    ),
    ToolSpec(
        name="assess_fraud_indicators",
        description="Compute risk features for a claim (days since inception, amount/limit ratio, report lag, prior "
                    "similar claims from long-term memory) and evaluate the underwriting rules retrieved from the "
                    "knowledge base. Returns matched rules with citations.",
        parameters={
            "type": "object",
            "properties": {"claim_id": {"type": "string", "description": "Claim id or record id"}},
            "required": ["claim_id"],
            "additionalProperties": False,
        },
        handler=assess_fraud_indicators,
        agents=(COVERAGE,),
    ),
    ToolSpec(
        name="search_underwriting_knowledge",
        description="Semantic search over the underwriting rules, fraud-indicator patterns and decision policy "
                    "knowledge base. Use it to ground explanations and answer rule questions.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 8, "default": 3},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=search_underwriting_knowledge,
        agents=(COVERAGE, BRIEFING, SUPERVISOR),
    ),
]

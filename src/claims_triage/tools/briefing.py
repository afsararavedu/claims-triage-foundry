"""Skills of the Adjuster Briefing Agent and the Supervisor."""
from __future__ import annotations

from collections import Counter
from typing import Any

from ..agent_names import BRIEFING, SUPERVISOR
from ..context import TriageContext
from ..data_loader import normalize_claim_id
from ..models import Action
from .common import resolve_claim
from .registry import ToolError, ToolSpec


def get_policyholder_history(ctx: TriageContext, policy_number: str, exclude_claim_id: str | None = None) -> dict[str, Any]:
    history = ctx.ltm.history_for_policy(policy_number, exclude_claim_id=exclude_claim_id)
    notes = ctx.ltm.notes_for_policy(policy_number)
    in_batch = []
    if ctx.batch:
        ex = normalize_claim_id(exclude_claim_id) if exclude_claim_id else None
        in_batch = [
            {"claim_id": c.claim_id, "claim_type": c.claim_type, "loss_date": c.raw.get("loss_date"),
             "claim_amount": c.raw.get("claim_amount")}
            for c in ctx.batch.claims
            if c.policy_number == policy_number and normalize_claim_id(c.claim_id) != ex
        ]
    return {
        "policy_number": policy_number,
        "prior_claims_from_memory": history,
        "adjuster_notes": notes,
        "other_claims_in_current_batch": in_batch,
    }


def recommend_next_action(ctx: TriageContext, claim_id: str) -> dict[str, Any]:
    """Apply the decision policy (from the knowledge base) to all findings for a claim."""
    claim, a = resolve_claim(ctx, claim_id)
    policy = ctx.decision_policy
    findings = a.all_findings()
    actionable = [f for f in findings if f.action]
    basis: list[str] = []
    if actionable:
        top = max(actionable, key=lambda f: policy.rank(f.action))
        action = top.action
        basis = list(dict.fromkeys(
            f"{f.code} -> {f.action}" for f in sorted(actionable, key=lambda f: -policy.rank(f.action))
        ))
    else:
        amount = claim.amount
        if amount is None or amount <= 0:
            action, basis = Action.REQUEST_DOCS.value, ["amount unknown -> request_more_documentation"]
        elif amount <= policy.auto_approve_ceiling:
            action = Action.AUTO_APPROVE.value
            basis = [f"no actionable findings and amount {amount:,.0f} <= auto-approve ceiling "
                     f"{policy.auto_approve_ceiling:,.0f}"]
        else:
            action = Action.REQUEST_DOCS.value
            basis = [f"amount {amount:,.0f} above auto-approve ceiling {policy.auto_approve_ceiling:,.0f}"]
    informational = [f"{f.code} (informational)" for f in findings if not f.action]
    a.recommended_action = action
    a.decision_basis = basis + informational
    return {
        "record_id": claim.record_id,
        "recommended_action": action,
        "basis": a.decision_basis,
        "policy_source": f"{policy.source} (precedence: {' > '.join(policy.precedence)})",
    }


def get_claim_triage_record(ctx: TriageContext, claim_id: str) -> dict[str, Any]:
    key = normalize_claim_id(claim_id)
    for rid, a in ctx.assessments.items():
        if rid == claim_id or normalize_claim_id(a.claim_id) == key:
            return {"origin": "current_session", "record": a.to_dict()}
    for rid, a in ctx.session.assessments.items():
        if rid == claim_id or normalize_claim_id(a.get("claim_id", "")) == key:
            return {"origin": "session_memory", "record": a}
    rec = ctx.ltm.get_claim(claim_id)
    if rec:
        return {"origin": "long_term_memory", "record": rec}
    raise ToolError(f"no triage record found for claim '{claim_id}'")


def get_session_overview(ctx: TriageContext) -> dict[str, Any]:
    items = [a.to_dict() for a in ctx.assessments.values()] or list(ctx.session.assessments.values())
    counts = Counter((i.get("final_action") or i.get("recommended_action") or "pending") for i in items)
    return {
        "batch_source": ctx.session.last_batch_source,
        "run_id": ctx.session.last_run_id,
        "claims": [{"claim_id": i["claim_id"], "record_id": i["record_id"],
                    "recommended_action": i.get("recommended_action"), "final_action": i.get("final_action")}
                   for i in items],
        "counts": dict(counts),
        "long_term_memory": ctx.ltm.stats,
    }


def remember_adjuster_note(ctx: TriageContext, policy_number: str, note: str) -> dict[str, Any]:
    if policy_number not in ctx.policies:
        raise ToolError(f"unknown policy '{policy_number}'")
    return {"saved": ctx.ltm.add_note(policy_number, note)}


TOOLS = [
    ToolSpec(
        name="get_policyholder_history",
        description="Read long-term memory for a policy: prior triaged claims (from earlier sessions), adjuster notes, "
                    "and other claims on the same policy in the current batch.",
        parameters={
            "type": "object",
            "properties": {
                "policy_number": {"type": "string", "pattern": "^POL-\\d{4}$"},
                "exclude_claim_id": {"type": "string", "description": "Claim to leave out (the one being briefed)"},
            },
            "required": ["policy_number"],
            "additionalProperties": False,
        },
        handler=get_policyholder_history,
        agents=(BRIEFING, SUPERVISOR),
    ),
    ToolSpec(
        name="recommend_next_action",
        description="Apply the decision policy from the knowledge base to all validation, coverage and fraud-indicator "
                    "findings for a claim. Returns auto_approve, request_more_documentation or route_to_investigator "
                    "with the basis. The briefing must use this action.",
        parameters={
            "type": "object",
            "properties": {"claim_id": {"type": "string"}},
            "required": ["claim_id"],
            "additionalProperties": False,
        },
        handler=recommend_next_action,
        agents=(BRIEFING,),
    ),
    ToolSpec(
        name="get_claim_triage_record",
        description="Fetch everything known about a claim's triage (validation issues, coverage checks, matched rules, "
                    "recommendation, adjuster decision) from the current session or long-term memory.",
        parameters={
            "type": "object",
            "properties": {"claim_id": {"type": "string"}},
            "required": ["claim_id"],
            "additionalProperties": False,
        },
        handler=get_claim_triage_record,
        agents=(BRIEFING, SUPERVISOR),
    ),
    ToolSpec(
        name="get_session_overview",
        description="Summary of the latest triage run in this conversation: claims, recommended/final actions, counts.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=get_session_overview,
        agents=(SUPERVISOR,),
    ),
    ToolSpec(
        name="remember_adjuster_note",
        description="Persist a note about a policyholder to long-term memory when the adjuster explicitly asks.",
        parameters={
            "type": "object",
            "properties": {
                "policy_number": {"type": "string", "pattern": "^POL-\\d{4}$"},
                "note": {"type": "string", "minLength": 3, "maxLength": 500},
            },
            "required": ["policy_number", "note"],
            "additionalProperties": False,
        },
        handler=remember_adjuster_note,
        agents=(SUPERVISOR,),
    ),
]

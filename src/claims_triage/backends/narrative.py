"""Template-based natural-language generation.

Used by the mock backend in place of the LLM, and by the routine as a *degraded-mode*
fallback when a real agent run fails or times out, so the adjuster still gets a briefing
built from the (deterministic) tool outputs.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..models import parse_amount, parse_date

ACTION_LABEL = {
    "auto_approve": "AUTO-APPROVE",
    "request_more_documentation": "REQUEST MORE DOCUMENTATION",
    "route_to_investigator": "ROUTE TO INVESTIGATOR",
}

DOCS_BY_CODE = {
    "missing_required_field": "the missing claim fields",
    "invalid_amount": "a valid claimed amount with an itemised estimate",
    "invalid_date": "correctly formatted loss/report dates",
    "date_inconsistency": "confirmation of the actual loss date",
    "invalid_policy_number": "the correct policy number",
    "unknown_policy": "the correct policy number",
    "coverage_unverifiable": "the details needed to verify coverage (loss date / amount)",
    "UW-003": "dated photos and contemporaneous evidence of the loss",
    "UW-005": "a police report number and proof of ownership (receipts/appraisals)",
    "UW-006": "an itemised proof-of-loss statement and an independent repair estimate",
    "UW-007": "evidence of the loss date",
    "UW-001": "proof of prior insurance and how/when the policy was bought",
    "UW-002": "an itemised list of the loss with replacement values",
    "UW-004": "the repair invoice for the earlier claim and a cause-of-loss report (e.g. plumber's report)",
    "UW-009": "a list of all losses in the last 12 months",
    "peril_not_covered": "confirmation of which policy the loss should be filed under",
    "loss_outside_policy_period": "evidence of the loss date",
    "exceeds_policy_limit": "an itemised estimate supporting the amount above the limit",
    "duplicate_claim_id": "confirmation whether this is a resubmission of the same loss",
    "tool_failure": "none - an automated check failed; review the claim manually",
}


def docs_for(codes: list[str]) -> list[str]:
    out: list[str] = []
    for c in codes:
        d = DOCS_BY_CODE.get(c)
        if d and d not in out:
            out.append(d)
    return out


def money(v: Any) -> str:
    a = parse_amount(v)
    return f"${a:,.0f}" if a is not None else f"{v!r}"


def months_between(earlier: str | None, later: date | str | None) -> str:
    e, l = parse_date(earlier), parse_date(later)
    if not e or not l:
        return ""
    months = round((l - e).days / 30.44)
    return "less than a month" if months < 1 else f"~{months} month{'s' if months != 1 else ''}"


def brief_text(a: dict[str, Any], history: dict[str, Any], rec: dict[str, Any]) -> str:
    ctype = (a.get("claim_type") or "unknown").replace("_", " ")
    parts = [
        f"{a['claim_id']}: {ctype} claim for {money(a.get('claim_amount'))} on {a.get('policy_number') or 'unknown policy'}"
        f" (loss {a.get('loss_date') or 'not provided'}, reported {a.get('report_date') or 'not provided'}) - "
        f"\"{a.get('description', '')}\"."
    ]
    val = a.get("validation", [])
    if val:
        parts.append("Intake issues: " + "; ".join(v["message"] for v in val) + ".")
    cov = a.get("coverage", {})
    status = cov.get("status")
    if status == "covered":
        lim = cov.get("checks", {}).get("limit", {})
        ratio = ""
        if lim.get("claim_amount") and lim.get("policy_limit"):
            ratio = f" ({lim['claim_amount'] / lim['policy_limit']:.0%} of the {money(lim['policy_limit'])} limit)"
        parts.append(f"Coverage: peril covered, loss inside the policy period, amount within limit{ratio}.")
    elif status:
        parts.append(f"Coverage {status.replace('_', ' ')}: " + "; ".join(f["message"] for f in cov.get("findings", [])) + ".")
    risks = [r for r in a.get("risk_findings", [])]
    if risks:
        parts.append("Risk signals from the underwriting knowledge base: "
                     + "; ".join(f"{r['code']} {r['message'].split(':')[0]} [{r['severity']}]" for r in risks) + ".")
    # Memory: prior claims and notes
    mem_bits = []
    this_loss = a.get("loss_date")
    priors = history.get("prior_claims_from_memory", [])
    similar = sorted((p for p in priors if p.get("claim_type") == a.get("claim_type")),
                     key=lambda p: p.get("loss_date") or "")
    others = [p for p in priors if p.get("claim_type") != a.get("claim_type")]
    for p in similar[-3:]:
        gap = months_between(p.get("loss_date"), this_loss)
        earlier = parse_date(p.get("loss_date")) and parse_date(this_loss) and parse_date(p.get("loss_date")) < parse_date(this_loss)
        when = f"{gap} earlier" if gap and earlier else f"on {p.get('loss_date')}"
        outcome = p.get("final_action") or p.get("recommended_action")
        mem_bits.append(
            f"this policyholder had a similar {ctype} claim {p['claim_id']} {when} "
            f"({'flagged; ' if p.get('flags') else ''}outcome: {outcome})"
        )
    if others:
        mem_bits.append(f"{len(others)} other claim(s) of different types on file "
                        f"({', '.join(p['claim_id'] for p in others[-4:])})")
    for other in history.get("other_claims_in_current_batch", []):
        gap = months_between(other.get("loss_date"), this_loss)
        if other.get("claim_type") == a.get("claim_type") and gap and parse_date(other.get("loss_date")) < parse_date(this_loss) \
                and other["claim_id"] not in {p["claim_id"] for p in priors}:
            mem_bits.append(f"a similar {ctype} claim {other['claim_id']} from {gap} earlier is in this same batch")
    if mem_bits:
        parts.append("Memory: " + "; ".join(mem_bits) + ".")
    for n in history.get("adjuster_notes", []):
        parts.append(f"Adjuster note on file ({n.get('recorded_at', '')[:10]}): {n['note']}")
    action = rec.get("recommended_action")
    docs = docs_for([f.get("code") for f in [*val, *cov.get("findings", []), *risks]])
    nxt = f"Recommended action: {ACTION_LABEL.get(action, action)}"
    basis = [b for b in rec.get("basis", []) if "informational" not in b]
    if basis:
        nxt += f" (basis: {', '.join(basis[:3])})"
    parts.append(nxt + ".")
    if action != "auto_approve" and docs:
        parts.append("Documents to request: " + "; ".join(docs) + ".")
    return " ".join(parts)


def coverage_summary(cov: dict[str, Any], risk: dict[str, Any]) -> str:
    rules = [m["rule_id"] for m in risk.get("matched_rules", [])]
    msg = f"Coverage {cov.get('status', 'unknown')}."
    if cov.get("findings"):
        msg += " " + "; ".join(f["message"] for f in cov["findings"]) + "."
    msg += f" Matched rules: {', '.join(rules) if rules else 'none'}."
    if risk.get("related_patterns"):
        msg += " Related patterns: " + ", ".join(p["chunk_id"] for p in risk["related_patterns"]) + "."
    return msg


def explain_text(record: dict[str, Any], origin: str, kb_hits: list[dict[str, Any]], question: str = "") -> str:
    cid = record.get("claim_id")
    action = record.get("final_action") or record.get("recommended_action")
    lines = [f"{cid} ({origin.replace('_', ' ')}): recommendation {ACTION_LABEL.get(record.get('recommended_action'), record.get('recommended_action'))}"
             + (f", adjuster decision {ACTION_LABEL.get(record.get('final_action'), record.get('final_action'))}" if record.get("final_action") else "")
             + "."]
    reasons = []
    for v in record.get("validation", []):
        reasons.append(f"- Intake: {v['message']} ({v['code']})")
    for f in record.get("coverage", {}).get("findings", []):
        reasons.append(f"- Coverage: {f['message']} ({f['code']})")
    for r in record.get("risk_findings", []):
        reasons.append(f"- {r['code']}: {r['message']} [severity {r['severity']}, source {r.get('citation')}]")
    for flag in record.get("flags", []) if not reasons else []:
        reasons.append(f"- {flag}")
    if record.get("history"):
        reasons.append("- Prior claims considered: " + ", ".join(
            f"{h['claim_id']} ({h.get('claim_type')}, {h.get('loss_date')}, {h.get('origin', '').replace('_', ' ')})"
            for h in record["history"]))
    if not reasons:
        reasons.append("- No validation, coverage or fraud-indicator findings." if action == "auto_approve"
                       else f"- Summary on file: {record.get('summary', 'n/a')}")
    lines.extend(reasons)
    codes = [v["code"] for v in record.get("validation", [])] + \
        [f["code"] for f in record.get("coverage", {}).get("findings", [])] + \
        [r["code"] for r in record.get("risk_findings", [])] + list(record.get("flags", []))
    docs = docs_for(codes)
    if docs and action != "auto_approve":
        doc_line = "Documents to request: " + "; ".join(docs) + "."
        if re.search(r"\b(document|docs|paperwork|evidence|need)\b", question, re.I):
            lines.insert(1, doc_line)  # the adjuster asked about documents: answer that first
        else:
            lines.append(doc_line)
    if kb_hits:
        lines.append("Grounding from the knowledge base:")
        for h in kb_hits:
            text = " ".join(h["text"].split())
            rat = text.split("**Rationale:**")[-1].strip() if "**Rationale:**" in text else text
            lines.append(f"  [{h['chunk_id']} - {h['source']}] {rat[:260]}{'...' if len(rat) > 260 else ''}")
    if record.get("adjuster_note"):
        lines.append(f"Adjuster note: {record['adjuster_note']}")
    return "\n".join(lines)


def history_text(h: dict[str, Any]) -> str:
    pn = h["policy_number"]
    lines = [f"History for {pn}:"]
    mem = h.get("prior_claims_from_memory", [])
    if mem:
        for p in mem:
            lines.append(f"- {p['claim_id']} {p.get('claim_type')} {money(p.get('claim_amount'))} loss {p.get('loss_date')}: "
                         f"{p.get('final_action') or p.get('recommended_action')} - {_clip(p.get('summary', ''), 110)}")
    else:
        lines.append("- No prior claims in long-term memory.")
    for n in h.get("adjuster_notes", []):
        lines.append(f"- Note ({n.get('recorded_at', '')[:10]}): {n['note']}")
    others = h.get("other_claims_in_current_batch", [])
    if others:
        lines.append("- In the current batch: " + ", ".join(f"{o['claim_id']} ({o['claim_type']})" for o in others))
    return "\n".join(lines)


def knowledge_answer(question: str, hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "I could not find anything in the underwriting knowledge base about that."
    lines = [f"From the knowledge base (question: \"{question}\"):"]
    for h in hits:
        text = " ".join(h["text"].split())
        lines.append(f"- [{h['chunk_id']}] {h['title']}: {text[:300]}{'...' if len(text) > 300 else ''}")
    return "\n".join(lines)


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 3] + "..."

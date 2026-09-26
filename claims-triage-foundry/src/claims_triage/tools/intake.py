"""Skills of the Intake & Validation Agent."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from ..agent_names import INTAKE
from ..context import TriageContext
from ..data_loader import MalformedInputError, load_claims, normalize_claim_id
from ..models import REQUIRED_FIELDS, ClaimAssessment, Finding, parse_amount, parse_date
from .registry import ToolError, ToolSpec

POLICY_FORMAT = re.compile(r"^POL-\d{4}$")


def load_claims_batch(ctx: TriageContext, source_path: str) -> dict[str, Any]:
    path = Path(source_path)
    if not path.is_absolute() and not path.exists():
        candidate = ctx.settings.data_dir / path.name
        path = candidate if candidate.exists() else path
    try:
        batch = load_claims(path)
    except MalformedInputError as exc:
        raise ToolError(str(exc)) from exc
    ctx.batch = batch
    ctx.assessments = {
        c.record_id: ClaimAssessment(
            record_id=c.record_id,
            claim_id=c.claim_id,
            policy_number=c.policy_number,
            claim_type=c.claim_type,
            claim_amount=c.raw.get("claim_amount"),
            loss_date=c.raw.get("loss_date") or None,
            report_date=c.raw.get("report_date") or None,
            description=c.description,
        )
        for c in batch.claims
    }
    return {
        "source": str(path),
        "claim_count": len(batch.claims),
        "record_ids": [c.record_id for c in batch.claims],
        "row_errors": batch.row_errors,
    }


def validate_claims(ctx: TriageContext, record_ids: list[str] | None = None) -> dict[str, Any]:
    if ctx.batch is None:
        raise ToolError("no batch loaded - call load_claims_batch first")
    id_counts = Counter(normalize_claim_id(c.claim_id) for c in ctx.batch.claims if c.claim_id)
    targets = [c for c in ctx.batch.claims if not record_ids or c.record_id in record_ids]
    results = []
    for claim in targets:
        issues: list[Finding] = []
        raw = claim.raw
        for f in REQUIRED_FIELDS:
            if raw.get(f) in (None, "") or (isinstance(raw.get(f), str) and not raw[f].strip()):
                issues.append(Finding("missing_required_field", f"'{f}' is missing or empty", "intake", "medium"))
        amount_raw = raw.get("claim_amount")
        if amount_raw not in (None, ""):
            amount = parse_amount(amount_raw)
            if amount is None:
                issues.append(Finding("invalid_amount", f"claim_amount {amount_raw!r} is not a number", "intake"))
            elif amount <= 0:
                issues.append(Finding("invalid_amount", f"claim_amount must be > 0 (got {amount_raw})", "intake"))
        for f in ("loss_date", "report_date"):
            if raw.get(f) not in (None, "") and parse_date(raw.get(f)) is None:
                issues.append(Finding("invalid_date", f"{f} {raw[f]!r} is not an ISO date (YYYY-MM-DD)", "intake"))
        loss, report = claim.loss_date, claim.report_date
        if loss and report and loss > report:
            issues.append(Finding("date_inconsistency", f"loss_date {loss} is after report_date {report}", "intake"))
        for label, d in (("loss_date", loss), ("report_date", report)):
            if d and d > ctx.today:
                issues.append(Finding("future_date", f"{label} {d} is in the future", "intake"))
        pn = claim.policy_number
        if pn and not POLICY_FORMAT.match(pn):
            issues.append(Finding("invalid_policy_number", f"'{pn}' does not match format POL-####", "intake", "high"))
        elif pn and pn not in ctx.policies:
            issues.append(Finding("unknown_policy", f"policy {pn} not found in coverage records", "intake", "high"))
        if claim.claim_id and id_counts[normalize_claim_id(claim.claim_id)] > 1:
            issues.append(Finding("duplicate_claim_id",
                                  f"claim_id {claim.claim_id} appears {id_counts[normalize_claim_id(claim.claim_id)]} "
                                  "times in this batch", "intake", "high"))
        for i in issues:
            i.action = ctx.decision_policy.finding_actions.get(i.code)
        ctx.assessments[claim.record_id].validation = issues
        results.append({"record_id": claim.record_id, "valid": not issues, "issues": [i.to_dict() for i in issues]})
    flagged = [r["record_id"] for r in results if not r["valid"]]
    return {"checked": len(results), "flagged": flagged, "results": results}


TOOLS = [
    ToolSpec(
        name="load_claims_batch",
        description="Load a batch of claim submissions from a JSON or CSV file. Returns record ids and any rows "
                    "that could not be parsed. Must be called before validate_claims.",
        parameters={
            "type": "object",
            "properties": {"source_path": {"type": "string", "description": "Path to the claims .json or .csv file"}},
            "required": ["source_path"],
            "additionalProperties": False,
        },
        handler=load_claims_batch,
        agents=(INTAKE,),
    ),
    ToolSpec(
        name="validate_claims",
        description="Validate loaded claims: missing required fields, invalid/non-positive amounts, bad or "
                    "inconsistent dates (loss after report, future dates), invalid or unknown policy numbers and "
                    "duplicate claim ids. Returns per-claim issues.",
        parameters={
            "type": "object",
            "properties": {
                "record_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional subset of record ids; omit to validate the whole batch",
                }
            },
            "additionalProperties": False,
        },
        handler=validate_claims,
        agents=(INTAKE,),
    ),
]

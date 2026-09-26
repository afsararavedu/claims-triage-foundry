from __future__ import annotations

from ..context import TriageContext
from ..data_loader import normalize_claim_id
from ..models import Claim, ClaimAssessment
from .registry import ToolError


def resolve_claim(ctx: TriageContext, claim_id: str) -> tuple[Claim, ClaimAssessment]:
    if ctx.batch is None:
        raise ToolError("no batch loaded in this session")
    exact = ctx.batch.by_record_id(claim_id) or ctx.batch.by_record_id(normalize_claim_id(claim_id))
    matches = [exact] if exact else ctx.batch.find(claim_id)
    if not matches:
        raise ToolError(f"claim '{claim_id}' is not in the current batch")
    claim = matches[0]
    return claim, ctx.assessments[claim.record_id]

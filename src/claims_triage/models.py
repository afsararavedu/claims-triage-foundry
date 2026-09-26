"""Domain models shared by tools, agents and the routine."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any


class Action(str, Enum):
    AUTO_APPROVE = "auto_approve"
    REQUEST_DOCS = "request_more_documentation"
    INVESTIGATE = "route_to_investigator"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


REQUIRED_FIELDS = (
    "claim_id",
    "policy_number",
    "claim_type",
    "loss_date",
    "report_date",
    "claim_amount",
    "description",
)


@dataclass
class Claim:
    """A claim as submitted. Values are kept raw; validation decides what is usable."""

    record_id: str  # unique within a batch (duplicates get a ``~2`` suffix)
    raw: dict[str, Any]
    source_row: int

    @property
    def claim_id(self) -> str:
        return str(self.raw.get("claim_id") or "").strip()

    @property
    def policy_number(self) -> str:
        return str(self.raw.get("policy_number") or "").strip()

    @property
    def claim_type(self) -> str:
        return str(self.raw.get("claim_type") or "").strip().lower()

    @property
    def description(self) -> str:
        return str(self.raw.get("description") or "").strip()

    @property
    def loss_date(self) -> date | None:
        return parse_date(self.raw.get("loss_date"))

    @property
    def report_date(self) -> date | None:
        return parse_date(self.raw.get("report_date"))

    @property
    def amount(self) -> float | None:
        return parse_amount(self.raw.get("claim_amount"))


@dataclass
class Policy:
    policy_number: str
    coverage_type: str
    policy_limit: float
    active_from: date
    active_to: date

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Policy":
        af, at = parse_date(d["active_from"]), parse_date(d["active_to"])
        if af is None or at is None:
            raise ValueError(f"Policy {d.get('policy_number')} has invalid active dates")
        return cls(d["policy_number"], d["coverage_type"], float(d["policy_limit"]), af, at)


@dataclass
class Finding:
    """One problem or signal about a claim, produced by any specialist."""

    code: str  # e.g. missing_required_field, peril_not_covered, UW-001
    message: str
    source: str  # intake | coverage | knowledge | system
    severity: str = Severity.MEDIUM.value
    action: str | None = None  # the action this finding pushes toward (from knowledge)
    citation: str | None = None  # knowledge file + rule id when grounded

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class ClaimAssessment:
    """Everything the routine knows about one claim, accumulated step by step."""

    record_id: str
    claim_id: str
    policy_number: str
    claim_type: str
    claim_amount: Any
    loss_date: str | None
    report_date: str | None
    description: str
    validation: list[Finding] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    risk_findings: list[Finding] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    recommended_action: str | None = None
    decision_basis: list[str] = field(default_factory=list)
    briefing: str | None = None
    final_action: str | None = None  # set by the human at the review gate
    adjuster_note: str | None = None

    @property
    def is_valid(self) -> bool:
        return not any(f.severity in (Severity.HIGH.value, Severity.MEDIUM.value) for f in self.validation)

    def all_findings(self) -> list[Finding]:
        cov = [Finding(**f) for f in self.coverage.get("findings", [])]
        return [*self.validation, *cov, *self.risk_findings]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["validation"] = [f.to_dict() for f in self.validation]
        d["risk_findings"] = [f.to_dict() for f in self.risk_findings]
        return d


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def parse_amount(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None

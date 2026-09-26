"""Turn knowledge-base chunks into executable rules - without ``eval``.

Rules live in ``knowledge/underwriting_rules.md``. Each has a ``Condition`` line such as
``claim_type == theft AND claim_amount >= 10000``. This module parses that line with a
tiny grammar (``feature op value [AND feature op value ...]``) and evaluates it against the
features computed for a claim. Unknown features or unparsable conditions are reported,
never silently treated as a match.

The decision policy (action precedence, auto-approve ceiling, finding->action table) is
parsed from ``knowledge/decision_policy.md`` the same way.
"""
from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .vector_store import Chunk, LocalVectorStore

_FIELD = re.compile(r"-\s+\*\*(?P<key>[^:*]+):\*\*\s*(?P<val>.+)")
_CLAUSE = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*(<=|>=|==|!=|<|>)\s*([A-Za-z0-9_.\-]+)\s*$")
_OPS = {
    "<=": operator.le,
    ">=": operator.ge,
    "<": operator.lt,
    ">": operator.gt,
    "==": operator.eq,
    "!=": operator.ne,
}


class RuleParseError(ValueError):
    pass


@dataclass
class Rule:
    rule_id: str
    title: str
    condition: str
    severity: str
    action: str | None
    rationale: str
    source: str

    @property
    def citation(self) -> str:
        return f"{self.source}#{self.rule_id}"


@dataclass
class RuleResult:
    rule: Rule
    matched: bool
    evaluable: bool
    detail: str


def _fields(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = _FIELD.match(line.strip())
        if m:
            out[m.group("key").strip().lower()] = m.group("val").strip().strip("`").strip()
    return out


def rule_from_chunk(chunk: Chunk) -> Rule:
    f = _fields(chunk.text)
    if "condition" not in f:
        raise RuleParseError(f"{chunk.chunk_id}: missing Condition")
    title = chunk.title.split("—", 1)[-1].strip() if "—" in chunk.title else chunk.title
    action = f.get("action", "none")
    return Rule(
        rule_id=chunk.chunk_id,
        title=title,
        condition=f["condition"],
        severity=f.get("severity", "medium").lower(),
        action=None if action.lower() == "none" else action,
        rationale=f.get("rationale", ""),
        source=chunk.source,
    )


def load_rules(store: LocalVectorStore, source: str = "underwriting_rules.md") -> list[Rule]:
    return [rule_from_chunk(c) for c in store.by_source(source)]


def _coerce(raw: str) -> Any:
    try:
        return float(raw)
    except ValueError:
        return raw.lower()


def evaluate(rule: Rule, features: dict[str, Any]) -> RuleResult:
    clauses = [c for c in re.split(r"\s+AND\s+", rule.condition) if c.strip()]
    details = []
    for clause in clauses:
        m = _CLAUSE.match(clause)
        if not m:
            return RuleResult(rule, False, False, f"unparsable clause '{clause}'")
        name, op, raw = m.groups()
        if name not in features:
            return RuleResult(rule, False, False, f"unknown feature '{name}'")
        actual = features[name]
        if actual is None:
            return RuleResult(rule, False, False, f"'{name}' unavailable for this claim")
        expected = _coerce(raw)
        if isinstance(expected, float) and not isinstance(actual, (int, float)):
            return RuleResult(rule, False, False, f"'{name}' is not numeric")
        if isinstance(expected, str):
            actual = str(actual).lower()
        if not _OPS[op](actual, expected):
            return RuleResult(rule, False, True, f"{name}={actual} fails {op} {raw}")
        details.append(f"{name}={actual} {op} {raw}")
    return RuleResult(rule, True, True, "; ".join(details))


# ------------------------------------------------------------------ decision policy
@dataclass
class DecisionPolicy:
    precedence: list[str]
    auto_approve_ceiling: float
    finding_actions: dict[str, str]
    source: str = "decision_policy.md"

    def rank(self, action: str | None) -> int:
        """Higher rank wins. Unknown/None actions rank lowest."""
        if action in self.precedence:
            return len(self.precedence) - self.precedence.index(action)
        return 0


def load_decision_policy(path: Path) -> DecisionPolicy:
    text = Path(path).read_text(encoding="utf-8")
    f = _fields(text)
    try:
        precedence = [p.strip() for p in f["action precedence"].split(">")]
        ceiling = float(f["auto-approve ceiling"])
    except (KeyError, ValueError) as exc:
        raise RuleParseError(f"decision_policy.md is missing a required setting: {exc}") from exc
    table: dict[str, str] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"[a-z_]+", cells[0]) and cells[0] != "finding_code":
            table[cells[0]] = cells[1]
    return DecisionPolicy(precedence, ceiling, table)

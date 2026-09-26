"""Loading claim batches and reference data, with graceful handling of bad input."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Claim, Policy


class MalformedInputError(Exception):
    """Raised when a claims file cannot be parsed at all (e.g. invalid JSON)."""


@dataclass
class ClaimBatch:
    source: str
    claims: list[Claim] = field(default_factory=list)
    row_errors: list[dict[str, Any]] = field(default_factory=list)  # rows skipped at parse time

    def by_record_id(self, record_id: str) -> Claim | None:
        return next((c for c in self.claims if c.record_id == record_id), None)

    def find(self, claim_id: str) -> list[Claim]:
        key = normalize_claim_id(claim_id)
        return [c for c in self.claims if normalize_claim_id(c.claim_id) == key or c.record_id == claim_id]


def normalize_claim_id(value: str) -> str:
    """``C2031``, ``c-2031`` and ``C-2031`` all refer to the same claim."""
    v = (value or "").strip().upper().replace("_", "-")
    if v.startswith("C") and not v.startswith("C-") and v[1:].isdigit():
        v = f"C-{v[1:]}"
    return v


def load_claims(path: str | Path) -> ClaimBatch:
    path = Path(path)
    if not path.exists():
        raise MalformedInputError(f"Claims file not found: {path}")
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            rows = json.loads(path.read_text(encoding="utf-8"))
        elif suffix == ".csv":
            with path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        else:
            raise MalformedInputError(f"Unsupported claims file type '{suffix}' (use .json or .csv)")
    except json.JSONDecodeError as exc:
        raise MalformedInputError(f"{path.name} is not valid JSON (line {exc.lineno}, col {exc.colno}): {exc.msg}") from exc
    except UnicodeDecodeError as exc:
        raise MalformedInputError(f"{path.name} is not UTF-8 text: {exc}") from exc

    if isinstance(rows, dict) and "claims" in rows:
        rows = rows["claims"]
    if not isinstance(rows, list):
        raise MalformedInputError(f"{path.name} must contain a list of claim objects")

    batch = ClaimBatch(source=str(path))
    seen: dict[str, int] = {}
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            batch.row_errors.append({"row": idx, "error": f"row is {type(row).__name__}, expected an object"})
            continue
        cid = normalize_claim_id(str(row.get("claim_id") or "")) or f"ROW-{idx}"
        seen[cid] = seen.get(cid, 0) + 1
        record_id = cid if seen[cid] == 1 else f"{cid}~{seen[cid]}"
        batch.claims.append(Claim(record_id=record_id, raw=dict(row), source_row=idx))
    return batch


def load_policies(path: str | Path) -> dict[str, Policy]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return {r["policy_number"]: Policy.from_dict(r) for r in rows}


def load_catalog(path: str | Path) -> dict[str, list[str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: [p.lower() for p in v] for k, v in data.items() if not k.startswith("_")}

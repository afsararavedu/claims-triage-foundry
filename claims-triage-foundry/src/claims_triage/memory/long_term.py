"""Long-term memory: a JSON file that persists across separate runs/sessions.

Stores
    * ``claims_history``  - one record per triaged claim (recommended + human-final action,
                            flags, one-line summary). Feeds "prior similar claims" features
                            and the "this policyholder had a similar claim..." briefing.
    * ``adjuster_notes``  - free-text notes an adjuster asked the assistant to remember.

Writes are atomic (temp file + ``os.replace``) so a crash never leaves a half-written file,
and they only happen *after* the human review gate - the agents can read memory but
cannot write triage outcomes on their own.

Production alternative: Azure Cosmos DB / Azure AI Search index keyed by policy number,
or Foundry's managed memory store. The interface below (``history_for_policy``,
``record_outcome``, ``add_note``) is what the tools depend on, so the backing store can be
swapped without touching the agents.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..data_loader import normalize_claim_id
from ..observability import log


class LongTermMemory:
    def __init__(self, path: Path, seed_path: Path | None = None) -> None:
        self.path = Path(path)
        self.seed_path = seed_path
        if not self.path.exists():
            self.reset()
        self._data = self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Graceful degradation: keep a copy of the corrupt file, start fresh.
            backup = self.path.with_suffix(".corrupt.json")
            shutil.copy(self.path, backup)
            log("ERROR", f"Long-term memory unreadable ({exc}); backed up to {backup.name} and reset.")
            self.reset()
            data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("claims_history", [])
        data.setdefault("adjuster_notes", [])
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self.path)

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.seed_path and Path(self.seed_path).exists():
            shutil.copy(self.seed_path, self.path)
        else:
            self.path.write_text(json.dumps({"claims_history": [], "adjuster_notes": []}), encoding="utf-8")
        self._data = json.loads(self.path.read_text(encoding="utf-8"))

    # --------------------------------------------------------------- reads
    def history_for_policy(self, policy_number: str, exclude_claim_id: str | None = None) -> list[dict]:
        excl = normalize_claim_id(exclude_claim_id) if exclude_claim_id else None
        return [
            r
            for r in self._data["claims_history"]
            if r.get("policy_number") == policy_number and normalize_claim_id(r.get("claim_id", "")) != excl
        ]

    def get_claim(self, claim_id: str) -> dict | None:
        key = normalize_claim_id(claim_id)
        matches = [r for r in self._data["claims_history"] if normalize_claim_id(r.get("claim_id", "")) == key]
        return matches[-1] if matches else None

    def notes_for_policy(self, policy_number: str) -> list[dict]:
        return [n for n in self._data["adjuster_notes"] if n.get("policy_number") == policy_number]

    @property
    def stats(self) -> dict[str, int]:
        return {"claims_history": len(self._data["claims_history"]), "adjuster_notes": len(self._data["adjuster_notes"])}

    # -------------------------------------------------------------- writes
    def record_outcome(self, record: dict[str, Any]) -> None:
        record = {**record, "recorded_at": _now(), "source": record.get("source", "triage")}
        key = normalize_claim_id(record["claim_id"])
        # Upsert by claim id: re-triaging a claim replaces its previous outcome.
        self._data["claims_history"] = [
            r for r in self._data["claims_history"] if normalize_claim_id(r.get("claim_id", "")) != key
        ] + [record]
        self._save()
        log("MEMORY", f"long-term memory <- outcome for {record['claim_id']} ({record.get('final_action')})")

    def add_note(self, policy_number: str, note: str) -> dict:
        entry = {"policy_number": policy_number, "note": note, "recorded_at": _now(), "source": "chat"}
        self._data["adjuster_notes"].append(entry)
        self._save()
        log("MEMORY", f"long-term memory <- note for {policy_number}")
        return entry


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

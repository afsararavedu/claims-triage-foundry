"""Human-in-the-loop (HITL) gates.

Two checkpoints in every triage cycle:

* **Gate 1 - after validation**: the handler sees which claims failed intake checks and
  chooses to continue, exclude specific claims from this run, or abort.
* **Gate 2 - adjuster review**: the handler sees every recommendation and accepts them,
  overrides individual ones (with a note), or rejects the run. Nothing is written to
  long-term memory until this gate passes, and ``auto_approve`` is only ever a
  *recommendation* - a human confirms it here.

Modes: ``interactive`` (terminal prompts), ``auto`` (accept everything - CI/tests/demos),
``scripted`` (pre-recorded answers - tests).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..backends.narrative import ACTION_LABEL
from ..observability import log

ACTION_ALIASES = {
    "approve": "auto_approve", "auto_approve": "auto_approve", "a": "auto_approve",
    "docs": "request_more_documentation", "request_more_documentation": "request_more_documentation", "d": "request_more_documentation",
    "investigate": "route_to_investigator", "route_to_investigator": "route_to_investigator", "i": "route_to_investigator",
}


@dataclass
class GateDecision:
    proceed: bool
    exclude: list[str] = field(default_factory=list)
    overrides: dict[str, tuple[str, str]] = field(default_factory=dict)  # record_id -> (action, note)
    reason: str = ""


class HumanGate:
    def __init__(self, mode: str = "interactive", script: Iterable[str] | None = None,
                 input_fn: Callable[[str], str] = input, output_fn: Callable[[str], None] = print) -> None:
        if mode not in {"interactive", "auto", "scripted"}:
            raise ValueError(f"unknown HITL mode {mode}")
        self.mode = mode
        self._script = list(script or [])
        self._input = input_fn
        self._out = output_fn

    def _ask(self, prompt: str) -> str:
        if self.mode == "scripted":
            answer = self._script.pop(0) if self._script else "c"
            self._out(f"{prompt}{answer}   (scripted)")
            return answer
        try:
            return self._input(prompt).strip()
        except EOFError:  # non-interactive stdin: behave safely (abort rather than approve)
            return "abort"

    # ------------------------------------------------------------ gate 1
    def review_validation(self, rows: list[dict]) -> GateDecision:
        flagged = [r for r in rows if r["issues"]]
        self._out("\n=== HITL GATE 1: intake validation review ===")
        for r in rows:
            mark = "!!" if r["issues"] else "ok"
            self._out(f"  [{mark}] {r['record_id']:<10} {', '.join(r['issues']) or 'no issues'}")
        if self.mode == "auto":
            log("GATE", "gate 1 auto-continued (HITL mode=auto)")
            return GateDecision(True, reason="auto")
        valid_ids = {r["record_id"] for r in rows}
        while True:
            ans = self._ask(f"{len(flagged)} flagged. [c]ontinue all / x <ids> exclude from this run / [a]bort: ")
            low = ans.lower()
            if low in ("", "c", "continue", "y", "yes"):
                return GateDecision(True, reason="continue")
            if low in ("a", "abort", "n", "no"):
                return GateDecision(False, reason="aborted by handler")
            if low.startswith("x "):
                ids = [i.strip().upper() for i in ans[2:].replace(",", " ").split()]
                unknown = [i for i in ids if i not in valid_ids]
                if unknown:
                    self._out(f"  unknown record ids: {unknown}")
                    continue
                return GateDecision(True, exclude=ids, reason=f"excluded {ids}")
            self._out("  please answer c, a, or x <ids>")

    # ------------------------------------------------------------ gate 2
    def review_recommendations(self, rows: list[dict]) -> GateDecision:
        self._out("\n=== HITL GATE 2: adjuster review of recommendations ===")
        for r in rows:
            self._out(f"  {r['record_id']:<10} {ACTION_LABEL.get(r['action'], r['action']):<28} {r['headline']}")
        if self.mode == "auto":
            log("GATE", "gate 2 auto-accepted (HITL mode=auto)")
            return GateDecision(True, reason="auto")
        valid_ids = {r["record_id"] for r in rows}
        overrides: dict[str, tuple[str, str]] = {}
        while True:
            ans = self._ask("[a]ccept / o <id> <approve|docs|investigate> [note] to override / [r]eject run: ")
            low = ans.lower()
            if low in ("", "a", "accept", "y", "yes"):
                return GateDecision(True, overrides=overrides, reason="accepted" + (" with overrides" if overrides else ""))
            if low in ("r", "reject", "abort", "n", "no"):
                return GateDecision(False, reason="rejected by adjuster")
            parts = ans.split(maxsplit=3)
            if len(parts) >= 3 and parts[0].lower() in ("o", "override"):
                rid, act = parts[1].upper(), ACTION_ALIASES.get(parts[2].lower())
                if rid not in valid_ids or not act:
                    self._out("  usage: o C-2031 investigate optional note")
                    continue
                note = parts[3] if len(parts) > 3 else "overridden at review gate"
                overrides[rid] = (act, note)
                self._out(f"  override recorded: {rid} -> {ACTION_LABEL[act]} ({note})")
                continue
            self._out("  please answer a, r, or o <id> <action> [note]")

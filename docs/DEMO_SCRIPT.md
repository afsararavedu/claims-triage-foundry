# Screen-share demo script (about 15 minutes)

Open the folder in VS Code. Use a split view: code on the left, terminal on the right.

## 0. Setup (before the call)
```powershell
.\scripts\setup.ps1 ; .\.venv\Scripts\Activate.ps1
python -m claims_triage reset-memory
```
Put `TRIAGE_TODAY=2026-09-24` in `.env` so the date checks are reproducible.

## 1. Architecture (3 min)
Show `docs/ARCHITECTURE.md` (the diagrams render in VS Code's Markdown preview) and give the
one-line summary: *"Agents reason, tools compute, the routine owns the order and the gates."*

## 2. Agents and skills in code (2 min)
* `src/claims_triage/agents/definitions.py`: the 4 agents and their instructions.
* `python -m claims_triage agents`: tools and JSON schemas per agent.
* `backends/foundry_backend.py`: `setup()` shows `create_agent`, `FunctionToolDefinition`,
  `FileSearchTool` and the vector store; `run()` shows the `requires_action` →
  `submit_tool_outputs` loop and the timeout/cancel path.

## 3. Live run with gates (4 min)
```
python -m claims_triage chat
handler> triage data/claims.json
```
* At Gate 1, point out C-2034 (missing loss date, amount 0) and answer `c`.
* Walk through the log: `[AGENT]` / `[TOOL]` / `[KNOWLEDGE]` lines per claim.
* At Gate 2, override one claim: `o C-2032 approve plumber invoice verified`, then `a`.
* Open `runs/<id>/triage_report.md`.

## 4. Multi-turn and memory (3 min)
```
handler> why was claim C2031 flagged?          # ID normalised; UW-004 + C-1804 from long-term memory + C-2032 from the batch
handler> what documents do we need for it?     # "it" resolved from session focus
handler> history for POL-5521
handler> what is the rule for high value theft?
handler> remember for POL-9981: rider confirmed by agent
handler> exit
python -m claims_triage chat --session <id>      # resume: thread memory
handler> why was it flagged?
python -m claims_triage --hitl auto triage data/claims_batch2.json   # new run: C-2050 flagged using earlier outcomes
```

## 5. Knowledge is data (1 min)
In `knowledge/underwriting_rules.md`, change UW-001 to `days_since_inception <= 1` and
re-run the triage: C-2033 no longer matches UW-001. Revert the change afterwards.

## 6. Failure handling (2 min)
```powershell
$env:TRIAGE_FAULT_INJECTION="assess_fraud_indicators:1,agent.adjuster_briefing:2"
python -m claims_triage --hitl auto triage
Remove-Item Env:TRIAGE_FAULT_INJECTION
python -m claims_triage --hitl auto triage data/claims_malformed.json
```
Show the retry line, the degraded claim in the report, and the clean FAILED state.

## 7. Tests (30 s)
`pytest -q` runs 26 tests.

---

## Likely questions and short answers

**Why not let the supervisor LLM orchestrate everything (Connected Agents)?**
The brief requires an explicit, traceable routine with gates. An LLM-chosen order can skip
steps. Here, the LLM works inside steps and the routine enforces the order and checks
post-conditions. Connected Agents would suit the free-form Q&A path (ALTERNATIVES §2).

**How do you stop hallucinated decisions?**
Facts come from tools. The action comes from `recommend_next_action`, which applies the
decision policy from the knowledge base. If the briefing text disagrees, the guardrail keeps
the policy action. A human confirms everything at Gate 2.

**Where is knowledge grounding, exactly?**
The rules are in `knowledge/*.md`. They are indexed in a vector store (local TF-IDF, plus a
Foundry vector store with `file_search`), parsed into conditions, and evaluated per claim.
Explanations cite the rule ID and the source file. Changing the markdown changes behaviour.

**Short-term vs long-term memory?**
Short-term: the session transcript, focus entities and one Foundry thread per agent,
resumable with `--session`. Long-term: a JSON store of outcomes and notes that persists
across runs, seeded with history. It feeds the repeat-peril feature and the briefing.

**What did you mock?**
Only the model's tool choice and prose, and only in mock mode. See README §5. The Foundry
path is unit-tested with real SDK model classes against a fake client.

**How would you productionise it?**
See the end of WALKTHROUGH.md: evaluations, managed identity and Key Vault, Cosmos DB or
AI Search memory, async approvals, AI Search retrieval, and Container Apps hosting.

**Scaling to 10k claims?**
Run coverage and briefing per claim in parallel (the steps are already per-claim and
isolated). Batch the deterministic checks without the LLM and only brief flagged claims.
Use a queue (Service Bus) with Durable Functions for the gates.

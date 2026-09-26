# Multi-Agent Insurance Claims Triage Assistant (Azure AI Foundry)

A supervisor + 3 specialist agents system that triages a batch of property/auto claims:

| Agent | Job | Skills (function tools, JSON schema) | Knowledge / memory |
|---|---|---|---|
| **Supervisor** | Routes requests, runs the triage routine, multi-turn chat | `get_session_overview`, `get_claim_triage_record`, `get_policyholder_history`, `search_underwriting_knowledge`, `remember_adjuster_note` | session (thread) memory, long-term memory |
| **Intake & Validation** | Loads the batch, flags missing fields, bad/inconsistent dates, invalid policy numbers, duplicate IDs | `load_claims_batch`, `validate_claims` | – |
| **Anomaly & Coverage** | Coverage check (peril, period, limit) + fraud indicators grounded in the knowledge base | `check_policy_coverage`, `assess_fraud_indicators`, `search_underwriting_knowledge` + Foundry `file_search` | **vector store** over `knowledge/*.md` |
| **Adjuster Briefing** | Plain-language briefing + recommended action, referencing prior claims | `get_policyholder_history`, `recommend_next_action`, `get_claim_triage_record`, `search_underwriting_knowledge` | **long-term memory** (JSON store) |

The triage cycle is an explicit state machine with two human-in-the-loop gates:

```
START → INTAKE → VALIDATE → [GATE 1: handler reviews intake issues] → COVERAGE → BRIEFING
      → [GATE 2: adjuster accepts / overrides] → PERSIST (long-term memory) → DONE
```

All data is **synthetic**.

---

## 1. Quick start (runs offline in ~2 minutes – no Azure needed)

Requirements: **Python 3.10+** (3.11/3.12 recommended), Git optional, VS Code recommended.

**Windows (PowerShell)**
```powershell
cd claims-triage-foundry
.\scripts\setup.ps1              # creates .venv, installs deps, copies .env, runs tests
.\.venv\Scripts\Activate.ps1
python -m claims_triage demo     # scripted end-to-end walkthrough
python -m claims_triage chat     # interactive chat with HITL gates
```

**macOS / Linux**
```bash
cd claims-triage-foundry
bash scripts/setup.sh
source .venv/bin/activate
python -m claims_triage demo
python -m claims_triage chat
```

Manual setup if you prefer: `python -m venv .venv`, activate it, `pip install -r requirements.txt`, `pip install -e .`, `cp .env.example .env`, `pytest`.

By default `TRIAGE_BACKEND=mock`: the LLM is replaced by a deterministic stand-in, **everything else is real** (agent definitions, tool schemas, tool dispatch, routine, HITL, memory, retrieval, tracing). See [§5](#5-what-is-mocked-and-why).

## 2. Commands

| Command | What it does |
|---|---|
| `python -m claims_triage demo` | Scripted multi-turn session: triage → "why was C2031 flagged?" → follow-up "what documents do we need for it?" → policy history → rule question → remember a note → summary |
| `python -m claims_triage chat [--session ID]` | Interactive chat. Gates prompt you. `--session` resumes an earlier conversation (thread memory) |
| `python -m claims_triage triage data/claims_extended.json` | One triage cycle on any JSON/CSV batch |
| `python -m claims_triage ask "why was C-2033 flagged?" --session ID` | One question in an existing session |
| `python -m claims_triage agents` | Prints every agent's instructions and every tool's JSON schema |
| `python -m claims_triage reset-memory` | Restores long-term memory from `data/seed_memory.json` |
| `python -m claims_triage --backend foundry foundry-cleanup` | Deletes the agents, vector store and files created in Foundry |

Global flags: `--backend mock|foundry`, `--hitl interactive|auto`, `--otel-console` (print OpenTelemetry spans), `--today YYYY-MM-DD`.

Example chat inputs: `triage data/claims.json`, `why was claim C2031 flagged?`, `what documents do we need for it?`, `history for POL-5521`, `what is the rule for high-value theft?`, `remember for POL-9981: rider confirmed`, `summary`.

At **Gate 1** answer `c` (continue), `x C-2034` (exclude a claim from this run) or `a` (abort).
At **Gate 2** answer `a` (accept), `o C-2032 approve verified invoice` (override, repeatable) or `r` (reject – nothing saved).

### Data sets
| File | Purpose |
|---|---|
| `data/claims.json` / `claims.csv` | The 4 sample claims from the assignment |
| `data/claims_extended.json` | + invalid policy number, loss-after-report, duplicate ID, peril not covered, over limit, late report, non-numeric amount, clean auto-approve |
| `data/claims_batch2.json` | A later batch – shows long-term memory from the earlier run being used |
| `data/claims_malformed.json` | Broken JSON – shows graceful failure |
| `data/policy_coverage.json`, `coverage_catalog.json` | Coverage records + which perils each product covers |
| `data/seed_memory.json` | Synthetic history (e.g. an earlier flagged water-damage claim on POL-5521) |
| `knowledge/*.md` | Underwriting rules, fraud-indicator patterns, decision policy (retrieval source) |

### Failure-mode demos
```bash
# a tool fails transiently once -> retried with backoff
TRIAGE_FAULT_INJECTION=assess_fraud_indicators:1 python -m claims_triage --hitl auto triage
# the briefing agent times out twice -> claim C-2031 briefed in degraded (deterministic) mode
TRIAGE_FAULT_INJECTION=agent.adjuster_briefing:2 python -m claims_triage --hitl auto triage
# malformed input -> run ends in FAILED with a clear message, nothing written to memory
python -m claims_triage --hitl auto triage data/claims_malformed.json
```
(PowerShell: `$env:TRIAGE_FAULT_INJECTION="assess_fraud_indicators:1"` then run the command.)

### Outputs
Each run writes `runs/<run_id>/triage_report.md` (adjuster-facing report), `assessments.json` and `trace.jsonl` (every step, gate, agent run and tool call with timings). Long-term memory lives in `.memory/long_term_memory.json`; sessions in `.memory/sessions/`.

## 3. Running against Azure AI Foundry

1. **Create a Foundry project** at <https://ai.azure.com> (or reuse one).
2. **Deploy a chat model that supports tool calling** in the project, e.g. `gpt-4o-mini` or `gpt-4.1-mini`. Note the *deployment name*.
3. **Permissions**: your user needs the **Azure AI User** role (or higher) on the Foundry resource/project.
4. **Sign in**: install the Azure CLI and run `az login` (the code uses `DefaultAzureCredential`; VS Code sign-in or a service principal via env vars also work).
5. **Configure** `.env`:
   ```
   TRIAGE_BACKEND=foundry
   AZURE_AI_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
   AZURE_AI_MODEL_DEPLOYMENT=gpt-4o-mini
   TRIAGE_FOUNDRY_TRACING=true        # optional: traces to the project's Application Insights
   ```
   The endpoint is on the project's **Overview** page. For tracing, connect an Application Insights resource under **Tracing** in the portal.
6. **Run** `python -m claims_triage demo` or `chat`. The first run uploads `knowledge/*.md`, creates a vector store and the 4 agents (IDs cached in `.memory/foundry_state.json`); later runs reuse them and only update an agent when its definition changes. You will see the agents under **Agents** in the portal, and each session's threads/runs.
7. **Clean up**: `python -m claims_triage --backend foundry foundry-cleanup`.

SDK used: `azure-ai-agents` (`AgentsClient`: agents, threads, messages, runs, files, vector stores) with the Foundry project endpoint, plus `azure-ai-projects` for the telemetry connection string. If your project only exposes the newer Responses-based agent API, see `docs/ALTERNATIVES.md` §1.

## 4. Project layout
```
src/claims_triage/
  agents/definitions.py       4 agent specs (instructions) - created in code by the backend
  tools/                      skills: intake.py, coverage.py, briefing.py + registry.py (schemas, validation, retries)
  knowledge/                  vector_store.py (local TF-IDF index), rules.py (safe rule + decision-policy parser)
  memory/                     long_term.py (persisted JSON), session.py (thread-level memory)
  orchestration/              routine.py (state machine), hitl.py (gates), supervisor.py (routing, multi-turn)
  backends/                   foundry_backend.py (Azure SDK), mock_backend.py, narrative.py (templated text)
  observability.py            console logs, trace.jsonl, OpenTelemetry / Foundry tracing
  cli.py                      commands
knowledge/                    underwriting_rules.md, fraud_indicators.md, decision_policy.md
data/                         synthetic claims, policies, seed memory
tests/                        26 pytest tests (incl. the Foundry tool loop against a fake client)
docs/                         ARCHITECTURE.md, WALKTHROUGH.md, ALTERNATIVES.md, DEMO_SCRIPT.md
```

## 5. What is mocked and why

The assignment allows mocking LLM calls if Foundry quota/access is a problem, provided the SDK integration stays intact and the mock is documented.

* **Mocked (only in `TRIAGE_BACKEND=mock`)**: the model's choice of tool calls (a fixed plan per task in `backends/mock_backend.py`) and the prose (templates in `backends/narrative.py`).
* **Not mocked**: agent definitions and instructions, JSON-schema tool registration, the tool dispatcher (argument validation, retries, error capture), all business logic, the vector-store retrieval, rule evaluation, decision policy, both memory layers, the state machine, HITL gates, logging/tracing, and the complete Foundry code path in `backends/foundry_backend.py` (unit-tested against a fake `AgentsClient` using the real SDK model classes).
* **Why**: reviewers can run the full system offline and deterministically; the same run with `--backend foundry` swaps in the real model with no other change.

## 6. Tests
```bash
pytest -q        # 26 tests: validation, coverage, rules-from-knowledge, retrieval, memory,
                 # routine path, HITL abort/override, retries, degraded mode, malformed input,
                 # routing & multi-turn, cross-run memory, Foundry tool loop & timeout
```

# Architecture

## 1. Component view

```mermaid
flowchart LR
    U[Claims handler<br/>CLI chat] --> S

    subgraph APP[Application process]
      S[Supervisor<br/>deterministic router + Supervisor agent]
      R[[Triage Routine<br/>state machine]]
      G{{HITL gates 1 & 2}}
      TR[Tool registry<br/>JSON schema · validation · retries · tracing]
      S -->|triage_batch| R
      R --> G
      R -->|runs| IA & CA & BA
      S -->|explain / history| BA
      S -->|rule questions| CA
    end

    subgraph FOUNDRY[Azure AI Foundry Agent Service]
      SA[Supervisor agent]
      IA[Intake & Validation agent]
      CA[Anomaly & Coverage agent]
      BA[Adjuster Briefing agent]
      VS[(Vector store<br/>knowledge/*.md)]
      CA -. file_search .-> VS
    end
    S --> SA

    IA & CA & BA & SA -->|function tool calls<br/>requires_action| TR
    TR --> D[(claims batch<br/>policy_coverage.json)]
    TR --> KB[(Local vector index<br/>rules · patterns · decision policy)]
    TR --> LTM[(Long-term memory<br/>.memory/long_term_memory.json)]
    S --> SM[(Session memory<br/>.memory/sessions/*.json<br/>+ Foundry threads)]
    R --> OBS[trace.jsonl · console · OpenTelemetry → App Insights]
```

Key idea: **agents reason, tools compute, the routine decides the order and owns the gates.**

* Agents (LLM) choose and call tools, and write the prose.
* Tools are deterministic Python functions with strict JSON schemas - validation, coverage
  arithmetic, rule evaluation, memory reads. Facts never come from the model's priors.
* The routine (plain Python) sequences steps, checks post-conditions after each step,
  enforces guardrails, runs the human gates and is the only writer of triage outcomes to
  long-term memory.

## 2. Routine (state machine)

```mermaid
stateDiagram-v2
    [*] --> INTAKE
    INTAKE --> VALIDATE: batch loaded (checkpoint)
    INTAKE --> FAILED: malformed / empty input
    VALIDATE --> GATE_VALIDATION: every claim validated (checkpoint)
    VALIDATE --> FAILED
    GATE_VALIDATION --> COVERAGE: handler continues (may exclude claims)
    GATE_VALIDATION --> ABORTED: handler aborts
    COVERAGE --> BRIEFING: coverage + risk present for every claim (checkpoint)
    BRIEFING --> GATE_REVIEW: recommendation present for every claim (checkpoint + guardrail)
    GATE_REVIEW --> PERSIST: adjuster accepts / overrides
    GATE_REVIEW --> ABORTED: adjuster rejects
    PERSIST --> DONE: outcomes written to long-term memory
```

`TRANSITIONS` in `orchestration/routine.py` is the single source of truth; `_transition()`
raises on anything not in the table, and every transition is logged and written to
`runs/<run_id>/trace.jsonl`.

## 3. One claim through the system (sequence)

```mermaid
sequenceDiagram
    participant H as Handler
    participant S as Supervisor
    participant R as Routine
    participant I as Intake agent
    participant C as Coverage agent
    participant B as Briefing agent
    participant T as Tools
    H->>S: "triage data/claims.json"
    S->>R: run(source)
    R->>I: ingest {source_path}
    I->>T: load_claims_batch
    R->>I: validate
    I->>T: validate_claims
    R->>H: GATE 1 (intake issues)
    H-->>R: continue
    loop each claim
      R->>C: assess_claim {claim_id}
      C->>T: check_policy_coverage
      C->>T: assess_fraud_indicators (rules from KB + prior claims from memory)
    end
    loop each claim
      R->>B: brief_claim {claim_id}
      B->>T: get_policyholder_history (long-term memory)
      B->>T: recommend_next_action (decision policy from KB)
    end
    R->>H: GATE 2 (recommendations)
    H-->>R: accept / override
    R->>T: record outcomes → long-term memory
    S->>H: summary + report path
```

## 4. Requirement → implementation map

| Requirement | Where |
|---|---|
| Foundry project + Agent Service, SDK | `backends/foundry_backend.py` (`azure-ai-agents` `AgentsClient`: `create_agent`/`update_agent`, `threads`, `messages`, `runs`, `submit_tool_outputs`, `files.upload_and_poll`, `vector_stores.create_and_poll`) |
| ≥4 agents created programmatically | `agents/definitions.py` + `FoundryBackend.setup()`; idempotent via definition hash |
| ≥3 custom function tools with JSON schemas | 10 tools in `tools/*.py`, each a `ToolSpec` with an explicit schema, registered as `FunctionToolDefinition` to its owning agent(s) |
| Short-term memory | `memory/session.py` (turns, focus claim/policy, results) + one Foundry thread per agent per session; `chat --session` resumes |
| Long-term memory across runs | `memory/long_term.py` JSON store (atomic writes, corrupt-file recovery), seeded history; read by `assess_fraud_indicators` and `get_policyholder_history`, written after Gate 2 |
| Knowledge grounding | `knowledge/*.md` → `LocalVectorStore` (TF-IDF, cached) + Foundry vector store / `file_search`; rules parsed from the KB and evaluated by a safe parser (`knowledge/rules.py`) |
| Explicit, traceable routine with HITL | `orchestration/routine.py` (`State`, `TRANSITIONS`, checkpoints, guardrail), `orchestration/hitl.py` |
| Supervisor routing + multi-turn | `orchestration/supervisor.py` (deterministic router with pronoun resolution → supervisor agent fallback) |
| Error handling | invalid tool args, transient tool failures (retry + backoff), persistent tool failure (`tool_failure` finding), agent failure/timeout (retry → degraded fallback, run cancel in Foundry), Foundry 429/5xx retry, malformed input (FAILED), corrupt memory |
| Observability | `observability.py`: tagged console logs, `trace.jsonl`, OpenTelemetry spans, optional Azure Monitor export + `AIAgentsInstrumentor` |

## 5. Design decisions worth defending

1. **Deterministic tools, LLM for language.** Coverage arithmetic and date logic are never
   left to a model. The briefing agent must use `recommend_next_action`'s result; the routine
   overrides it if the text disagrees (guardrail, logged).
2. **Rules as data.** Thresholds live in `underwriting_rules.md`; change `<= 7` to `<= 3`
   and the system changes (see `test_rules_come_from_knowledge_not_code`). The same files are
   indexed for retrieval, so explanations quote the exact rule that fired.
3. **Per-claim isolation.** Coverage and briefing run per claim, so one timeout or bad record
   degrades one claim, not the batch.
4. **Humans own irreversible steps.** Nothing reaches long-term memory before Gate 2;
   `auto_approve` is a recommendation that a human confirms.
5. **Hybrid routing.** Regex routing for known intents is free, fast and testable; the
   Supervisor agent handles the long tail with read-only tools.
6. **Manual tool loop** instead of `enable_auto_function_calls`, so every call is validated,
   retried, logged and traced by our registry.

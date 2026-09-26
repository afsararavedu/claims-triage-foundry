# Step-by-step: what was built, in what order, and why

This is the build log for the assignment, written so you can explain and defend each
decision in the interview. Each step lists **what**, **why**, **where in the code**, and
**how to show it**.

---

## Step 0 - Read the brief and turn it into a checklist

**What.** Pulled out every hard requirement: 4 agents made in code, ≥3 JSON-schema tools,
short- and long-term memory, retrieval-based knowledge, an explicit routine with HITL gates,
multi-turn routing, error handling, and logging/tracing. Also noted the allowances: synthetic
data only, AI assistants allowed, mocking allowed if documented.

**Why.** The reviewers score architecture and your ability to defend it. A checklist
up front makes sure each requirement ends up in a specific, demonstrable place.
`docs/ARCHITECTURE.md` §4 is that checklist with file references.

## Step 1 - Pick the Foundry SDK surface

**What.** Used `azure-ai-agents` (`AgentsClient`) against the Foundry **project endpoint**
for agents, threads, messages, runs, file upload and vector stores. `azure-ai-projects` is
used only to get the Application Insights connection string for tracing.

**Why.** The brief names `azure-ai-projects` / `azure-ai-agents`. The threads/runs model
maps cleanly onto the requirements: a *thread* is conversation memory, a *run* in
`requires_action` hands tool calls back to our code, and `file_search` plus a vector store
covers knowledge grounding. Recent `azure-ai-projects` 2.x releases moved to a newer
Responses-based agent API; see `ALTERNATIVES.md` §1 for that route.

**Where.** `backends/foundry_backend.py`.

## Step 2 - Create synthetic data and extend it to cover every rule

**What.** Kept the 4 sample claims and 3 policies exactly as given. Added
`claims_extended.json`, which has one claim per failure type: invalid policy number,
loss after report, duplicate ID, peril not covered, over limit, late report, non-numeric
amount and one clean claim. Added two more policies (one `auto`), a peril catalog, a second
batch for cross-run memory, a malformed file, and a CSV copy.

**Why.** The sample alone never triggers several required checks (duplicates, invalid
policy format), and it has no clean claim to show `auto_approve`. Extended data lets every
code path be demonstrated and tested.

**Where.** `data/`.

## Step 3 - Write the knowledge base as retrievable, machine-readable rules

**What.** Three markdown files:
* `underwriting_rules.md` – 9 rules. Each has a `Condition` line (e.g.
  `days_since_inception <= 7`), a severity, an action and a rationale. Both required
  examples are included (UW-001 for early inception and UW-002 for within 2% of the limit).
* `fraud_indicators.md` – 6 narrative fraud patterns, used for explanations.
* `decision_policy.md` – action precedence, the auto-approve ceiling, and a table mapping
  each finding to an action.

**Why.** The brief says rules must be *retrieved*, not hard-coded in the prompt. Keeping
conditions as data means that editing a threshold in markdown changes behaviour with no
code change (a test proves this). It also means the explanation can quote the exact rule
that fired, and compliance can review the rules without reading Python.

**Where.** `knowledge/`, parsed by `knowledge/rules.py`, which uses a small grammar with no
`eval`. An unknown feature or a missing value makes a rule *not evaluable*, never a silent
match.

## Step 4 - Build the retrieval layer

**What.** `LocalVectorStore` splits each file into one chunk per `##` section and keeps the
rule ID as metadata. It builds TF-IDF vectors, runs cosine search, and caches the index on
disk (rebuilt when the files change). In Foundry mode the same files are uploaded to a
Foundry vector store and attached to the Anomaly & Coverage agent through `file_search`.

**Why.** It needs no dependencies or keys, it is deterministic (so it can be tested and
defended), and it is fast. The interface (`search(query, top_k)`) is the only thing callers
depend on, so it can be swapped for embeddings or Azure AI Search (`ALTERNATIVES.md` §3).

**Where.** `knowledge/vector_store.py`.

## Step 5 - Implement the skills (tools) with strict JSON schemas

**What.** 10 tools, each a `ToolSpec` with a name, description, JSON schema, handler and
owning agent(s):

| Agent | Tools |
|---|---|
| Intake | `load_claims_batch`, `validate_claims` |
| Coverage | `check_policy_coverage`, `assess_fraud_indicators`, `search_underwriting_knowledge` |
| Briefing | `get_policyholder_history`, `recommend_next_action`, `get_claim_triage_record`, `search_underwriting_knowledge` |
| Supervisor | `get_session_overview`, `get_claim_triage_record`, `get_policyholder_history`, `search_underwriting_knowledge`, `remember_adjuster_note` |

The **registry** is the one place every tool call goes through. It checks that the tool
belongs to the calling agent, validates arguments against the schema, injects faults for
demos, retries transient errors with backoff, turns failures into `{"ok": false, "error": ...}`
instead of crashing, and logs and traces each call.

**Why.** Facts such as dates, limits and amounts must come from code, not the model. A single
chokepoint gives uniform validation, error handling and observability in both backends.

**Where.** `tools/registry.py`, `tools/intake.py`, `tools/coverage.py`, `tools/briefing.py`.

## Step 6 - Memory: two layers

**What.**
* **Short-term (thread level)** – `SessionMemory` holds the turns, the focus claim and policy
  (so "it" and "that policyholder" resolve), the latest assessments, and the Foundry thread
  ID per agent. It is saved after every turn; `chat --session <id>` resumes it.
* **Long-term** – `LongTermMemory` is a JSON file with the history of triaged claims and
  adjuster notes. It is seeded with synthetic history (C-1804, an earlier flagged water-damage
  claim on POL-5521). Writes are atomic, a corrupt file is backed up and the store reset,
  and entries are upserted by claim ID.

**Why.** The brief asks for both, and for the briefing to reference earlier claims.
Long-term memory feeds two things: the `prior_similar_claims_12m` feature (which triggers
UW-004) and the "this policyholder had a similar water damage claim…" sentence. Only the
routine writes outcomes, and only after Gate 2, so agents cannot write unreviewed decisions
into memory.

**Where.** `memory/`. The cross-run proof is `test_long_term_memory_across_separate_runs`:
batch 2's C-2050 is flagged because of C-2031 and C-2032 from an earlier run.

## Step 7 - Define the 4 agents in code

**What.** Each `AgentSpec` has narrow instructions and a JSON output contract. The backend
creates the agents, or updates them when the definition hash changes, and registers each
agent's function tools and `file_search`.

**Why.** Narrow agents are easier to test and to reason about. JSON contracts let the routine
check an agent's output, and the hash-based update makes re-runs idempotent (no duplicate
agents in the portal).

**Where.** `agents/definitions.py`, `FoundryBackend.setup()`.

## Step 8 - The routine: an explicit state machine with checkpoints and gates

**What.** `START → INTAKE → VALIDATE → GATE_VALIDATION → COVERAGE → BRIEFING → GATE_REVIEW →
PERSIST → DONE`, with `FAILED` and `ABORTED` exits. After each step a **checkpoint** checks
its post-condition. If an agent skipped a required tool, the routine calls that tool itself
and logs a "checkpoint repair". A **guardrail** stops the briefing text from changing the
decision-policy action.

**Why.** The brief rejects "let the LLM decide everything". With this design the order of
steps is guaranteed, auditable and testable, and each step still gets LLM flexibility.
Gate 1 stops bad input from being processed silently. Gate 2 keeps a human in charge of
every decision, including auto-approvals.

**Where.** `orchestration/routine.py`, `orchestration/hitl.py`.

## Step 9 - Supervisor routing and multi-turn conversation

**What.** A deterministic router covers the known intents (triage, explain claim, policy
history, knowledge question, remember note). It normalises IDs (`C2031` becomes `C-2031`) and
resolves pronouns from the session focus. Anything it cannot classify goes to the Supervisor
agent, which has read-only tools.

**Why.** Deterministic routing is free, instant and unit-testable for the common paths. The
LLM handles the long tail. Each specialist keeps its own Foundry thread per session, so a
follow-up has model-side context too.

**Where.** `orchestration/supervisor.py`.

## Step 10 - Error handling

| Failure | Handling |
|---|---|
| Malformed claims file | Tool returns a clear error → routine goes to `FAILED`; report and trace are still written, and nothing reaches memory |
| Bad row values (`"twelve thousand"`) | Validation finding `invalid_amount` → request docs |
| Invalid tool arguments from the model | Schema validation error returned to the model, which can correct itself |
| Transient tool failure | Retry with exponential backoff (`TRIAGE_TOOL_MAX_RETRIES`) |
| Persistent tool failure | `tool_failure` finding → request docs (never auto-approve); other claims unaffected |
| Agent run fails or times out | Retry once → deterministic fallback, claim marked *degraded* in the report; Foundry runs are cancelled on timeout |
| Foundry 408/429/5xx | Retry with backoff |
| Corrupt long-term memory | Backed up, then reset from seed |

**Where.** `tools/registry.py`, `orchestration/routine.py`, `backends/foundry_backend.py`,
`memory/long_term.py`. Demo it with `TRIAGE_FAULT_INJECTION`.

## Step 11 - Observability

**What.** Every step, gate, agent run and tool call is a `span`. It prints a tagged console
line (`[STEP]`, `[AGENT]`, `[TOOL]`, `[GATE]`, `[MEMORY]`, `[KNOWLEDGE]`, `[ROUTER]`), records
an event with its duration and status to `runs/<run_id>/trace.jsonl`, and opens an
OpenTelemetry span. `--otel-console` prints the spans. `TRIAGE_FOUNDRY_TRACING=true` exports
them to the project's Application Insights and enables `AIAgentsInstrumentor`, so SDK calls
show up in the Foundry **Tracing** view.

## Step 12 - Mock backend (documented)

**What.** A deterministic stand-in replaces only the model: a fixed tool plan per task, and
templated prose. It goes through the same registry, routine and memory.

**Why.** The system runs offline, reproducibly, with zero cost. The brief explicitly allows
this if the SDK code stays intact, which it does, and it is unit-tested with real SDK model
classes (`tests/test_foundry_backend.py`).

## Step 13 - Tests and documentation

26 pytest tests cover each requirement, including HITL abort and override, degraded mode,
routing, resume, cross-run memory, and the Foundry tool loop and timeout against a fake
client. Docs: README (setup and run), ARCHITECTURE, this walkthrough, ALTERNATIVES and
DEMO_SCRIPT.

---

## Are these the final steps?

For the assignment, yes: every requirement is covered and demonstrable, offline and
on Foundry. For **production**, these would come next (roughly in priority order):

1. **Run once against your real Foundry project** (`--backend foundry`). Tune the
   instructions if the model skips tools. The checkpoint repairs will show you where.
2. **Evaluation**: a labelled set of synthetic claims with expected actions, scored with
   Azure AI Evaluation (groundedness and tool-call accuracy) in CI.
3. **Security and identity**: managed identity, Key Vault, private networking,
   content-safety filters, and PII redaction if real data were ever used.
4. **Durable state**: move long-term memory to Cosmos DB or AI Search, and session state to
   Redis or Cosmos DB.
5. **Async HITL**: gates become a work queue or Teams approval (Durable Functions / Logic
   Apps) instead of terminal prompts.
6. **Semantic retrieval**: embeddings or Azure AI Search hybrid search once the knowledge
   base grows beyond a few dozen rules.
7. **Hosting**: a FastAPI service on Azure Container Apps with a small web UI for adjusters.

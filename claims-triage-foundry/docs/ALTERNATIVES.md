# Alternatives considered (and when to pick them)

The chosen design is one valid answer. For each decision below: what was chosen, the main
alternatives, and when each alternative would be the better choice. Details of Azure
services change quickly, so check current Microsoft docs before committing to any of them.

## 1. Foundry agent API / SDK

| Option | Notes | When to prefer |
|---|---|---|
| **`azure-ai-agents` threads/runs (chosen)** | `AgentsClient` against the project endpoint; explicit `requires_action` tool loop; `FileSearchTool`, vector stores | You want full control of every tool call (validation, retries, tracing) and a well-documented API |
| `azure-ai-projects` 2.x "new" Foundry agents | Agents are versioned resources (`project.agents.create_version(agent_name, definition=PromptAgentDefinition(...))`), and conversations run through the OpenAI Responses API (`project.get_openai_client()`) | Your Foundry project is on the newer agent experience, or you want agent versioning, hosted agents and workflow agents (`WorkflowAgentDefinition`) |
| .NET SDK (`Azure.AI.Agents.Persistent` / `Azure.AI.Projects`) | Same concepts in C# | A .NET shop (the brief allows it) |

Porting to the 2.x API only touches `backends/foundry_backend.py`: `setup()` becomes
`create_version` per agent, and `run()` becomes a Responses call that loops on
`function_call` output items. Tools, routine, memory and knowledge stay as they are.

## 2. Multi-agent orchestration

| Option | Trade-off |
|---|---|
| **Code-owned state machine + specialists (chosen)** | Deterministic order, inspectable, testable, HITL is natural. Less "emergent" flexibility |
| Foundry **Connected Agents** (`ConnectedAgentTool`): the supervisor agent calls the specialists as tools | Least code; the delegation lives in Foundry. But the *LLM* decides the order, which the brief warns against unless you add checks. Good for Q&A-style routing, weaker for a mandated pipeline with gates |
| **Microsoft Agent Framework** (successor to Semantic Kernel + AutoGen) workflows: sequential, handoff, group-chat and graph workflows with built-in human-in-the-loop and checkpointing | A strong choice for a larger production system; the routine above maps almost one-to-one onto a graph workflow |
| **LangGraph** | Mature graph and state machine with interrupts for HITL and a persistence layer; not Azure-native, but it works with Azure OpenAI and Foundry models |
| Durable Functions / Logic Apps orchestrating agent calls | Best when gates are long-running (hours or days) and need durable waits, retries and audit |
| Foundry workflow agents | Declarative multi-agent workflows hosted in Foundry; check the current feature status |

## 3. Knowledge grounding

| Option | When |
|---|---|
| **Local TF-IDF index + Foundry `file_search` (chosen)** | Small knowledge base, offline demo, deterministic tests |
| Embeddings (Azure OpenAI `text-embedding-3-*`) + FAISS / Chroma | Paraphrased questions, a larger corpus, still local |
| **Azure AI Search** index + `AzureAISearchTool` (hybrid + semantic ranker) | Production: security trimming, freshness, many documents, citations |
| Foundry `file_search` only | Simplest, but the model evaluates the rules itself, so auditability is lower. The chosen design evaluates rule conditions in code and uses retrieval for explanation |

## 4. Long-term memory

| Option | When |
|---|---|
| **JSON file (chosen)** | Demo and single user; atomic writes |
| SQLite | Local, but you need queries and concurrency |
| Azure Cosmos DB (partition key = policy number) | Production claims history and audit trail |
| Azure AI Search index over past briefings | "Find similar past claims" semantic recall |
| Foundry managed **memory stores** (preview in `azure-ai-projects` 2.x) | You want the platform to extract and recall memories per user/scope |
| Mem0 / Redis | Cross-app agent memory, low latency |

## 5. Routing

| Option | Trade-off |
|---|---|
| **Regex router + LLM fallback (chosen)** | Fast, free and testable for known intents |
| LLM-only intent classification (JSON output) | More flexible language, costs a call per turn, needs evaluation |
| Embedding-similarity router against example utterances | Middle ground; no LLM call, handles paraphrases |

## 6. HITL gates

Terminal prompts (chosen, fine for a demo) → Teams Adaptive Card approvals → Durable
Functions external events / Logic Apps approval → Agent Framework human-in-the-loop
requests → a small web UI (Streamlit, Chainlit or React) with an approval queue.

## 7. Observability

Console + `trace.jsonl` + OpenTelemetry (chosen) → Application Insights via Foundry
tracing (built in, enable with `TRIAGE_FOUNDRY_TRACING=true`) → Azure AI Evaluation for
quality → third-party LLM observability (Langfuse, Arize Phoenix) if the organisation uses it.

## 8. When Foundry is not available

| Option | Notes |
|---|---|
| **Deterministic mock backend (chosen)** | Reproducible, zero cost; prose is templated |
| Record and replay real Foundry responses | Realistic text in tests; needs one real run first |
| Local model with an OpenAI-compatible API (Ollama, LM Studio) | Real LLM behaviour offline; tool-calling quality varies by model |
| GitHub Models / Azure OpenAI directly | Real model without Foundry Agent Service; you implement threads yourself |

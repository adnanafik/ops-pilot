# ops-pilot — Agentic CI/CD Incident Responder

---

## Current Status: Evolving from pipeline → production AI agent

ops-pilot started as a well-architected multi-agent pipeline (Monitor → Triage → Fix → Notify).
It is now being evolved into a true agentic system suitable for enterprise sales.
Each phase below adds a new capability layer. Read the roadmap before suggesting changes.

---

## How We Collaborate

- Concepts first, code second. Explain the "why" before writing anything.
- Ask the user what they think before offering a solution.
- Reference claurst patterns (`~/dev/claurst`) as production examples where relevant.
- User wants to understand every decision well enough to explain it to a customer.

---

## Evolution Roadmap

### Phase 1 — Agent Loop  `[ DONE ]`
Replace the linear single-call pipeline with a real tool-use loop where the model drives.

**The problem diagnosed by the user:**
- `TriageAgent._build_prompt` fires one `_call_llm` call with a fixed log tail — no retry, no follow-up
- `fix_confidence: LOW` is produced but never acted on — pipeline continues regardless
- Model cannot request more log lines, read source files, or ask a clarifying question
- Schema enforces a confident answer shape even when confidence is genuinely absent

**Three tools the model needs during triage (identified by user):**
1. `get_file(repo, path, ref)` — read the actual source at the offending line
2. `get_more_log(run_id, job, offset)` — fetch earlier log sections (real cause often 50–100 lines above tail)
3. `get_commit_diff(repo, sha)` — full unified diff, not the DiffSummary abstraction

**What to build:**
- Generic `AgentLoop` class: runs until `end_turn` or max turns
- Convert TriageAgent and FixAgent from single-call to loop-based
- Turn limits and timeout safety

**Key concepts:** tool-use streaming, conversation history as working memory, error recovery

---

### Phase 2 — Tool System  `[ PENDING ]`
Convert hardcoded provider calls into schema-defined tools the model can discover and choose.

**What to build:**
- `Tool` base class: name, description, JSON schema, permission level
- Convert all provider methods into tools
- Tool registry (agent discovers available tools at runtime)
- Permission tiers: `READ_ONLY`, `WRITE`, `DANGEROUS`, `REQUIRES_CONFIRMATION`

**Key concepts:** JSON Schema drives model tool selection, description quality = capability quality, blast radius per tool

---

### Phase 3 — Multi-Agent Orchestration  `[ PENDING ]`
Coordinator agent spawns parallel workers for complex incidents.

**What to build:**
- `CoordinatorAgent` that spawns workers via a `SpawnAgent` tool
- Worker isolation (own context, filtered tool list — workers can't spawn workers)
- Result aggregation back to coordinator

**Key concepts:** parallel investigation vs. serial, coordinator system prompt design, context isolation

---

### Phase 4 — Memory System  `[ PENDING ]`
Accumulate operational knowledge across incidents so the agent improves over time.

**What to build:**
- Post-incident extraction: failure type, root cause, fix applied, was fix permanent
- Similarity retrieval: before triage, pull 3 most similar past incidents
- Weekly consolidation job: compress raw logs into durable knowledge

**Key concepts:** what's worth remembering vs. ephemeral, embedding-based similarity, extraction/consolidation pattern

---

### Phase 5 — Context Management  `[ PENDING ]`
Token budgeting and compaction for long-running investigations.

**What to build:**
- Token counter tracking usage per investigation
- Compress-old-results strategy (keep conclusions, drop raw data)
- Hard limit with graceful degradation (summarize + continue vs. crash)

**Key concepts:** context window as a finite resource, load-bearing vs. compressible context, cost/capability trade-off

---

### Phase 6 — Production Hardening (Multi-Tenant)  `[ PENDING ]`
Isolation, metering, and rate limiting for enterprise B2B sales.

**What to build:**
- Tenant namespace model: isolated state, config, memory per customer
- Per-tenant tool permissions and escalation rules
- Usage tracking per tenant (API calls, tokens, incidents resolved)
- Rate limiting per tenant

**Key concepts:** config-per-deployment vs. true multi-tenancy, designing isolation from the start

---

### Phase 7 — Trust & Explainability Layer  `[ PENDING ]`
Pre-execution explanations, audit logs, confidence surfacing, human escalation.

**What to build:**
- Pre-action explanation generator (separate LLM call before dangerous tools)
- Structured audit log: every tool call, arguments, result, timestamp, tenant, user
- Confidence scoring surfaced in notifications
- Human escalation path: "I'm stuck, here's what I found" instead of silently failing

**Key concepts:** explainability requires reasoning *about* action (not just doing it), audit log design, progressive trust

---

## Original Build Context (v1 — completed)

The original ops-pilot was built as a portfolio project for a Director of Agentic AI role.
Architecture decisions from that phase:
- Four agents (Monitor, Triage, Fix, Notify) each extending `BaseAgent[OutputT]`
- LLM backend injected via dependency, not hardcoded — swappable (Anthropic, Bedrock, Vertex)
- File-based task queue in `./current_tasks/` using POSIX atomic rename (no external deps)
- All inter-agent data uses Pydantic models in `shared/models.py` — no raw dicts
- Demo mode (`DEMO_MODE=true`) serves pre-recorded scenarios from `demo/scenarios/*.json`

**Do not break these foundations.** Evolution phases build on top of them.

---

## Scenario files — do not overwrite
- `demo/scenarios/null_pointer_auth.json`
- `demo/scenarios/missing_dependency_docker.json`
- `demo/scenarios/flaky_integration_test.json`

---

## Code style
- Python 3.11+, type hints everywhere, no `Any` types
- Docstrings on every class and public method
- pytest with at least 80% coverage on agent logic
- ruff for linting (config in pyproject.toml)
- Dependencies in pyproject.toml, no requirements.txt

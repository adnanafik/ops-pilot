# Phase 7 — Trust & Explainability Layer Design

**Date:** 2026-04-06
**Branch:** feature/phase7-trust-explainability
**Status:** Approved, pending implementation

---

## Context

ops-pilot now runs autonomously through Phases 1–6: it triages failures, writes patches, opens PRs, tracks usage, and enforces rate limits — all without human involvement. Phase 7 makes this autonomy legible to the enterprise buyers who need to trust it. The deliverables are: explanations before risky actions, a structured audit trail of every tool call, LOW-confidence escalation instead of guessing, and confidence scoring surfaced in notifications.

Deployment model: separate instance per customer (same code, isolated runtime data). No shared-process multi-tenancy.

---

## Goals

1. **Pre-action explanations** — before executing `REQUIRES_CONFIRMATION` tools, generate a plain-English sentence explaining what the agent is about to do and why
2. **Audit log** — every tool call recorded: tool name, arguments, result, timestamp, tenant, actor, explanation
3. **Escalation path** — LOW confidence triage skips FixAgent entirely; a structured escalation summary is generated for the on-call engineer instead
4. **Confidence in notifications** — `fix_confidence` surfaced prominently; escalation summary or fix summary included depending on outcome

---

## Architecture

### New modules

```
shared/
  audit_log.py          ← AuditLog — writes JSONL records to audit/YYYY-MM-DD.jsonl
  explanation_gen.py    ← ExplanationGenerator — LLM call before REQUIRES_CONFIRMATION tools
  escalation.py         ← generate_escalation_summary() — LLM call on LOW confidence
  trust_context.py      ← TrustContext dataclass + make_trust_context() factory

audit/                  ← runtime directory (gitignored)
  YYYY-MM-DD.jsonl      ← one JSON record per line per audit event
```

### TrustContext

```python
@dataclass
class TrustContext:
    audit_log: AuditLog
    explanation_generator: ExplanationGenerator
```

Constructed once at startup by `make_trust_context(config, backend) -> TrustContext`. Injected into `AgentLoop` alongside `TenantContext` — same injection pattern used throughout Phases 5 and 6. `ExplanationGenerator` receives the backend at construction time (same instance as `AgentLoop`) so test doubles are injectable and backend auth is not duplicated.

```python
def make_trust_context(config: OpsPilotConfig, backend) -> TrustContext:
    return TrustContext(
        audit_log=AuditLog(base_dir=Path("audit")),
        explanation_generator=ExplanationGenerator(
            backend=backend,
            model=config.trust.explanation_model,
        ),
    )
```

### Config additions (`ops-pilot.yml`)

```yaml
trust:
  explanation_model: claude-haiku-4-5-20251001  # cheaper model for explanation calls; defaults to main model
```

`OpsPilotConfig` gains `trust: TrustConfig` with:

```python
class TrustConfig(BaseModel):
    explanation_model: str = ""   # empty = use same model as main agent
```

`make_trust_context()` resolves `explanation_model` to `config.model` when the field is empty.

---

## Component Detail

### AuditLog

File: `shared/audit_log.py`

```python
class AuditLog:
    def record(
        self,
        *,
        tenant_id: str,
        actor: str,
        tool_name: str,
        tool_input: dict,
        tool_result: str,
        is_error: bool,
        explanation: str | None,
    ) -> None: ...
```

Each record is one JSON line:

```json
{
  "ts": 1743686400.0,
  "tenant_id": "acme-corp",
  "actor": "FixAgent",
  "tool_name": "update_file",
  "tool_input": {"path": "auth/token.py", "content": "..."},
  "tool_result": "File updated successfully",
  "is_error": false,
  "explanation": "Patching null-check at line 47 because the diff shows token.validate() was called before the guard was added in commit a1b2c3d."
}
```

`explanation` is `null` for `READ_ONLY` and `WRITE` tools — only populated for `REQUIRES_CONFIRMATION`.

Writes use POSIX atomic rename (same pattern as `UsageTracker` and `MemoryStore`). Write failures log a warning and return silently — a broken audit trail never stops an investigation.

Storage: `audit/YYYY-MM-DD.jsonl` — one file per UTC calendar day. Directory created on first write.

### ExplanationGenerator

File: `shared/explanation_gen.py`

```python
class ExplanationGenerator:
    def __init__(self, backend, model: str) -> None: ...

    def generate(
        self,
        tool_name: str,
        tool_input: dict,
        context_summary: str,
    ) -> str: ...
```

Makes one `backend.complete()` call with a concise prompt containing the tool name, sanitised arguments, and `context_summary` (the `last_assistant_text` at the point of the tool call — 2–3 sentences of what the investigation found so far). Returns a single plain-English sentence.

On backend failure: logs a warning, returns `""` (empty string). Tool execution proceeds regardless — explanation generation is observability, not a gate.

### escalation.py

File: `shared/escalation.py`

```python
class EscalationSummary(BaseModel):
    failure_id: str
    tenant_id: str | None
    what_was_investigated: str
    what_was_inconclusive: str
    recommended_next_step: str

def generate_escalation_summary(
    failure: Failure,
    triage: Triage,
    backend,
    model: str,
) -> EscalationSummary: ...
```

One `backend.complete()` call. Called from `run_pipeline.py` when `triage.fix_confidence == "LOW"`. Replaces the FixAgent path entirely for that investigation.

On backend failure: returns a minimal `EscalationSummary` with `what_was_investigated` populated from `triage.output` directly — the pipeline never crashes.

---

## Integration Points

| Location | Change |
|---|---|
| `AgentLoop.__init__` | Add `trust_context: TrustContext \| None = None` and `actor: str = "agent"` parameters |
| `AgentLoop._execute_tools_concurrent()` | Add `last_text: str` parameter so the explanation generator can use the current investigation summary as context |
| `AgentLoop.run_one()` — `REQUIRES_CONFIRMATION` gate | If `trust_context` is set: generate explanation and auto-proceed (Phase 7 — observability without blocking). If `trust_context` is absent: fall through to existing `confirm` hook gate (Phase 8 will wire real approval there) |
| `AgentLoop.run_one()` — after any tool execution | Call `trust_context.audit_log.record(...)` with all fields; `explanation` is `None` for non-confirmation tools |
| `UpdateFileTool.permission` | Change return value from `Permission.WRITE` to `Permission.REQUIRES_CONFIRMATION` |
| `OpenDraftPRTool.permission` | Change return value from `Permission.WRITE` to `Permission.REQUIRES_CONFIRMATION` |
| `CreateBranchTool.permission` | No change — stays `Permission.WRITE` |
| `run_pipeline.py` | After triage, branch on `fix_confidence`: LOW → `generate_escalation_summary()` + notify; else → existing FixAgent path |
| `NotifyAgent` | Accept `fix: Fix \| None` and `escalation: EscalationSummary \| None`; render escalation format when fix is absent |
| `OpsPilotConfig` | Add `trust: TrustConfig` field |
| `run_pipeline.py` startup | Construct `trust_ctx = make_trust_context(config, backend)` alongside `tenant_ctx` |
| `.gitignore` | Add `audit/` |

---

## Error Handling

| Failure | Behaviour |
|---|---|
| `AuditLog` write fails | Log warning, continue — broken audit trail never stops an investigation |
| `ExplanationGenerator` call fails | Log warning, set `explanation = None` in audit record, continue with tool execution |
| `generate_escalation_summary` fails | Log error, return minimal `EscalationSummary` from `triage.output` directly |
| `NotifyAgent` receives `EscalationSummary` with no fix | Renders escalation message format — no special error handling needed |

---

## Testing

| Module | What to test |
|---|---|
| `AuditLog` | Record written correctly to daily file, daily file rollover (new UTC day = new file), atomic write pattern, silent failure on unwritable path |
| `ExplanationGenerator` | Correct prompt constructed from tool name + input + context summary; backend called exactly once; backend failure returns empty string |
| `escalation.py` | `EscalationSummary` fields populated from triage; backend called once; fallback summary returned on backend failure |
| `TrustContext` | `make_trust_context()` passes backend reference to `ExplanationGenerator`; resolves empty `explanation_model` to `config.model` |
| `AgentLoop` | Explanation generated before `REQUIRES_CONFIRMATION` tool; audit record written after every tool; no explanation call for `READ_ONLY`/`WRITE` tools; `actor` stamped on every audit record |
| `run_pipeline.py` | LOW confidence skips FixAgent and calls `generate_escalation_summary`; MEDIUM/HIGH proceeds to FixAgent |
| Tool promotions | `UpdateFileTool.permission == REQUIRES_CONFIRMATION`; `OpenDraftPRTool.permission == REQUIRES_CONFIRMATION`; `CreateBranchTool.permission == WRITE` |

Target: 80% coverage on all new modules, consistent with existing standards.

---

## Storage Layout

```
audit/
  2026-04-06.jsonl    # {"ts": ..., "tenant_id": ..., "actor": ..., "tool_name": ..., ...}
  2026-04-07.jsonl
```

Both the `audit/` directory and its files are gitignored. The directory is created on first write.

---

## Future Upgrade Path

Phase 8 (human-in-the-loop approval): replace the auto-proceed behaviour in the `trust_context` branch with a real approval gate. The seam is already in place — when `trust_context` is set and a `confirm` hook is also provided, Phase 8 can check the hook result after explanation generation instead of auto-proceeding. Zero changes to `AuditLog`, `ExplanationGenerator`, or any tool definitions. The `confirm` hook signature is unchanged.

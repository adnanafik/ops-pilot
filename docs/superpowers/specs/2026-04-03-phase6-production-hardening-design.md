# Phase 6 — Production Hardening Design

**Date:** 2026-04-03
**Branch:** feature/phase6-production-hardening
**Status:** Approved, pending implementation

---

## Context

ops-pilot is deployed as separate instances per customer — same codebase, different config and data per deployment. Phase 6 makes each deployment production-ready and cost-controlled for enterprise B2B sales. There is no shared-process multi-tenancy; OS-level process isolation handles tenant separation.

---

## Goals

1. **Tenant identity** — each deployment knows who it is; `tenant_id` is stamped on all incidents, logs, and usage records
2. **Tool permissions** — per-deployment allowlist controlling which tools agents can invoke
3. **Usage tracking** — per-deployment daily counters for tokens consumed, API calls made, and incidents resolved
4. **Rate limiting** — per-deployment sliding-window caps to prevent runaway LLM costs

---

## Architecture

### New modules

```
shared/
  tenant_context.py    ← TenantContext dataclass + make_tenant_context() factory
  tool_permissions.py  ← ToolPermissions — allowed tool set per deployment
  usage_tracker.py     ← file-based daily usage counters
  rate_limiter.py      ← sliding-window rate limiter, file-backed
  exceptions.py        ← RateLimitExceeded, ToolPermissionDenied

usage/                 ← runtime directory (gitignored)
  YYYY-MM-DD.json      ← daily counters: tokens_consumed, api_calls, incidents_resolved
  rate_state.json      ← sliding window event list for rate limiter
```

### TenantContext

```python
@dataclass
class TenantContext:
    tenant_id: str
    permissions: ToolPermissions
    usage_tracker: UsageTracker
    rate_limiter: RateLimiter
```

Constructed once at startup by `make_tenant_context(config: OpsPilotConfig) -> TenantContext`. Injected into agents alongside `LLMBackend` — same injection pattern already used for the LLM backend. Agents hold a `TenantContext`; they do not import individual subsystems directly.

### Config additions (ops-pilot.yml)

```yaml
tenant_id: acme-corp           # required; stamped on all records and log entries

permissions:
  allowed_tools:               # explicit allowlist; omit = all tools allowed (default-open)
    - get_file
    - get_more_log
    - get_commit_diff

rate_limits:
  max_api_calls_per_hour: 100
  max_tokens_per_hour: 500000
```

`OpsPilotConfig` gains three new Pydantic fields: `tenant_id: str`, `permissions: PermissionsConfig`, `rate_limits: RateLimitsConfig`.

---

## Component Detail

### ToolPermissions

- Wraps `allowed_tools: list[str]` from config
- `is_allowed(tool_name: str) -> bool`
- If `allowed_tools` is omitted in config, all tools are permitted (no breaking change for existing deployments)
- `ToolRegistry.execute()` calls `permissions.is_allowed(tool_name)` before executing any tool; raises `ToolPermissionDenied` on denial

### UsageTracker

- Three counters per day: `tokens_consumed`, `api_calls`, `incidents_resolved`
- File: `usage/YYYY-MM-DD.json` — one file per calendar day, UTC
- Writes use POSIX atomic rename (same pattern as `MemoryStore`)
- API: `record_tokens(n: int)`, `record_api_call()`, `record_incident()`
- Reading: load today's file — no aggregation in Phase 6 (future DB phase adds querying)

### RateLimiter

- Sliding window over the last 60 minutes
- State: `usage/rate_state.json` — list of `{ts: float, tokens: int}` events
- `check_and_consume(tokens: int) -> None` — raises `RateLimitExceeded` if either cap would be exceeded
- Algorithm: drop events older than 3600s, sum remaining, check against caps, append new event, atomic write
- Fail-open on corrupt or missing state file (logs error, allows the call)
- Future DB upgrade: replace this class only — zero agent changes required

### Exceptions

```python
# shared/exceptions.py
class RateLimitExceeded(Exception):
    """Raised when a deployment's rate limit cap is reached."""

class ToolPermissionDenied(Exception):
    """Raised when an agent attempts to use a tool not in its allowlist."""
```

---

## Integration Points

| Location | Change |
|---|---|
| `AgentLoop` | Call `rate_limiter.check_and_consume(estimated_tokens)` before each LLM call (estimated via existing `ContextBudget._estimate_tokens()` heuristic); call `usage_tracker.record_tokens(n)` + `record_api_call()` after |
| `ToolRegistry.execute()` | Call `permissions.is_allowed(tool_name)` before execution |
| `MemoryRecord` | Add `tenant_id: str | None = None` field (optional for backward compat with existing records); `make_memory_record()` receives it from `TenantContext` |
| Pipeline entrypoint | Call `usage_tracker.record_incident()` after completed triage+fix cycle |
| Structured logs | Include `tenant_id` in all agent log entries |
| `load_config()` | No change — caller constructs `TenantContext` via `make_tenant_context(config)` |

---

## Error Handling

**Rate limit exceeded:** `AgentLoop` catches `RateLimitExceeded`, logs a structured warning with `tenant_id` and the cap hit, and stops the investigation gracefully — same "summarize + stop" pattern as the Phase 5 context budget hard limit.

**Permission denied:** `ToolRegistry` raises `ToolPermissionDenied`. `AgentLoop` surfaces it to the model as a tool result with `is_error: true`. The model can adapt (try a different tool, or conclude without that data).

**Usage/rate file errors:** Usage tracking fails silently with a log warning. Rate limiting fails open (allows the call) with a log error. A broken meter does not take down an investigation — correctness of AI work takes priority over correctness of accounting.

---

## Storage Layout

```
usage/
  2026-04-03.json       # {"tokens_consumed": 12400, "api_calls": 8, "incidents_resolved": 2}
  rate_state.json       # [{"ts": 1743686400.0, "tokens": 1500}, ...]
```

Both files are gitignored. The `usage/` directory is created on first write.

---

## Future DB Upgrade Path

The seam is `RateLimiter` and `UsageTracker`. Both are constructed inside `make_tenant_context()`. Swapping to a database means:

1. Replace `UsageTracker` with a DB-backed implementation behind the same interface
2. Replace `RateLimiter` with a DB-backed implementation behind the same interface
3. Update `make_tenant_context()` to construct the new implementations

Zero changes to agents, `AgentLoop`, or `ToolRegistry`.

---

## Testing

| Module | What to test |
|---|---|
| `ToolPermissions` | Allowlist enforcement, default-open when omitted, denied tool raises correctly |
| `UsageTracker` | Write counters, read back, atomic write verification, daily file rollover (new day = new file) |
| `RateLimiter` | Window expiry drops old events, `api_calls` cap enforced, `tokens` cap enforced, fail-open on corrupt state |
| `TenantContext` | `make_tenant_context()` wires all four components correctly from a config dict |
| `ToolRegistry` | Denied tool raises `ToolPermissionDenied` before execution (not after) |

Target: 80% coverage on all new modules, consistent with existing test standards.

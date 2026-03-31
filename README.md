# ops-pilot

**ops-pilot** is a multi-agent AI system that watches your CI/CD pipelines, automatically diagnoses failures, generates fix patches, and notifies your team — so engineers spend time shipping features, not hunting stack traces.

[Live Demo](https://ops-pilot.onrender.com) · [Architecture](#architecture) · [Quickstart](#quickstart) · [Design Decisions](#design-decisions)

---

## Architecture

```
                          ops-pilot
  ┌───────────────────────────────────────────────────────────────┐
  │                                                               │
  │  ┌──────────────┐   Failure    ┌──────────────┐   Triage     │
  │  │   Monitor    │ ──────────▶  │   Triage     │ ──────────▶  │
  │  │   Agent      │              │   Agent      │              │
  │  │              │              │              │              │
  │  │ GitHub API   │              │ Claude LLM   │              │
  │  │ poll failed  │              │ root cause   │              │
  │  │ CI runs      │              │ + severity   │              │
  │  └──────────────┘              └──────────────┘              │
  │                                                               │
  │  ┌──────────────┐   Fix        ┌──────────────┐              │
  │  │   Notify     │ ◀──────────  │   Fix        │              │
  │  │   Agent      │              │   Agent      │              │
  │  │              │              │              │              │
  │  │ Slack bot /  │              │ Claude LLM   │              │
  │  │ webhook /    │              │ patch + open │              │
  │  │ console      │              │ draft PR     │              │
  │  └──────────────┘              └──────────────┘              │
  │                                                               │
  │  ┌─────────────────────────────────────────────────────────┐ │
  │  │                  Shared Infrastructure                  │ │
  │  │  StateStore (JSON)  ·  TaskQueue (file locks)           │ │
  │  │  Pydantic models  ·  BaseAgent abstraction              │ │
  │  └─────────────────────────────────────────────────────────┘ │
  └───────────────────────────────────────────────────────────────┘

  External:  GitHub Actions API  ·  Anthropic Claude API  ·  Slack API
```

Each agent extends `BaseAgent`, receives typed Pydantic models, and returns typed Pydantic models. No raw dicts cross agent boundaries.

---

## How it works

1. **Watch loop** (`scripts/watch_and_fix.py`) polls every configured repo for failed GitHub Actions runs. It deduplicates by commit SHA — if an open ops-pilot PR already exists for a commit, it waits for human review before acting again.

2. **TriageAgent** sends the CI log tail to Claude with a structured prompt and extracts root cause, severity (LOW/MEDIUM/HIGH/CRITICAL), affected service, and fix confidence into a typed `Triage` model.

3. **FixAgent** asks Claude which source file(s) to edit (when diff info is unavailable), fetches them from GitHub, prompts Claude for a minimal fix, commits the patched files to a new `ops-pilot/fix-<sha>` branch, and opens a draft PR — humans review before anything merges.

4. **NotifyAgent** generates a concise Slack message via Claude and posts it via bot token, incoming webhook, or falls back to console output in demo/dev mode.

The demo server (`demo/app.py`) replays pre-recorded scenarios over Server-Sent Events with a typewriter effect — no live API calls, zero hosting cost.

---

## Quickstart

### Run locally (3 commands)

```bash
git clone https://github.com/adnanafik/ops-pilot
cd ops-pilot
cp .env.example .env          # fill in ANTHROPIC_API_KEY + GITHUB_TOKEN
pip install -e ".[dev]"
python3 scripts/watch_and_fix.py --once --dry-run   # triage only, no PRs
```

### Demo UI

```bash
DEMO_MODE=true uvicorn demo.app:app --reload
open http://localhost:8000
```

### Docker

```bash
# Demo UI on http://localhost:8000
docker compose up ops-pilot-demo

# Production watcher (live PRs + Slack)
docker compose --profile watcher up ops-pilot-watcher
```

### Configure pipelines

Copy `ops-pilot.example.yml` to `ops-pilot.yml` and add your repos:

```yaml
anthropic_api_key: ${ANTHROPIC_API_KEY}
github_token: ${GITHUB_TOKEN}
slack_bot_token: ${SLACK_BOT_TOKEN}   # or slack_webhook_url

pipelines:
  - repo: myorg/backend
    slack_channel: "#platform-alerts"
    severity_threshold: medium        # ignore low-severity noise

  - repo: myorg/payments
    slack_channel: "#payments-oncall"
    severity_threshold: high
```

All values support `${ENV_VAR}` substitution. Environment variables always override file values.

---

## Project structure

```
ops-pilot/
├── agents/
│   ├── base_agent.py       # Abstract BaseAgent — run(), describe(), _call_llm()
│   ├── monitor_agent.py    # Polls GitHub Actions / loads demo scenarios
│   ├── triage_agent.py     # Root cause analysis via Claude
│   ├── fix_agent.py        # Patch generation + draft PR via GitHub API
│   └── notify_agent.py     # Slack bot / webhook / console notifications
├── shared/
│   ├── models.py           # Pydantic models: Failure, Triage, Fix, Alert
│   ├── config.py           # YAML config loader with env-var substitution
│   ├── task_queue.py       # File-locked task queue (atomic rename pattern)
│   └── state_store.py      # JSON state persistence (survives restarts)
├── scripts/
│   └── watch_and_fix.py    # Production watcher — Monitor→Triage→Fix→Notify loop
├── demo/
│   ├── app.py              # FastAPI SSE server
│   ├── scenarios/          # Pre-recorded JSON scenarios (3 realistic failures)
│   └── static/index.html   # Single-file demo UI (vanilla JS, no build step)
├── tests/
│   ├── test_triage_agent.py
│   ├── test_fix_agent.py
│   └── fixtures/           # Sample CI log files
├── ops-pilot.example.yml   # Config template
├── .env.example            # Environment variable reference
├── Dockerfile
└── docker-compose.yml      # demo + watcher services
```

---

## Design decisions

### Why file-based task locking?

The task queue uses `os.rename()` for atomic task claiming — a POSIX guarantee that means two workers can never claim the same task without a database or message broker. This keeps ops-pilot dependency-free, git-friendly, and trivially deployable anywhere. Inspired by the [Anthropic multi-agent compiler article](https://www.anthropic.com/research/building-effective-agents).

### Why simulation mode?

Live agentic demos are brittle: API rate limits, flaky network, non-deterministic LLM output. Simulation mode replays realistic pre-recorded runs with SSE streaming, so the demo always works and hosting costs nothing. Engineers and recruiters see exactly what the system does without a credit card.

### Why BaseAgent abstraction?

The `BaseAgent` contract (`run()`, `describe()`, injected LLM client) makes each agent independently testable with a mock client, swappable for a different LLM provider, and composable into larger pipelines. The `Generic[OutputT]` typing means callers always know what type they get back — no `Any`, no runtime surprises.

### Why Pydantic models between agents?

Raw dicts are invisible to the type checker and break silently when a key is missing. Pydantic models validate at construction time, generate JSON Schema automatically (useful for tool-use prompts), and self-document via field descriptions.

### Why GitHub open PRs as deduplication source of truth?

Local state files get wiped on container restarts. Using the GitHub API to check for open `ops-pilot/fix-<sha>` branches means the watcher never raises a second PR for the same failure, even after a crash or redeploy.

---

## Running tests

```bash
pytest                   # all tests with coverage report
pytest -k triage         # triage agent tests only
ruff check agents/ shared/
```

Coverage target: ≥80% on `agents/` and `shared/`.

---

## License

MIT © 2026

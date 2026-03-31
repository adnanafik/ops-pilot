# ops-pilot — Agentic CI/CD Incident Responder

## Important — scenario files already exist
The following files are already created and must not be overwritten:
- demo/scenarios/null_pointer_auth.json
- demo/scenarios/missing_dependency_docker.json  
- demo/scenarios/flaky_integration_test.json

Build the demo UI and FastAPI server to read and serve these exact files.

## Project goal
Build a multi-agent Python system called `ops-pilot` that monitors CI/CD
pipelines, triages failures, generates fix suggestions, and notifies teams.
This is a portfolio project for a Director of Agentic AI role. Code quality,
architecture clarity, and README quality matter as much as functionality.

## Repo structure to create
ops-pilot/
├── agents/
│   ├── base_agent.py          # Abstract BaseAgent class all agents extend
│   ├── monitor_agent.py       # Polls for CI failures (GitHub Actions API)
│   ├── triage_agent.py        # Root cause analysis from logs + diff
│   ├── fix_agent.py           # Generates patch + opens draft PR
│   └── notify_agent.py        # Slack / console notification
├── shared/
│   ├── task_queue.py          # File-based task locking (like the Anthropic
│   │                          #   compiler article pattern)
│   ├── state_store.py         # Simple JSON state persistence
│   └── models.py              # Pydantic models: Failure, Triage, Fix, Alert
├── demo/
│   ├── app.py                 # FastAPI server for the interactive demo
│   ├── scenarios/             # Pre-recorded JSON scenario files
│   │   ├── null_pointer.json
│   │   ├── missing_dependency.json
│   │   └── flaky_test.json
│   └── static/
│       └── index.html         # Single-page demo UI with streaming display
├── tests/
│   ├── test_triage_agent.py
│   ├── test_fix_agent.py
│   └── fixtures/              # Sample CI log files for testing
├── .github/
│   └── workflows/
│       └── ops-pilot-self-test.yml  # ops-pilot watches itself
├── CLAUDE.md                  # This file
├── README.md                  # Portfolio-quality README with architecture
│                              #   diagram, demo link, and usage
└── pyproject.toml

## Architecture rules — enforce these throughout
1. Every agent extends BaseAgent which defines: run(), describe(), and
   a structured output model. No agent should have business logic in __init__.
2. The LLM provider (Anthropic) is injected via dependency, not hardcoded.
   All agents must work with ANTHROPIC_API_KEY from environment.
3. The task queue uses file locks in ./current_tasks/ — same pattern as the
   Anthropic multi-agent compiler article. Git-compatible, no external deps.
4. All inter-agent data uses Pydantic models defined in shared/models.py.
   No raw dicts passed between agents.
5. Demo mode: the FastAPI app serves pre-recorded scenarios from
   demo/scenarios/*.json with simulated streaming (no live API calls).
   Set DEMO_MODE=true to enable. This keeps hosting cost at $0.
6. Live mode: set DEMO_MODE=false and ANTHROPIC_API_KEY to run real agents.

## Demo UI requirements
- Single HTML file, no framework, vanilla JS only
- Shows 3 scenario buttons: "Null pointer crash", "Missing dependency",
  "Flaky integration test"
- When clicked, streams agent output step by step with typewriter effect
- Shows 4 panels: Monitor → Triage → Fix → Notify, lighting up in sequence
- Displays the generated PR description and Slack message at the end
- Mobile-friendly, works without any build step

## README requirements — this is a portfolio piece, write it like one
- Lead with a 2-sentence "what and why" that a non-technical recruiter
  understands
- Architecture diagram in ASCII or Mermaid
- "How it works" section explaining the agent coordination pattern
- "Run locally in 3 commands" quickstart
- Link placeholder for live demo: [Live Demo](https://ops-pilot.onrender.com)
- "Design decisions" section explaining: why file-based task locking,
  why simulation mode, why BaseAgent abstraction
- MIT license

## Code style
- Python 3.11+, type hints everywhere, no Any types
- Docstrings on every class and public method
- pytest for tests, at least 80% coverage on agent logic
- ruff for linting (include pyproject.toml config)
- No requirements.txt — use pyproject.toml with [project.dependencies]

## Build order
1. shared/models.py and shared/task_queue.py first
2. agents/base_agent.py
3. agents/triage_agent.py (most complex, most impressive)
4. agents/monitor_agent.py and fix_agent.py
5. agents/notify_agent.py
6. tests/ with fixtures
7. demo/scenarios/*.json (3 realistic pre-recorded runs)
8. demo/app.py and demo/static/index.html
9. README.md (write this last when everything works)
10. .github/workflows/ops-pilot-self-test.yml

## When you finish each file, run the tests for that module before moving on.
## Do not move to the next file if tests are failing.
## Commit after each working module with a descriptive message.
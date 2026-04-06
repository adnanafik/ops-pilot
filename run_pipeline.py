#!/usr/bin/env python3
"""Live multi-agent pipeline runner for ops-pilot.

Runs the full Monitor → Triage → Fix → Notify pipeline against a real
scenario using the Anthropic API. Each agent step is printed as it completes.

Usage:
    ANTHROPIC_API_KEY=sk-ant-... python run_pipeline.py
    ANTHROPIC_API_KEY=sk-ant-... python run_pipeline.py --scenario null_pointer_auth
    ANTHROPIC_API_KEY=sk-ant-... python run_pipeline.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime

# Ensure project root is on path when run directly
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from shared.models import DiffSummary, Failure, FailureDetail, PipelineInfo
from agents.triage_agent import TriageAgent
from agents.fix_agent import FixAgent
from agents.notify_agent import NotifyAgent
from shared.config import load_config
from shared.memory_store import MemoryStore, make_memory_record
from shared.task_queue import TaskQueue
from shared.state_store import StateStore
from shared.tenant_context import make_tenant_context


SCENARIOS_DIR = Path(__file__).parent / "demo" / "scenarios"

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
CYAN = "\033[96m"

SEVERITY_COLOR = {
    "low": DIM,
    "medium": YELLOW,
    "high": RED,
    "critical": f"\033[95m",  # magenta
}


def banner(text: str, color: str = BLUE) -> None:
    width = 64
    print(f"\n{color}{BOLD}{'─' * width}")
    print(f"  {text}")
    print(f"{'─' * width}{RESET}")


def step(agent: str, msg: str, color: str = CYAN) -> None:
    ts = datetime.utcnow().strftime("%H:%M:%S")
    print(f"{DIM}[{ts}]{RESET} {color}{BOLD}{agent:12}{RESET} {msg}")


def load_scenario(scenario_id: str) -> Failure:
    path = SCENARIOS_DIR / f"{scenario_id}.json"
    if not path.exists():
        print(f"{RED}Scenario '{scenario_id}' not found in {SCENARIOS_DIR}{RESET}")
        sys.exit(1)
    data = json.loads(path.read_text())
    return Failure(
        id=data["id"],
        pipeline=PipelineInfo(**data["pipeline"]),
        failure=FailureDetail(**data["failure"]),
        diff_summary=DiffSummary(**data["diff_summary"]),
    )


def list_scenarios() -> None:
    print(f"\n{BOLD}Available scenarios:{RESET}")
    for path in sorted(SCENARIOS_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        print(f"  {CYAN}{data['id']}{RESET}  —  {data['label']}")
    print()


def run_pipeline(scenario_id: str, dry_run: bool = False) -> int:
    """Run the full agent pipeline and return exit code."""

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and not dry_run:
        print(f"{RED}ANTHROPIC_API_KEY is not set.{RESET}")
        print("Set it with:  export ANTHROPIC_API_KEY=sk-ant-...")
        print("Or run demo mode:  DEMO_MODE=true uvicorn demo.app:app --reload")
        return 1

    failure = load_scenario(scenario_id)
    config = load_config()
    tenant_ctx = make_tenant_context(config)
    memory_store = MemoryStore()
    store = StateStore(path=f"ops_pilot_state_{scenario_id}.json")
    queue = TaskQueue()

    banner(f"ops-pilot  ·  live run  ·  {scenario_id}", BLUE)
    print(f"  Repo:    {BOLD}{failure.pipeline.repo}{RESET}")
    print(f"  Branch:  {failure.pipeline.branch}")
    print(f"  Commit:  {failure.pipeline.commit}  —  {failure.pipeline.commit_message}")
    print(f"  Job:     {failure.failure.job} / {failure.failure.step}")
    print(f"  Model:   {os.environ.get('CLAUDE_MODEL', 'claude-sonnet-4-6')}")

    # ── 1. Monitor ────────────────────────────────────────────────────────────
    banner("1 / 4  MONITOR", CYAN)
    step("monitor", "CI failure detected — enqueuing for triage…")
    task_id = queue.enqueue(failure.model_dump(mode="json"))
    step("monitor", f"Task enqueued  id={task_id[:8]}…  {GREEN}✓{RESET}")
    store.set(failure.id, "monitor", {"task_id": task_id, "status": "complete"})

    # ── 2. Triage ─────────────────────────────────────────────────────────────
    banner("2 / 4  TRIAGE", YELLOW)
    step("triage", "Analysing logs + diff with Claude…")

    t0 = time.time()
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    triage_agent = TriageAgent(model=model, tenant_context=tenant_ctx)
    triage = triage_agent.run(failure)
    elapsed = time.time() - t0

    sev_color = SEVERITY_COLOR.get(triage.severity.value, "")
    step("triage", f"Severity:  {sev_color}{BOLD}{triage.severity.value.upper()}{RESET}")
    step("triage", f"Service:   {triage.affected_service}")
    step("triage", f"Commit:    {triage.regression_introduced_in}")
    step("triage", f"Confidence:{BOLD} {triage.fix_confidence}{RESET}")
    step("triage", f"Elapsed:   {elapsed:.1f}s  {GREEN}✓{RESET}")
    print(f"\n  {DIM}{triage.output}{RESET}\n")

    store.set(failure.id, "triage", triage.model_dump(mode="json"))
    queue.claim_next()

    # ── 3. Fix ────────────────────────────────────────────────────────────────
    banner("3 / 4  FIX", YELLOW)
    step("fix", "Generating patch + PR description with Claude…")

    t0 = time.time()
    fix_agent = FixAgent(model=model, demo_mode=True)  # demo_mode=True skips real GitHub API
    fix = fix_agent.run(failure, triage)
    elapsed = time.time() - t0

    step("fix", f"PR title:  {BOLD}{fix.pr_title}{RESET}")
    step("fix", f"PR URL:    {CYAN}{fix.pr_url}{RESET}")
    step("fix", f"Elapsed:   {elapsed:.1f}s  {GREEN}✓{RESET}")
    print(f"\n  {DIM}{fix.pr_body[:400]}{'…' if len(fix.pr_body) > 400 else ''}{RESET}\n")

    store.set(failure.id, "fix", fix.model_dump(mode="json"))

    # Record completed investigation to memory and usage tracker
    tenant_ctx.usage_tracker.record_incident()
    memory_record = make_memory_record(failure, triage, tenant_id=tenant_ctx.tenant_id)
    memory_store.append(memory_record)
    step("memory", f"Incident saved to memory  id={failure.id[:8]}…  {GREEN}✓{RESET}")

    # ── 4. Notify ─────────────────────────────────────────────────────────────
    banner("4 / 4  NOTIFY", YELLOW)
    step("notify", "Generating Slack message with Claude…")

    t0 = time.time()
    notify_agent = NotifyAgent(model=model, demo_mode=True, channel="#platform-alerts")
    alert = notify_agent.run(failure, triage, fix)
    elapsed = time.time() - t0

    step("notify", f"Channel:   {alert.channel}")
    step("notify", f"Elapsed:   {elapsed:.1f}s  {GREEN}✓{RESET}")
    print(f"\n  {BOLD}Slack message:{RESET}")
    print(f"  {alert.slack_message}\n")

    store.set(failure.id, "notify", alert.model_dump(mode="json"))

    # ── Summary ───────────────────────────────────────────────────────────────
    banner("PIPELINE COMPLETE", GREEN)
    all_data = store.get_all(failure.id)
    step("summary", f"State saved → ops_pilot_state_{scenario_id}.json")
    step("summary", f"{GREEN}{BOLD}All 4 agents completed successfully ✓{RESET}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ops-pilot live pipeline runner")
    parser.add_argument(
        "--scenario",
        default="null_pointer_auth",
        help="Scenario ID to run (default: null_pointer_auth)",
    )
    parser.add_argument("--list", action="store_true", help="List available scenarios")
    args = parser.parse_args()

    if args.list:
        list_scenarios()
        return

    sys.exit(run_pipeline(args.scenario))


if __name__ == "__main__":
    main()

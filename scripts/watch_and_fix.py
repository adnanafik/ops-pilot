#!/usr/bin/env python3
"""ops-pilot production watcher.

Reads ops-pilot.yml, monitors every configured pipeline for CI failures,
and runs Triage → Fix → Notify for each new failure.

Usage:
    python3 scripts/watch_and_fix.py
    python3 scripts/watch_and_fix.py --config /path/to/ops-pilot.yml
    python3 scripts/watch_and_fix.py --once        # process current failures and exit
    python3 scripts/watch_and_fix.py --dry-run     # triage only, no PRs or Slack
"""

from __future__ import annotations

import argparse
import logging
import sys
import os
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from providers.factory import make_provider
from shared.config import OpsPilotConfig, PipelineConfig, load_config
from shared.llm_backend import LLMBackend, make_backend
from shared.models import Failure, Severity
from shared.state_store import StateStore
from agents.coordinator_agent import CoordinatorAgent
from agents.fix_agent import FixAgent
from agents.investigation_router import InvestigationRouter
from agents.notify_agent import NotifyAgent
from agents.triage_agent import TriageAgent
from shared.context_budget import ContextBudget
from shared.memory_store import MemoryStore, make_memory_record

logger = logging.getLogger("ops-pilot")

RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED   = "\033[91m"
CYAN  = "\033[96m"
BLUE  = "\033[94m"

SEVERITY_COLOR = {
    Severity.LOW:      DIM,
    Severity.MEDIUM:   YELLOW,
    Severity.HIGH:     RED,
    Severity.CRITICAL: "\033[95m",
}


def hdr(text: str, color: str = BLUE) -> None:
    print(f"\n{color}{BOLD}{'─' * 60}\n  {text}\n{'─' * 60}{RESET}")


def step(agent: str, msg: str, color: str = CYAN) -> None:
    ts = datetime.utcnow().strftime("%H:%M:%S")
    print(f"{DIM}[{ts}]{RESET} {color}{BOLD}{agent:12}{RESET} {msg}")


def validate_env() -> None:
    """Exit with a clear message if required environment variables are missing."""
    demo_mode = os.getenv("DEMO_MODE", "false").lower() == "true"
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if demo_mode:
        return

    if not api_key:
        print(
            f"\n{RED}{BOLD}ops-pilot: missing required environment variable{RESET}\n\n"
            f"  {BOLD}ANTHROPIC_API_KEY{RESET} is not set.\n\n"
            f"Please set it in your .env file.\n"
            f"See Quickstart: https://github.com/adnanafik/ops-pilot#quickstart\n",
            file=sys.stderr,
        )
        sys.exit(1)


# ── Pipeline runner ────────────────────────────────────────────────────────────

# Context limit for claude-sonnet-4-6 (200k tokens). ContextBudget compacts
# history at 75% of this — ~150k tokens — before each model call.
_MODEL_CONTEXT_TOKENS = 200_000


def run_pipeline(
    failure: Failure,
    pipeline: PipelineConfig,
    cfg: OpsPilotConfig,
    backend: LLMBackend,
    dry_run: bool = False,
    memory_store: MemoryStore | None = None,
) -> None:
    """Run Triage → Fix → Notify for a single failure."""
    from providers.base import CIProvider

    hdr(f"{failure.pipeline.repo.split('/')[-1]}  ·  commit {failure.pipeline.commit}", BLUE)
    print(f"  Provider: {failure.pipeline.provider}")
    print(f"  Job:      {failure.failure.job} / {failure.failure.step}")
    print(f"  Branch:   {failure.pipeline.branch}")
    print(f"  Message:  {failure.pipeline.commit_message}")

    # Triage — router decides fast path (TriageAgent) or deep path (CoordinatorAgent)
    hdr("TRIAGE", YELLOW)
    router = InvestigationRouter()
    route = router.route(failure)
    step("triage", f"Route: {route}  —  Analysing with Claude…")
    t0 = time.time()

    try:
        provider_for_triage = make_provider(pipeline, cfg)
    except Exception:
        provider_for_triage = None

    context_budget = ContextBudget(max_tokens=_MODEL_CONTEXT_TOKENS)

    if route == "deep":
        triage = CoordinatorAgent(
            backend=backend,
            model=cfg.model,
            provider=provider_for_triage,
            memory_store=memory_store,
            context_budget=context_budget,
        ).run(failure)
    else:
        triage = TriageAgent(
            backend=backend,
            model=cfg.model,
            provider=provider_for_triage,
            context_budget=context_budget,
        ).run(failure)

    # Persist incident to memory store for future similarity retrieval
    if memory_store is not None:
        try:
            memory_store.append(make_memory_record(failure, triage))
            logger.debug("Memory: saved incident %s", failure.id)
        except Exception as exc:
            logger.warning("Memory: failed to save incident %s: %s", failure.id, exc)

    sev_color = SEVERITY_COLOR.get(triage.severity, "")
    step("triage", f"Severity: {sev_color}{BOLD}{triage.severity.value.upper()}{RESET}  "
                   f"confidence: {triage.fix_confidence}  ({time.time()-t0:.1f}s)  {GREEN}✓{RESET}")

    if _below_threshold(triage.severity, pipeline.severity_threshold):
        step("triage", f"{DIM}Below threshold ({pipeline.severity_threshold.value}) — skipping fix+notify{RESET}")
        return

    print(f"\n  {DIM}{triage.output}{RESET}\n")

    if dry_run:
        step("fix", f"{DIM}--dry-run: skipping PR and Slack{RESET}")
        return

    # Build provider for fix/notify (already used for monitoring, rebuild for clarity)
    try:
        provider = make_provider(pipeline, cfg)
        has_code_host = True
    except Exception:
        provider = None
        has_code_host = False

    # Fix
    hdr("FIX", YELLOW)
    step("fix", "Generating code fix + opening draft PR…")
    t0 = time.time()
    fix_agent = FixAgent(
        backend=backend,
        model=cfg.model,
        provider=provider,
        demo_mode=not has_code_host,
    )
    fix = fix_agent.run(failure, triage, base_branch=pipeline.base_branch)
    step("fix", f"PR: {CYAN}{fix.pr_url}{RESET}  ({time.time()-t0:.1f}s)  {GREEN}✓{RESET}")
    print(f"\n  {BOLD}{fix.pr_title}{RESET}\n")

    # Notify
    hdr("NOTIFY", YELLOW)
    step("notify", f"Sending to {pipeline.slack_channel}…")
    t0 = time.time()
    notify_agent = NotifyAgent(
        backend=backend,
        model=cfg.model,
        slack_bot_token=cfg.slack_bot_token,
        slack_webhook_url=cfg.slack_webhook_url,
        channel=pipeline.slack_channel,
        demo_mode=not cfg.has_slack,
    )
    notify_agent.run(failure, triage, fix)
    step("notify", f"Done  ({time.time()-t0:.1f}s)  {GREEN}✓{RESET}")

    hdr("DONE", GREEN)
    if fix.pr_url and "acme-corp" not in fix.pr_url:
        print(f"  {GREEN}{BOLD}PR → {fix.pr_url}{RESET}")
    if cfg.has_slack:
        print(f"  {GREEN}Slack → {pipeline.slack_channel}{RESET}")
    print()


def _below_threshold(severity: Severity, threshold: Severity) -> bool:
    order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
    return order.index(severity) < order.index(threshold)


# ── Main watch loop ────────────────────────────────────────────────────────────

def watch(cfg: OpsPilotConfig, once: bool = False, dry_run: bool = False) -> None:
    store = StateStore(cfg.state_file)
    backend = make_backend(cfg)
    memory_store = MemoryStore()

    print(f"\n{BOLD}ops-pilot{RESET}  —  monitoring {len(cfg.pipelines)} pipeline(s)")
    for p in cfg.pipelines:
        threshold_color = SEVERITY_COLOR.get(p.severity_threshold, "")
        print(f"  {CYAN}{p.repo}{RESET}  [{p.provider}]  →  {p.slack_channel}  "
              f"threshold: {threshold_color}{p.severity_threshold.value}{RESET}")

    if cfg.has_bedrock:
        llm_label = f"bedrock ({cfg.aws_region or 'default region'})"
    elif cfg.has_vertex:
        llm_label = f"vertex_ai ({cfg.gcp_region or 'us-east5'}, project={cfg.gcp_project or 'default'})"
    else:
        llm_label = "anthropic"
    print(f"\nSlack: {'bot token' if cfg.slack_bot_token else 'webhook' if cfg.slack_webhook_url else 'console (no token set)'}")
    print(f"LLM:   {llm_label}")
    print(f"Model: {cfg.model}")
    print(f"\nPolling every {cfg.poll_interval_seconds}s  (Ctrl+C to stop)\n")

    while True:
        for pipeline in cfg.pipelines:
            repo = pipeline.repo
            try:
                provider = make_provider(pipeline, cfg)
                open_prs = provider.get_open_fix_prs(repo)
                failures = provider.get_failures(repo)
            except Exception as exc:
                logger.warning("Failed to poll %s: %s", repo, exc)
                continue

            for failure in failures:
                run_id = failure.pipeline.run_id
                commit_sha = failure.pipeline.commit
                processed_key = f"processed_{run_id}"

                # GitHub/GitLab is source of truth — open PR for this commit → wait
                if commit_sha in open_prs:
                    pr = open_prs[commit_sha]
                    print(f"{DIM}  ↩ {repo}@{commit_sha} — PR #{pr['number']} open, waiting for review{RESET}")
                    store.set("runs", processed_key, {"skipped": True})
                    continue

                if store.get("runs", processed_key):
                    continue

                print(f"{YELLOW}▶ Failure:{RESET} {repo}  [{pipeline.provider}]  commit {commit_sha}  run {run_id}")
                try:
                    run_pipeline(failure, pipeline, cfg, backend=backend, dry_run=dry_run, memory_store=memory_store)
                    store.set("runs", processed_key, {
                        "repo": repo,
                        "provider": pipeline.provider,
                        "run_id": run_id,
                        "commit": commit_sha,
                        "processed_at": datetime.utcnow().isoformat(),
                    })
                except Exception as exc:
                    print(f"{RED}Error processing {repo} run {run_id}: {exc}{RESET}")
                    logger.exception("Pipeline failed for %s run %s", repo, run_id)

        if once:
            break
        time.sleep(cfg.poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="ops-pilot production watcher")
    parser.add_argument("--config", help="Path to ops-pilot.yml")
    parser.add_argument("--once", action="store_true", help="Process current failures and exit")
    parser.add_argument("--dry-run", action="store_true", help="Triage only — no PRs or Slack")
    args = parser.parse_args()
    validate_env()
    cfg = load_config(args.config)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not cfg.pipelines:
        print(f"{RED}No pipelines configured.{RESET}")
        print("Create an ops-pilot.yml (see ops-pilot.example.yml) or set OPS_PILOT_CONFIG.")
        sys.exit(1)

    watch(cfg, once=args.once, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

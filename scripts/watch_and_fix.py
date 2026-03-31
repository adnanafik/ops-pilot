#!/usr/bin/env python3
"""ops-pilot production watcher.

Reads ops-pilot.yml, monitors every configured pipeline for CI failures,
and runs Monitor → Triage → Fix → Notify for each new failure.

Usage:
    python3 scripts/watch_and_fix.py
    python3 scripts/watch_and_fix.py --config /path/to/ops-pilot.yml
    python3 scripts/watch_and_fix.py --once        # process current failures and exit
    python3 scripts/watch_and_fix.py --dry-run     # triage only, no PRs or Slack
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import httpx

from shared.config import OpsPilotConfig, PipelineConfig, load_config
from shared.models import DiffSummary, Failure, FailureDetail, PipelineInfo, Severity
from shared.state_store import StateStore
from agents.triage_agent import TriageAgent
from agents.fix_agent import FixAgent
from agents.notify_agent import NotifyAgent

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ops-pilot")

GITHUB_API = "https://api.github.com"

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BLUE = "\033[94m"

SEVERITY_COLOR = {
    Severity.LOW: DIM,
    Severity.MEDIUM: YELLOW,
    Severity.HIGH: RED,
    Severity.CRITICAL: "\033[95m",
}


def hdr(text: str, color: str = BLUE) -> None:
    print(f"\n{color}{BOLD}{'─' * 60}\n  {text}\n{'─' * 60}{RESET}")


def step(agent: str, msg: str, color: str = CYAN) -> None:
    ts = datetime.utcnow().strftime("%H:%M:%S")
    print(f"{DIM}[{ts}]{RESET} {color}{BOLD}{agent:12}{RESET} {msg}")


# ── GitHub helpers ─────────────────────────────────────────────────────────────

def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_failed_runs(repo: str, token: str) -> list[dict]:
    """Return the latest failed run per workflow, ignoring ops-pilot branches."""
    url = f"{GITHUB_API}/repos/{repo}/actions/runs"
    with httpx.Client(timeout=20) as http:
        resp = http.get(url, headers=gh_headers(token), params={"status": "failure", "per_page": 10})
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])

    # Never process failures on ops-pilot's own fix branches
    runs = [r for r in runs if not r.get("head_branch", "").startswith("ops-pilot/")]

    # One failure per workflow (newest first)
    seen: set[str] = set()
    latest: list[dict] = []
    for run in runs:
        name = run.get("name", run.get("path", "unknown"))
        if name not in seen:
            seen.add(name)
            latest.append(run)
    return latest


def get_failed_job(repo: str, run_id: int, token: str) -> dict:
    url = f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/jobs"
    with httpx.Client(timeout=20) as http:
        resp = http.get(url, headers=gh_headers(token))
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        return next((j for j in jobs if j["conclusion"] == "failure"), jobs[0] if jobs else {})


def get_job_logs(repo: str, job_id: int, token: str) -> list[str]:
    url = f"{GITHUB_API}/repos/{repo}/actions/jobs/{job_id}/logs"
    with httpx.Client(timeout=30, follow_redirects=True) as http:
        resp = http.get(url, headers=gh_headers(token))
        if resp.status_code != 200:
            return ["(logs not available)"]
        lines = []
        for line in resp.text.splitlines()[-60:]:
            if len(line) > 29 and line[10] == "T":
                line = line[29:].strip()
            if line:
                lines.append(line)
        return lines


def get_open_ops_pilot_prs(repo: str, token: str) -> dict[str, dict]:
    """Return open ops-pilot PRs keyed by commit SHA (from branch name)."""
    url = f"{GITHUB_API}/repos/{repo}/pulls"
    with httpx.Client(timeout=20) as http:
        resp = http.get(url, headers=gh_headers(token), params={"state": "open", "per_page": 20})
        if resp.status_code != 200:
            return {}
    result = {}
    for pr in resp.json():
        ref = pr.get("head", {}).get("ref", "")
        if ref.startswith("ops-pilot/fix-"):
            result[ref.replace("ops-pilot/fix-", "")] = pr
    return result


def build_failure(repo: str, run: dict, token: str) -> Failure:
    triggered_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    updated_at = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
    duration = int((updated_at - triggered_at).total_seconds())

    job = get_failed_job(repo, run["id"], token)
    job_name = job.get("name", "unknown-job")
    failed_step = next(
        (s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"),
        "unknown step",
    )
    log_tail = get_job_logs(repo, job["id"], token) if job.get("id") else ["(no logs)"]
    commit = run.get("head_commit", {})

    return Failure(
        id=f"{repo.replace('/', '_')}_{run['id']}",
        pipeline=PipelineInfo(
            provider="github_actions",
            repo=repo,
            workflow=run.get("path", "").split("/")[-1] or run.get("name", "ci.yml"),
            run_id=str(run["id"]),
            branch=run["head_branch"],
            commit=run["head_sha"][:7],
            commit_message=(commit.get("message", "").splitlines()[0] if commit else ""),
            author=commit.get("author", {}).get("email", "unknown"),
            triggered_at=triggered_at,
            failed_at=updated_at,
            duration_seconds=duration,
        ),
        failure=FailureDetail(
            job=job_name,
            step=failed_step,
            exit_code=1,
            log_tail=log_tail,
        ),
        diff_summary=DiffSummary(
            files_changed=[],
            lines_added=0,
            lines_removed=0,
            key_change="(detected from live CI run)",
        ),
    )


# ── Pipeline runner ────────────────────────────────────────────────────────────

def run_pipeline(
    failure: Failure,
    pipeline: PipelineConfig,
    cfg: OpsPilotConfig,
    dry_run: bool = False,
) -> None:
    """Run Triage → Fix → Notify for a single failure."""

    hdr(f"{failure.pipeline.repo.split('/')[-1]}  ·  commit {failure.pipeline.commit}", BLUE)
    print(f"  Job:     {failure.failure.job} / {failure.failure.step}")
    print(f"  Branch:  {failure.pipeline.branch}")
    print(f"  Message: {failure.pipeline.commit_message}")

    # Triage
    hdr("TRIAGE", YELLOW)
    step("triage", "Analysing with Claude…")
    t0 = time.time()
    triage = TriageAgent(model=cfg.model).run(failure)

    sev_color = SEVERITY_COLOR.get(triage.severity, "")
    step("triage", f"Severity: {sev_color}{BOLD}{triage.severity.value.upper()}{RESET}  "
                   f"confidence: {triage.fix_confidence}  ({time.time()-t0:.1f}s)  {GREEN}✓{RESET}")

    # Enforce severity threshold — don't act on noise
    if _below_threshold(triage.severity, pipeline.severity_threshold):
        step("triage", f"{DIM}Below threshold ({pipeline.severity_threshold.value}) — skipping fix+notify{RESET}")
        return

    print(f"\n  {DIM}{triage.output}{RESET}\n")

    if dry_run:
        step("fix", f"{DIM}--dry-run: skipping PR and Slack{RESET}")
        return

    # Fix
    hdr("FIX", YELLOW)
    step("fix", "Generating code fix + opening draft PR…")
    t0 = time.time()
    fix_agent = FixAgent(
        model=cfg.model,
        github_token=cfg.github_token,
        demo_mode=not cfg.has_github,
    )
    fix = fix_agent.run(failure, triage)
    step("fix", f"PR: {CYAN}{fix.pr_url}{RESET}  ({time.time()-t0:.1f}s)  {GREEN}✓{RESET}")
    print(f"\n  {BOLD}{fix.pr_title}{RESET}\n")

    # Notify
    hdr("NOTIFY", YELLOW)
    step("notify", f"Sending to {pipeline.slack_channel}…")
    t0 = time.time()
    notify_agent = NotifyAgent(
        model=cfg.model,
        slack_bot_token=cfg.slack_bot_token,
        slack_webhook_url=cfg.slack_webhook_url,
        channel=pipeline.slack_channel,
        demo_mode=not cfg.has_slack,
    )
    alert = notify_agent.run(failure, triage, fix)
    step("notify", f"Done  ({time.time()-t0:.1f}s)  {GREEN}✓{RESET}")

    hdr("DONE", GREEN)
    if cfg.has_github and "acme-corp" not in fix.pr_url:
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

    print(f"\n{BOLD}ops-pilot{RESET}  —  monitoring {len(cfg.pipelines)} pipeline(s)")
    for p in cfg.pipelines:
        threshold_color = SEVERITY_COLOR.get(p.severity_threshold, "")
        print(f"  {CYAN}{p.repo}{RESET}  →  {p.slack_channel}  "
              f"threshold: {threshold_color}{p.severity_threshold.value}{RESET}")
    print(f"\nSlack: {'bot token' if cfg.slack_bot_token else 'webhook' if cfg.slack_webhook_url else 'console (no token set)'}")
    print(f"PRs:   {'enabled' if cfg.has_github else 'mock (no GITHUB_TOKEN)'}")
    print(f"Model: {cfg.model}")
    print(f"\nPolling every {cfg.poll_interval_seconds}s  (Ctrl+C to stop)\n")

    while True:
        for pipeline in cfg.pipelines:
            repo = pipeline.repo
            try:
                open_prs = get_open_ops_pilot_prs(repo, cfg.github_token)
                runs = get_failed_runs(repo, cfg.github_token)
            except Exception as exc:
                logger.warning("Failed to poll %s: %s", repo, exc)
                continue

            for run in runs:
                run_id = str(run["id"])
                commit_sha = run["head_sha"][:7]
                processed_key = f"processed_{run_id}"

                # GitHub is source of truth — open PR for this commit means we're waiting
                if commit_sha in open_prs:
                    pr = open_prs[commit_sha]
                    print(f"{DIM}  ↩ {repo}@{commit_sha} — PR #{pr['number']} open, waiting for review{RESET}")
                    store.set("runs", processed_key, {"skipped": True})
                    continue

                if store.get("runs", processed_key):
                    continue

                print(f"{YELLOW}▶ Failure:{RESET} {repo}  commit {commit_sha}  run {run_id}")
                try:
                    failure = build_failure(repo, run, cfg.github_token)
                    run_pipeline(failure, pipeline, cfg, dry_run=dry_run)
                    store.set("runs", processed_key, {
                        "repo": repo,
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

    cfg = load_config(args.config)

    if not cfg.pipelines:
        print(f"{RED}No pipelines configured.{RESET}")
        print(f"Create an ops-pilot.yml (see ops-pilot.example.yml) or set OPS_PILOT_CONFIG.")
        sys.exit(1)

    watch(cfg, once=args.once, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

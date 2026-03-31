"""MonitorAgent — polls GitHub Actions for CI failures.

In live mode (DEMO_MODE=false) it queries the GitHub Actions API for failed
workflow runs, converts them into Failure models, and enqueues them for
triage. In demo mode it loads pre-recorded scenario files instead.

The agent is designed to run on a schedule (cron or a simple polling loop)
and is stateless between runs — deduplication is handled by the task queue
(tasks with the same run_id are not re-enqueued if already present).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx

from agents.base_agent import BaseAgent
from shared.models import (
    AgentStatus,
    DiffSummary,
    Failure,
    FailureDetail,
    PipelineInfo,
)
from shared.task_queue import TaskQueue

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class MonitorAgent(BaseAgent[list[Failure]]):
    """Monitors GitHub Actions for CI failures and enqueues triage tasks.

    Args:
        repo:        ``owner/repo`` to monitor (required in live mode).
        github_token: GitHub personal access token. Falls back to
                     ``GITHUB_TOKEN`` environment variable.
        task_queue:  Queue to enqueue failures into. Created automatically if
                     not provided.
        demo_mode:   If True, loads scenario files instead of calling GitHub.
        scenarios_dir: Path to ``demo/scenarios/`` directory.
    """

    def __init__(
        self,
        repo: str | None = None,
        github_token: str | None = None,
        task_queue: TaskQueue | None = None,
        demo_mode: bool = True,
        scenarios_dir: str = "demo/scenarios",
        **kwargs,
    ) -> None:
        """Initialize the monitor with repository and auth configuration."""
        super().__init__(**kwargs)
        self.repo = repo
        self.github_token = github_token or os.environ.get("GITHUB_TOKEN", "")
        self.task_queue = task_queue or TaskQueue()
        self.demo_mode = demo_mode
        self.scenarios_dir = Path(scenarios_dir)

    def describe(self) -> str:
        """Return a one-line description of this agent's responsibility."""
        return "Polls GitHub Actions for CI failures and enqueues triage tasks"

    def run(self) -> list[Failure]:
        """Check for new CI failures and enqueue them for triage.

        Returns:
            List of Failure models that were detected and enqueued.
        """
        self._status = AgentStatus.RUNNING
        logger.info("MonitorAgent: starting poll (demo_mode=%s)", self.demo_mode)

        try:
            if self.demo_mode:
                failures = self._load_demo_scenarios()
            else:
                failures = self._poll_github_actions()

            for failure in failures:
                self.task_queue.enqueue(failure.model_dump(mode="json"))
                logger.info(
                    "MonitorAgent: enqueued failure %s for repo %s",
                    failure.id,
                    failure.pipeline.repo,
                )

            self._status = AgentStatus.COMPLETE
            logger.info("MonitorAgent: enqueued %d failure(s)", len(failures))
            return failures

        except Exception as exc:
            self._status = AgentStatus.FAILED
            logger.error("MonitorAgent: failed — %s", exc)
            raise

    def _load_demo_scenarios(self) -> list[Failure]:
        """Load pre-recorded scenario files from the scenarios directory."""
        failures: list[Failure] = []
        for scenario_file in sorted(self.scenarios_dir.glob("*.json")):
            try:
                data = json.loads(scenario_file.read_text())
                failure = Failure(
                    id=data["id"],
                    pipeline=PipelineInfo(**data["pipeline"]),
                    failure=FailureDetail(**data["failure"]),
                    diff_summary=DiffSummary(**data["diff_summary"]),
                )
                failures.append(failure)
            except Exception as exc:
                logger.warning(
                    "MonitorAgent: skipping %s — %s", scenario_file.name, exc
                )
        return failures

    def _poll_github_actions(self) -> list[Failure]:
        """Query the GitHub Actions API for recent failed workflow runs.

        Returns newly-failed runs not already in the task queue.
        """
        if not self.repo:
            raise ValueError("MonitorAgent: 'repo' is required in live mode")
        if not self.github_token:
            raise ValueError("MonitorAgent: GITHUB_TOKEN is required in live mode")

        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        url = f"{GITHUB_API}/repos/{self.repo}/actions/runs"
        params = {"status": "failure", "per_page": 10}

        with httpx.Client(timeout=30) as http:
            response = http.get(url, headers=headers, params=params)
            response.raise_for_status()
            runs = response.json().get("workflow_runs", [])

        failures: list[Failure] = []
        for run in runs:
            failure_id = f"gh_{run['id']}"
            # Skip if already queued
            if self.task_queue.get(failure_id):
                continue

            failure = self._run_to_failure(run, headers, http if False else None)
            if failure:
                failures.append(failure)

        return failures

    def _run_to_failure(
        self,
        run: dict,
        headers: dict,
        _http: None,
    ) -> Failure | None:
        """Convert a GitHub Actions run dict to a Failure model.

        Makes additional API calls to fetch job logs.
        """
        with httpx.Client(timeout=30) as http:
            # Fetch failed jobs
            jobs_url = run["jobs_url"]
            jobs_resp = http.get(jobs_url, headers=headers)
            jobs_resp.raise_for_status()
            jobs = jobs_resp.json().get("jobs", [])

            failed_job = next((j for j in jobs if j["conclusion"] == "failure"), None)
            if not failed_job:
                return None

            failed_step = next(
                (s for s in failed_job.get("steps", []) if s["conclusion"] == "failure"),
                {"name": "unknown"},
            )

            # Fetch logs (truncated to last 50 lines)
            logs_url = f"{GITHUB_API}/repos/{self.repo}/actions/jobs/{failed_job['id']}/logs"
            logs_resp = http.get(logs_url, headers=headers)
            log_lines = []
            if logs_resp.status_code == 200:
                log_lines = logs_resp.text.splitlines()[-50:]

        triggered_at = datetime.fromisoformat(
            run["created_at"].replace("Z", "+00:00")
        )
        failed_at = datetime.fromisoformat(
            run["updated_at"].replace("Z", "+00:00")
        )
        duration = int((failed_at - triggered_at).total_seconds())

        return Failure(
            id=f"gh_{run['id']}",
            pipeline=PipelineInfo(
                provider="github_actions",
                repo=self.repo or "",
                workflow=run["path"].split("/")[-1],
                run_id=str(run["id"]),
                branch=run["head_branch"],
                commit=run["head_sha"][:7],
                commit_message=run["head_commit"]["message"].splitlines()[0],
                author=run["head_commit"]["author"]["email"],
                triggered_at=triggered_at,
                failed_at=failed_at,
                duration_seconds=duration,
            ),
            failure=FailureDetail(
                job=failed_job["name"],
                step=failed_step.get("name", "unknown"),
                exit_code=1,
                log_tail=log_lines,
            ),
            diff_summary=DiffSummary(
                files_changed=[],
                lines_added=0,
                lines_removed=0,
                key_change="(live run — diff not pre-fetched)",
            ),
        )


def main() -> None:
    """Entry point for running the monitor from the CLI."""
    import sys

    logging.basicConfig(level=logging.INFO)
    demo_mode = os.environ.get("DEMO_MODE", "true").lower() != "false"
    repo = os.environ.get("GITHUB_REPO")

    agent = MonitorAgent(repo=repo, demo_mode=demo_mode)
    failures = agent.run()
    print(f"MonitorAgent: detected {len(failures)} failure(s)")
    for f in failures:
        print(f"  - {f.id}: {f.pipeline.repo} @ {f.pipeline.commit}")

    sys.exit(0 if agent.status.value == "complete" else 1)

"""Jenkins provider for ops-pilot.

Jenkins is a build server only — it has no git hosting or PR/MR concept.
This provider splits responsibility:
  - CI data (get_failures):    polls the Jenkins JSON API
  - Git ops (everything else): delegates to an embedded code-host provider
                               (GitHubProvider or GitLabProvider)

Configuration example in ops-pilot.yml:
    pipelines:
      - repo: myorg/backend          # code-host owner/repo for PRs
        provider: jenkins
        code_host: github            # or gitlab
        jenkins_url: https://ci.example.com
        jenkins_job: folder/my-job   # job path within Jenkins
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from providers.base import CIProvider
from shared.models import (
    DiffSummary,
    Failure,
    FailureDetail,
    PipelineInfo,
)

logger = logging.getLogger(__name__)

# How many recent builds to inspect for failures
_BUILD_DEPTH = 5


class JenkinsProvider(CIProvider):
    """CI provider that polls Jenkins for build failures.

    All git/PR operations are delegated to the embedded code_host provider
    so the rest of the system sees a uniform interface.

    Args:
        url:               Jenkins base URL, e.g. 'https://ci.example.com'.
        job:               Jenkins job path, e.g. 'folder/my-job'.
        user:              Jenkins username.
        token:             Jenkins API token (not password).
        code_host:         Fully-configured GitHubProvider or GitLabProvider
                           used for all git/file/PR operations.
    """

    def __init__(
        self,
        url: str,
        job: str,
        user: str,
        token: str,
        code_host: CIProvider,
    ) -> None:
        self._url = url.rstrip("/")
        self._job = job
        self._auth = (user, token)
        self._code_host = code_host

    def provider_name(self) -> str:
        return "jenkins"

    # ── CI data ────────────────────────────────────────────────────────────────

    def get_failures(self, repo: str) -> list[Failure]:
        """Poll Jenkins JSON API for failed builds.

        Args:
            repo: The code-host owner/repo slug (used in Failure.pipeline.repo).
                  The Jenkins job is taken from self._job set at construction.
        """
        job_path = "/job/".join(self._job.split("/"))
        url = f"{self._url}/job/{job_path}/api/json"
        tree = f"builds[number,result,timestamp,duration,fullDisplayName,url,changeSet[items[comment,commitId,authorEmail]]]{{0,{_BUILD_DEPTH}}}"

        try:
            with httpx.Client(timeout=20) as http:
                resp = http.get(url, auth=self._auth, params={"tree": tree})
                if resp.status_code == 404:
                    return []
                resp.raise_for_status()
                builds = resp.json().get("builds", [])
        except Exception as exc:
            logger.warning("JenkinsProvider: could not fetch builds for %s — %s", self._job, exc)
            return []

        failures: list[Failure] = []
        for build in builds:
            if build.get("result") != "FAILURE":
                continue
            try:
                failure = self._build_failure(repo, build)
                if failure:
                    failures.append(failure)
            except Exception as exc:
                logger.warning(
                    "JenkinsProvider: skipping build #%s — %s", build.get("number"), exc
                )
            # One failure at a time (latest failed build)
            break

        return failures

    def get_open_fix_prs(self, repo: str) -> dict[str, dict]:
        """Delegate to code host — Jenkins has no PR concept."""
        return self._code_host.get_open_fix_prs(repo)

    def _get_console_log(self, build_url: str) -> list[str]:
        """Fetch last 60 non-empty lines of a build's console output."""
        url = f"{build_url.rstrip('/')}/consoleText"
        try:
            with httpx.Client(timeout=30) as http:
                resp = http.get(url, auth=self._auth)
                if resp.status_code != 200:
                    return ["(logs not available)"]
            return [line for line in resp.text.splitlines()[-60:] if line.strip()]
        except Exception:
            return ["(logs not available)"]

    def _build_failure(self, repo: str, build: dict) -> Failure | None:
        """Convert a Jenkins build dict to a Failure model."""
        build_url = build.get("url", "")
        log_tail = self._get_console_log(build_url)

        # Timestamp is milliseconds since epoch
        ts_ms = build.get("timestamp", 0)
        triggered_at = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        duration_ms = build.get("duration", 0)
        failed_at = datetime.fromtimestamp((ts_ms + duration_ms) / 1000, tz=UTC)

        # Extract commit info from changeSet if present
        change_items = build.get("changeSet", {}).get("items", [])
        last_change = change_items[0] if change_items else {}
        commit_sha = last_change.get("commitId", str(build.get("number", "0")))[:7]
        author = last_change.get("authorEmail", "unknown")
        commit_message = (last_change.get("comment") or "").splitlines()[0] if last_change else ""

        build_number = build.get("number", 0)

        return Failure(
            id=f"jenkins_{self._job.replace('/', '_')}_{build_number}",
            pipeline=PipelineInfo(
                provider="jenkins",
                repo=repo,
                workflow=self._job,
                run_id=str(build_number),
                branch="main",  # Jenkins doesn't always expose branch — override in config
                commit=commit_sha,
                commit_message=commit_message,
                author=author,
                triggered_at=triggered_at,
                failed_at=failed_at,
                duration_seconds=int(duration_ms / 1000),
            ),
            failure=FailureDetail(
                job=build.get("fullDisplayName", self._job),
                step="build",
                exit_code=1,
                log_tail=log_tail,
            ),
            diff_summary=DiffSummary(
                files_changed=[],
                lines_added=0,
                lines_removed=0,
                key_change="(detected from Jenkins build failure)",
            ),
        )

    # ── Git ops — all delegated to code host ───────────────────────────────────

    def get_file(self, repo: str, path: str, ref: str = "HEAD") -> tuple[str, str]:
        """Delegate to code host."""
        return self._code_host.get_file(repo, path, ref)

    def get_repo_tree(
        self,
        repo: str,
        ref: str = "HEAD",
        extensions: tuple[str, ...] | None = None,
    ) -> list[str]:
        """Delegate to code host."""
        return self._code_host.get_repo_tree(repo, ref, extensions)

    def create_branch(self, repo: str, branch: str, from_ref: str) -> None:
        """Delegate to code host."""
        return self._code_host.create_branch(repo, branch, from_ref)

    def update_file(
        self,
        repo: str,
        path: str,
        content: str,
        blob_id: str,
        branch: str,
        commit_message: str,
    ) -> None:
        """Delegate to code host."""
        return self._code_host.update_file(repo, path, content, blob_id, branch, commit_message)

    def open_draft_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> tuple[str, int]:
        """Delegate to code host."""
        return self._code_host.open_draft_pr(repo, title, body, head, base)

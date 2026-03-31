"""GitHub Actions + GitHub API provider for ops-pilot.

Consolidates all GitHub-specific HTTP logic previously spread across
watch_and_fix.py, fix_agent.py, and monitor_agent.py into one place.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime

import httpx

from providers.base import CIProvider
from shared.models import (
    DiffSummary,
    Failure,
    FailureDetail,
    PipelineInfo,
)

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubProvider(CIProvider):
    """CI provider for GitHub Actions with GitHub as the code host.

    Args:
        token: GitHub personal access token (classic PAT with repo +
               workflow scopes, or fine-grained with Contents write +
               Pull requests write).
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def provider_name(self) -> str:
        return "github_actions"

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ── CI data ────────────────────────────────────────────────────────────────

    def get_failures(self, repo: str) -> list[Failure]:
        """Fetch latest failed GitHub Actions runs, one per workflow."""
        url = f"{GITHUB_API}/repos/{repo}/actions/runs"
        with httpx.Client(timeout=20) as http:
            resp = http.get(
                url,
                headers=self._headers(),
                params={"status": "failure", "per_page": 10},
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            runs = resp.json().get("workflow_runs", [])

        # Never process failures on ops-pilot's own fix branches
        runs = [r for r in runs if not r.get("head_branch", "").startswith("ops-pilot/")]

        # Deduplicate — one failure per workflow name, newest first
        seen: set[str] = set()
        latest: list[dict] = []
        for run in runs:
            name = run.get("name", run.get("path", "unknown"))
            if name not in seen:
                seen.add(name)
                latest.append(run)

        failures: list[Failure] = []
        for run in latest:
            try:
                failure = self._build_failure(repo, run)
                if failure:
                    failures.append(failure)
            except Exception as exc:
                logger.warning("GitHubProvider: skipping run %s — %s", run.get("id"), exc)

        return failures

    def get_open_fix_prs(self, repo: str) -> dict[str, dict]:
        """Return open ops-pilot fix PRs keyed by commit SHA."""
        url = f"{GITHUB_API}/repos/{repo}/pulls"
        try:
            with httpx.Client(timeout=20) as http:
                resp = http.get(
                    url,
                    headers=self._headers(),
                    params={"state": "open", "per_page": 20},
                )
                if resp.status_code != 200:
                    return {}
                prs = resp.json()
        except Exception as exc:
            logger.warning("GitHubProvider: could not fetch open PRs for %s — %s", repo, exc)
            return {}

        result: dict[str, dict] = {}
        for pr in prs:
            ref = pr.get("head", {}).get("ref", "")
            if ref.startswith("ops-pilot/fix-"):
                sha = ref.replace("ops-pilot/fix-", "")
                result[sha] = pr
        return result

    def _get_failed_job(self, repo: str, run_id: int) -> dict:
        """Return the first failed job for a run."""
        url = f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/jobs"
        with httpx.Client(timeout=20) as http:
            resp = http.get(url, headers=self._headers())
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
        return next((j for j in jobs if j["conclusion"] == "failure"), jobs[0] if jobs else {})

    def _get_job_logs(self, repo: str, job_id: int) -> list[str]:
        """Fetch and return the last 60 non-empty log lines for a job."""
        url = f"{GITHUB_API}/repos/{repo}/actions/jobs/{job_id}/logs"
        with httpx.Client(timeout=30, follow_redirects=True) as http:
            resp = http.get(url, headers=self._headers())
            if resp.status_code != 200:
                return ["(logs not available)"]
        lines = []
        for line in resp.text.splitlines()[-60:]:
            # Strip GitHub's ISO timestamp prefix (first 29 chars when col 10 == 'T')
            if len(line) > 29 and line[10] == "T":
                line = line[29:].strip()
            if line:
                lines.append(line)
        return lines

    def _build_failure(self, repo: str, run: dict) -> Failure | None:
        """Convert a GitHub Actions run dict to a Failure model."""
        triggered_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        updated_at = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
        duration = int((updated_at - triggered_at).total_seconds())

        job = self._get_failed_job(repo, run["id"])
        if not job:
            return None

        job_name = job.get("name", "unknown-job")
        failed_step = next(
            (s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"),
            "unknown step",
        )
        log_tail = self._get_job_logs(repo, job["id"]) if job.get("id") else ["(no logs)"]
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

    # ── Git / file operations ──────────────────────────────────────────────────

    def get_file(self, repo: str, path: str, ref: str = "HEAD") -> tuple[str, str]:
        """Fetch file content and blob SHA from GitHub."""
        url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
        with httpx.Client(timeout=30) as http:
            resp = http.get(url, headers=self._headers(), params={"ref": ref})
            if resp.status_code == 404:
                raise FileNotFoundError(f"{path} not found in {repo}@{ref}")
            resp.raise_for_status()
            data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]

    def get_repo_tree(
        self,
        repo: str,
        ref: str = "HEAD",
        extensions: tuple[str, ...] | None = None,
    ) -> list[str]:
        """Return all file paths in the repo, optionally filtered by extension."""
        url = f"{GITHUB_API}/repos/{repo}/git/trees/{ref}"
        with httpx.Client(timeout=30) as http:
            resp = http.get(url, headers=self._headers(), params={"recursive": "1"})
            resp.raise_for_status()
            tree = resp.json().get("tree", [])

        paths = [item["path"] for item in tree if item["type"] == "blob"]
        if extensions:
            paths = [p for p in paths if any(p.endswith(ext) for ext in extensions)]
        return sorted(paths)

    def create_branch(self, repo: str, branch: str, from_ref: str) -> None:
        """Create a branch from from_ref; silently succeeds if it already exists."""
        # First resolve from_ref to a SHA if it looks like a branch name
        sha = self._resolve_ref(repo, from_ref)

        url = f"{GITHUB_API}/repos/{repo}/git/refs"
        with httpx.Client(timeout=30) as http:
            resp = http.post(
                url,
                headers=self._headers(),
                json={"ref": f"refs/heads/{branch}", "sha": sha},
            )
            if resp.status_code == 422:
                # Branch already exists — that's fine
                logger.debug("GitHubProvider: branch %s already exists", branch)
                return
            resp.raise_for_status()

    def _resolve_ref(self, repo: str, ref: str) -> str:
        """Resolve a branch name to its HEAD commit SHA."""
        url = f"{GITHUB_API}/repos/{repo}/git/ref/heads/{ref}"
        with httpx.Client(timeout=30) as http:
            resp = http.get(url, headers=self._headers())
            resp.raise_for_status()
        return resp.json()["object"]["sha"]

    def update_file(
        self,
        repo: str,
        path: str,
        content: str,
        blob_id: str,
        branch: str,
        commit_message: str,
    ) -> None:
        """Commit an updated file to an existing branch via the Contents API."""
        url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        with httpx.Client(timeout=30) as http:
            resp = http.put(
                url,
                headers=self._headers(),
                json={
                    "message": commit_message,
                    "content": encoded,
                    "sha": blob_id,
                    "branch": branch,
                },
            )
            resp.raise_for_status()

    def open_draft_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> tuple[str, int]:
        """Open a draft PR; return (url, number) of existing PR on 422."""
        url = f"{GITHUB_API}/repos/{repo}/pulls"
        with httpx.Client(timeout=30) as http:
            resp = http.post(
                url,
                headers=self._headers(),
                json={"title": title, "body": body, "head": head, "base": base, "draft": True},
            )
            if resp.status_code == 422:
                # PR already open for this branch — fetch and return it
                owner = repo.split("/")[0]
                existing = http.get(
                    url,
                    headers=self._headers(),
                    params={"head": f"{owner}:{head}", "state": "open"},
                )
                existing.raise_for_status()
                prs = existing.json()
                if prs:
                    return prs[0]["html_url"], prs[0]["number"]
                raise RuntimeError(f"422 on PR creation but no open PR found for {head}")
            resp.raise_for_status()
            data = resp.json()
        return data["html_url"], data["number"]

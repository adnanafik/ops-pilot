"""GitLab CI + GitLab API provider for ops-pilot.

Supports both gitlab.com and self-hosted GitLab instances.
Uses the GitLab REST API v4.

GitLab terminology mapping:
  GitHub PR      → GitLab Merge Request (MR)
  GitHub Actions → GitLab CI/CD pipelines
  blob SHA       → not needed (GitLab file API uses path + branch)
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from urllib.parse import quote

import httpx

from providers.base import CIProvider
from shared.models import (
    DiffSummary,
    Failure,
    FailureDetail,
    PipelineInfo,
)

logger = logging.getLogger(__name__)


class GitLabProvider(CIProvider):
    """CI provider for GitLab CI/CD with GitLab as the code host.

    Args:
        token:    GitLab personal access token or project access token
                  with api + read_repository + write_repository scopes.
        base_url: GitLab instance base URL. Defaults to 'https://gitlab.com'.
    """

    def __init__(self, token: str, base_url: str = "https://gitlab.com") -> None:
        self._token = token
        self._api = f"{base_url.rstrip('/')}/api/v4"

    def provider_name(self) -> str:
        return "gitlab_ci"

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._token}

    @staticmethod
    def _encode(repo: str) -> str:
        """URL-encode 'group/project' for use in API paths."""
        return quote(repo, safe="")

    # ── CI data ────────────────────────────────────────────────────────────────

    def get_failures(self, repo: str) -> list[Failure]:
        """Fetch latest failed GitLab CI pipelines, one per pipeline name."""
        pid = self._encode(repo)
        url = f"{self._api}/projects/{pid}/pipelines"

        with httpx.Client(timeout=20) as http:
            resp = http.get(
                url,
                headers=self._headers(),
                params={"status": "failed", "per_page": 10, "order_by": "updated_at"},
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            pipelines = resp.json()

        # Exclude ops-pilot fix branches
        pipelines = [p for p in pipelines if not p.get("ref", "").startswith("ops-pilot/")]

        # Deduplicate by pipeline name (use ref as proxy — GitLab has no workflow name)
        seen: set[str] = set()
        latest: list[dict] = []
        for p in pipelines:
            key = p.get("ref", p.get("id", "unknown"))
            if key not in seen:
                seen.add(key)
                latest.append(p)

        failures: list[Failure] = []
        for pipeline in latest:
            try:
                failure = self._build_failure(repo, pipeline, http_client=None)
                if failure:
                    failures.append(failure)
            except Exception as exc:
                logger.warning(
                    "GitLabProvider: skipping pipeline %s — %s", pipeline.get("id"), exc
                )
        return failures

    def get_open_fix_prs(self, repo: str) -> dict[str, dict]:
        """Return open ops-pilot fix MRs keyed by commit SHA."""
        pid = self._encode(repo)
        url = f"{self._api}/projects/{pid}/merge_requests"
        try:
            with httpx.Client(timeout=20) as http:
                resp = http.get(
                    url,
                    headers=self._headers(),
                    params={"state": "opened", "per_page": 20},
                )
                if resp.status_code != 200:
                    return {}
                mrs = resp.json()
        except Exception as exc:
            logger.warning("GitLabProvider: could not fetch open MRs for %s — %s", repo, exc)
            return {}

        result: dict[str, dict] = {}
        for mr in mrs:
            ref = mr.get("source_branch", "")
            if ref.startswith("ops-pilot/fix-"):
                sha = ref.replace("ops-pilot/fix-", "")
                # Normalise to match GitHub PR shape consumed by the watch loop
                result[sha] = {"number": mr["iid"], "html_url": mr["web_url"], **mr}
        return result

    def _get_failed_job(self, repo: str, pipeline_id: int) -> dict | None:
        """Return the first failed job for a pipeline."""
        pid = self._encode(repo)
        url = f"{self._api}/projects/{pid}/pipelines/{pipeline_id}/jobs"
        with httpx.Client(timeout=20) as http:
            resp = http.get(
                url, headers=self._headers(), params={"scope[]": "failed"}
            )
            resp.raise_for_status()
            jobs = resp.json()
        return jobs[0] if jobs else None

    def _get_job_logs(self, repo: str, job_id: int) -> list[str]:
        """Fetch the last 60 non-empty log lines for a job."""
        pid = self._encode(repo)
        url = f"{self._api}/projects/{pid}/jobs/{job_id}/trace"
        with httpx.Client(timeout=30) as http:
            resp = http.get(url, headers=self._headers())
            if resp.status_code != 200:
                return ["(logs not available)"]
        lines = [line for line in resp.text.splitlines()[-60:] if line.strip()]
        return lines

    def _build_failure(
        self, repo: str, pipeline: dict, http_client: None
    ) -> Failure | None:
        """Convert a GitLab pipeline dict to a Failure model."""
        job = self._get_failed_job(repo, pipeline["id"])
        if not job:
            return None

        log_tail = self._get_job_logs(repo, job["id"])

        created_at = datetime.fromisoformat(
            pipeline["created_at"].replace("Z", "+00:00")
        )
        updated_at = datetime.fromisoformat(
            pipeline["updated_at"].replace("Z", "+00:00")
        )
        duration = int((updated_at - created_at).total_seconds())

        # GitLab pipeline sha is the full commit SHA
        commit_sha = pipeline.get("sha", "")[:7]

        return Failure(
            id=f"{repo.replace('/', '_')}_{pipeline['id']}",
            pipeline=PipelineInfo(
                provider="gitlab_ci",
                repo=repo,
                workflow=job.get("stage", "build"),
                run_id=str(pipeline["id"]),
                branch=pipeline.get("ref", "main"),
                commit=commit_sha,
                commit_message=pipeline.get("name", ""),
                author=pipeline.get("user", {}).get("username", "unknown"),
                triggered_at=created_at,
                failed_at=updated_at,
                duration_seconds=duration,
            ),
            failure=FailureDetail(
                job=job.get("name", "unknown-job"),
                step=job.get("stage", "unknown"),
                exit_code=job.get("exit_code", 1) or 1,
                log_tail=log_tail,
            ),
            diff_summary=DiffSummary(
                files_changed=[],
                lines_added=0,
                lines_removed=0,
                key_change="(detected from live GitLab CI pipeline)",
            ),
        )

    # ── Git / file operations ──────────────────────────────────────────────────

    def get_file(self, repo: str, path: str, ref: str = "HEAD") -> tuple[str, str]:
        """Fetch file content from GitLab. blob_id is empty (not needed for updates)."""
        pid = self._encode(repo)
        encoded_path = quote(path, safe="")
        url = f"{self._api}/projects/{pid}/repository/files/{encoded_path}"
        with httpx.Client(timeout=30) as http:
            resp = http.get(url, headers=self._headers(), params={"ref": ref})
            if resp.status_code == 404:
                raise FileNotFoundError(f"{path} not found in {repo}@{ref}")
            resp.raise_for_status()
            data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, ""  # GitLab doesn't need blob SHA for updates

    def get_repo_tree(
        self,
        repo: str,
        ref: str = "HEAD",
        extensions: tuple[str, ...] | None = None,
    ) -> list[str]:
        """Return all file paths in the repo via GitLab's recursive tree API."""
        pid = self._encode(repo)
        url = f"{self._api}/projects/{pid}/repository/tree"
        paths: list[str] = []

        page = 1
        with httpx.Client(timeout=30) as http:
            while True:
                resp = http.get(
                    url,
                    headers=self._headers(),
                    params={"ref": ref, "recursive": "true", "per_page": 100, "page": page},
                )
                resp.raise_for_status()
                items = resp.json()
                if not items:
                    break
                for item in items:
                    if item["type"] == "blob":
                        paths.append(item["path"])
                if len(items) < 100:
                    break
                page += 1

        if extensions:
            paths = [p for p in paths if any(p.endswith(ext) for ext in extensions)]
        return sorted(paths)

    def create_branch(self, repo: str, branch: str, from_ref: str) -> None:
        """Create a branch from from_ref; silently succeeds if it already exists."""
        pid = self._encode(repo)
        url = f"{self._api}/projects/{pid}/repository/branches"
        with httpx.Client(timeout=30) as http:
            resp = http.post(
                url,
                headers=self._headers(),
                json={"branch": branch, "ref": from_ref},
            )
            if resp.status_code in (400, 422):
                # 400 = already exists in GitLab
                logger.debug("GitLabProvider: branch %s already exists", branch)
                return
            resp.raise_for_status()

    def update_file(
        self,
        repo: str,
        path: str,
        content: str,
        blob_id: str,  # unused for GitLab
        branch: str,
        commit_message: str,
    ) -> None:
        """Commit an updated file to an existing branch via the Files API."""
        pid = self._encode(repo)
        encoded_path = quote(path, safe="")
        url = f"{self._api}/projects/{pid}/repository/files/{encoded_path}"
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        with httpx.Client(timeout=30) as http:
            resp = http.put(
                url,
                headers=self._headers(),
                json={
                    "branch": branch,
                    "content": encoded,
                    "encoding": "base64",
                    "commit_message": commit_message,
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
        """Open a draft MR; return (url, iid) of existing MR on 409."""
        pid = self._encode(repo)
        url = f"{self._api}/projects/{pid}/merge_requests"
        with httpx.Client(timeout=30) as http:
            resp = http.post(
                url,
                headers=self._headers(),
                json={
                    "title": f"Draft: {title}",
                    "description": body,
                    "source_branch": head,
                    "target_branch": base,
                    "draft": True,
                    "remove_source_branch": False,
                },
            )
            if resp.status_code in (409, 422):
                # MR already exists — fetch it
                existing = http.get(
                    url,
                    headers=self._headers(),
                    params={"source_branch": head, "state": "opened"},
                )
                existing.raise_for_status()
                mrs = existing.json()
                if mrs:
                    return mrs[0]["web_url"], mrs[0]["iid"]
                raise RuntimeError(f"Conflict on MR creation but no open MR for {head}")
            resp.raise_for_status()
            data = resp.json()
        return data["web_url"], data["iid"]

"""FixAgent — generates fix suggestions and opens draft PRs.

Given a Triage (root cause analysis), this agent:
1. Fetches the broken file content from GitHub.
2. Uses the LLM to generate the fixed file content.
3. Pushes the fix to a new branch via the GitHub Contents API.
4. Opens a draft PR — humans review and merge.

In demo mode (no GITHUB_TOKEN), returns plausible mock PR details.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime
from typing import Optional

import httpx

from agents.base_agent import BaseAgent
from shared.models import AgentStatus, Failure, Fix, Triage

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

PR_SYSTEM_PROMPT = """You are an expert software engineer generating a precise, minimal code fix for a CI failure.

Your job:
1. Based on the triage analysis and failure details, produce a fix.
2. Write a clear PR title (under 72 chars) and a well-structured PR body.
3. The PR body must use this structure:
   ## Problem
   ## Root cause
   ## Fix (include a code diff if helpful)
   ## Tests
   ---
   *This PR was opened automatically by ops-pilot.*

Output valid JSON only. No markdown fences around the JSON."""

PR_SCHEMA = """{
  "pr_title": "<imperative mood, under 72 chars>",
  "pr_body": "<full markdown PR body>",
  "summary": "<one sentence summary of what the fix does>"
}"""

CODE_FIX_SYSTEM_PROMPT = """You are an expert software engineer. You will be given a broken source file and a description of the bug.
Return ONLY the complete fixed file content — no explanation, no markdown fences, just the raw fixed source code.
Make the minimal change needed to fix the bug. Do not reformat or refactor unrelated code."""

FILE_INFERENCE_SYSTEM_PROMPT = """You are an expert software engineer. Given a CI failure triage and a list of files in the repository, identify which source file(s) need to be changed to fix the bug.

Output valid JSON only — no markdown fences.
Schema: {"files": ["path/to/file.py", ...]}
List only files that need editing. Maximum 3 files. Prefer the most likely single file."""


class FixAgent(BaseAgent[Fix]):
    """Generates a fix, pushes a branch, and opens a draft PR on GitHub.

    Args:
        github_token: GitHub PAT with repo write access. Falls back to
                      ``GITHUB_TOKEN`` environment variable.
        demo_mode:    If True, skips GitHub API calls and returns mock PR details.
    """

    def __init__(
        self,
        github_token: Optional[str] = None,
        demo_mode: bool = True,
        **kwargs,
    ) -> None:
        """Initialize the fix agent with optional GitHub credentials."""
        super().__init__(**kwargs)
        self.github_token = github_token or os.environ.get("GITHUB_TOKEN", "")
        self.demo_mode = demo_mode

    def describe(self) -> str:
        """Return a one-line description of this agent's responsibility."""
        return "Generates fix patches, pushes a branch, and opens draft PRs on GitHub"

    def run(self, failure: Failure, triage: Triage) -> Fix:
        """Generate a fix and open a draft PR.

        Args:
            failure: The original CI failure.
            triage:  Root cause analysis from TriageAgent.

        Returns:
            Fix model with PR details.
        """
        self._status = AgentStatus.RUNNING
        logger.info("FixAgent: generating fix for %s", failure.id)

        try:
            pr_data = self._generate_pr_content(failure, triage)

            if self.demo_mode or not self.github_token:
                pr_url, pr_number = self._mock_pr_url(failure)
            else:
                pr_url, pr_number = self._push_fix_and_open_pr(
                    failure=failure,
                    triage=triage,
                    pr_title=pr_data["pr_title"],
                    pr_body=pr_data["pr_body"],
                )

            fix = Fix(
                failure_id=failure.id,
                output=f"Fix generated and draft PR opened. {pr_data['summary']}",
                pr_title=pr_data["pr_title"],
                pr_body=pr_data["pr_body"],
                pr_url=pr_url,
                pr_number=pr_number,
                timestamp=datetime.utcnow(),
            )

            self._status = AgentStatus.COMPLETE
            logger.info("FixAgent: PR ready — %s", pr_url)
            return fix

        except Exception as exc:
            self._status = AgentStatus.FAILED
            logger.error("FixAgent: failed — %s", exc)
            raise

    # ── PR content generation ──────────────────────────────────────────────

    def _generate_pr_content(self, failure: Failure, triage: Triage) -> dict:
        """Call the LLM to generate the PR title and body."""
        log_tail = "\n".join(failure.failure.log_tail[-20:])
        files_changed = ", ".join(failure.diff_summary.files_changed)

        user_message = f"""## Failure to fix

**Repository:** {failure.pipeline.repo}
**Commit:** {failure.pipeline.commit} — "{failure.pipeline.commit_message}"
**Job:** {failure.failure.job}

### Triage Analysis
{triage.output}

**Severity:** {triage.severity.value}
**Affected service:** {triage.affected_service}
**Regression introduced in:** {triage.regression_introduced_in}
**Fix confidence:** {triage.fix_confidence}

### Key diff change
Files: {files_changed}
{failure.diff_summary.key_change}

### Failing log (last 20 lines)
```
{log_tail}
```

### Expected JSON output schema
{PR_SCHEMA}

Respond with ONLY the JSON object."""

        raw = self._call_llm(
            system=PR_SYSTEM_PROMPT,
            user=user_message,
            max_tokens=1500,
        )
        return self._parse_response(raw)

    def _generate_code_fix(self, broken_content: str, triage: Triage, filename: str) -> str:
        """Ask the LLM to return the fixed version of a source file."""
        user_message = f"""File: {filename}

Bug description:
{triage.output}

Key change that introduced the bug:
{triage.regression_introduced_in} — see diff summary.

Broken file content:
```
{broken_content}
```

Return the complete fixed file content only."""

        raw = self._call_llm(
            system=CODE_FIX_SYSTEM_PROMPT,
            user=user_message,
            max_tokens=3000,
        ).strip()
        # Strip markdown fences — the LLM sometimes wraps output in ```python ... ```
        # even when instructed not to. Writing fences into source files breaks CI.
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(
                line for line in lines if not line.startswith("```")
            ).strip()
        return raw

    # ── Real GitHub integration ────────────────────────────────────────────

    def _push_fix_and_open_pr(
        self,
        failure: Failure,
        triage: Triage,
        pr_title: str,
        pr_body: str,
    ) -> tuple[str, int]:
        """Push a fix branch with real code changes and open a draft PR.

        Steps:
        1. Fetch the broken file(s) from GitHub.
        2. Ask Claude to generate the fixed content.
        3. Push the fixed file(s) to a new branch via Contents API.
        4. Open a draft PR.
        """
        repo = failure.pipeline.repo
        headers = self._gh_headers()
        fix_branch = f"ops-pilot/fix-{failure.pipeline.commit[:7]}"

        # Get the SHA of the base branch HEAD to branch from
        base_sha = self._get_branch_sha(repo, "main", headers)

        # Create the fix branch
        self._create_branch(repo, fix_branch, base_sha, headers)
        logger.info("FixAgent: created branch %s", fix_branch)

        # Determine which files to fix — use diff info or ask Claude
        files_to_fix = failure.diff_summary.files_changed or self._infer_files_to_fix(repo, triage, headers)

        # Fix each file
        fixed_any = False
        for filepath in files_to_fix:
            try:
                file_content, file_sha = self._get_file(repo, filepath, headers)
                fixed_content = self._generate_code_fix(file_content, triage, filepath)
                self._update_file(
                    repo=repo,
                    path=filepath,
                    content=fixed_content,
                    sha=file_sha,
                    branch=fix_branch,
                    message=f"fix: {pr_title}",
                    headers=headers,
                )
                logger.info("FixAgent: fixed %s on branch %s", filepath, fix_branch)
                fixed_any = True
            except Exception as exc:
                logger.warning("FixAgent: could not fix %s — %s", filepath, exc)

        if not fixed_any:
            raise RuntimeError(f"Could not commit any fix to branch {fix_branch} — no files patched")

        # Open the draft PR
        pr_url, pr_number = self._open_draft_pr(
            repo=repo,
            title=pr_title,
            body=pr_body,
            head=fix_branch,
            base="main",
            headers=headers,
        )
        return pr_url, pr_number

    def _infer_files_to_fix(self, repo: str, triage: Triage, headers: dict) -> list[str]:
        """Ask Claude to identify which file(s) to fix when diff info is unavailable."""
        # Get the repo file tree (top 2 levels only to keep prompt small)
        url = f"{GITHUB_API}/repos/{repo}/git/trees/HEAD"
        with httpx.Client(timeout=30) as http:
            resp = http.get(url, headers=headers, params={"recursive": "1"})
            resp.raise_for_status()
            all_paths = [
                item["path"] for item in resp.json().get("tree", [])
                if item["type"] == "blob" and item["path"].endswith((".py", ".txt", ".toml", ".yml", ".yaml"))
                and not item["path"].startswith((".github", "tests/", "test_"))
            ]

        user_message = f"""CI failure triage:
{triage.output}

Affected service: {triage.affected_service}
Regression in: {triage.regression_introduced_in}

Repository files:
{chr(10).join(all_paths[:60])}

Which file(s) need to be edited to fix this bug?"""

        raw = self._call_llm(system=FILE_INFERENCE_SYSTEM_PROMPT, user=user_message, max_tokens=200)
        try:
            data = json.loads(raw.strip().strip("```").strip())
            files = data.get("files", [])
            logger.info("FixAgent: inferred files to fix: %s", files)
            return files
        except Exception as exc:
            logger.warning("FixAgent: could not infer files — %s", exc)
            return []

    def _gh_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get_branch_sha(self, repo: str, branch: str, headers: dict) -> str:
        """Get the HEAD commit SHA of a branch."""
        url = f"{GITHUB_API}/repos/{repo}/git/ref/heads/{branch}"
        with httpx.Client(timeout=30) as http:
            resp = http.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()["object"]["sha"]

    def _create_branch(self, repo: str, branch: str, sha: str, headers: dict) -> None:
        """Create a new branch from a given SHA."""
        url = f"{GITHUB_API}/repos/{repo}/git/refs"
        with httpx.Client(timeout=30) as http:
            resp = http.post(url, headers=headers, json={
                "ref": f"refs/heads/{branch}",
                "sha": sha,
            })
            if resp.status_code == 422:
                # Branch already exists — that's fine
                logger.debug("FixAgent: branch %s already exists", branch)
                return
            resp.raise_for_status()

    def _get_file(self, repo: str, path: str, headers: dict) -> tuple[str, str]:
        """Fetch file content and its blob SHA from GitHub. Returns (content, sha)."""
        url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
        with httpx.Client(timeout=30) as http:
            resp = http.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, data["sha"]

    def _update_file(
        self,
        repo: str,
        path: str,
        content: str,
        sha: str,
        branch: str,
        message: str,
        headers: dict,
    ) -> None:
        """Commit an updated file to a branch via the Contents API."""
        url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        with httpx.Client(timeout=30) as http:
            resp = http.put(url, headers=headers, json={
                "message": message,
                "content": encoded,
                "sha": sha,
                "branch": branch,
            })
            resp.raise_for_status()

    def _open_draft_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        headers: dict,
    ) -> tuple[str, int]:
        """Open a draft PR and return (html_url, pr_number).

        If the branch already has an open PR (422), return the existing one.
        """
        url = f"{GITHUB_API}/repos/{repo}/pulls"
        with httpx.Client(timeout=30) as http:
            resp = http.post(url, headers=headers, json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "draft": True,
            })
            if resp.status_code == 422:
                # PR already exists for this branch — fetch and return it
                logger.info("FixAgent: PR already exists for %s, fetching existing", head)
                existing = http.get(url, headers=headers, params={"head": f"{repo.split('/')[0]}:{head}", "state": "open"})
                existing.raise_for_status()
                prs = existing.json()
                if prs:
                    return prs[0]["html_url"], prs[0]["number"]
                raise RuntimeError(f"422 on PR creation but no open PR found for branch {head}")
            resp.raise_for_status()
            data = resp.json()
        return data["html_url"], data["number"]

    # ── Helpers ────────────────────────────────────────────────────────────

    def _parse_response(self, raw: str) -> dict:
        """Parse JSON from LLM response, stripping accidental markdown fences."""
        text = raw.strip()
        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines() if not line.startswith("```")
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            logger.error("FixAgent: failed to parse JSON: %s\n%s", exc, raw)
            raise ValueError(f"LLM returned non-JSON: {exc}") from exc

    def _mock_pr_url(self, failure: Failure) -> tuple[str, int]:
        """Return a plausible mock PR URL for demo mode."""
        pr_number = int(failure.pipeline.run_id[-4:]) % 9000 + 1000
        url = f"https://github.com/{failure.pipeline.repo}/pull/{pr_number}"
        return url, pr_number

"""FixAgent — generates fix suggestions and opens draft PRs/MRs.

Given a Triage (root cause analysis), this agent:
1. Determines which files to fix (from diff info or by asking Claude).
2. Fetches the broken file content via the CIProvider.
3. Uses the LLM to generate the fixed file content.
4. Pushes the fix to a new branch and opens a draft PR/MR via the provider.

In demo mode (no provider), returns plausible mock PR details.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from agents.base_agent import BaseAgent
from providers.base import CIProvider
from shared.models import AgentStatus, Failure, Fix, Triage

logger = logging.getLogger(__name__)

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
    """Generates a fix, pushes a branch, and opens a draft PR/MR via the provider.

    Args:
        provider:  CIProvider instance for all git/PR operations. When None,
                   falls back to demo mode with mock PR details.
        demo_mode: If True, skips provider calls and returns mock PR details.
                   Automatically True when provider is None.
    """

    def __init__(
        self,
        provider: CIProvider | None = None,
        demo_mode: bool = True,
        **kwargs,
    ) -> None:
        """Initialize the fix agent with an optional provider."""
        super().__init__(**kwargs)
        self.provider = provider
        self.demo_mode = demo_mode or provider is None

    def describe(self) -> str:
        """Return a one-line description of this agent's responsibility."""
        return "Generates fix patches, pushes a branch, and opens draft PRs/MRs via the CI provider"

    def run(self, failure: Failure, triage: Triage, base_branch: str = "main") -> Fix:
        """Generate a fix and open a draft PR/MR.

        Args:
            failure:     The original CI failure.
            triage:      Root cause analysis from TriageAgent.
            base_branch: Branch to open the PR/MR against.

        Returns:
            Fix model with PR/MR details.
        """
        self._status = AgentStatus.RUNNING
        logger.info("FixAgent: generating fix for %s", failure.id)

        try:
            pr_data = self._generate_pr_content(failure, triage)

            if self.demo_mode or not self.provider:
                pr_url, pr_number = self._mock_pr_url(failure)
            else:
                pr_url, pr_number = self._push_fix_and_open_pr(
                    failure=failure,
                    triage=triage,
                    pr_title=pr_data["pr_title"],
                    pr_body=pr_data["pr_body"],
                    base_branch=base_branch,
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

    # ── PR content generation ──────────────────────────────────────────────────

    def _generate_pr_content(self, failure: Failure, triage: Triage) -> dict:
        """Call the LLM to generate the PR title, body, and summary."""
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

        raw = self._call_llm(system=PR_SYSTEM_PROMPT, user=user_message, max_tokens=1500)
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
            system=CODE_FIX_SYSTEM_PROMPT, user=user_message, max_tokens=3000
        ).strip()

        # Strip markdown fences — the LLM sometimes wraps output in ```python ... ```
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(line for line in lines if not line.startswith("```")).strip()
        return raw

    # ── Provider-backed git operations ─────────────────────────────────────────

    def _push_fix_and_open_pr(
        self,
        failure: Failure,
        triage: Triage,
        pr_title: str,
        pr_body: str,
        base_branch: str = "main",
    ) -> tuple[str, int]:
        """Push fix files to a branch and open a draft PR/MR via the provider."""
        assert self.provider is not None
        repo = failure.pipeline.repo
        fix_branch = f"ops-pilot/fix-{failure.pipeline.commit[:7]}"

        # Create the fix branch from the base branch
        self.provider.create_branch(repo, fix_branch, from_ref=base_branch)
        logger.info("FixAgent: created branch %s", fix_branch)

        # Determine which files to fix — use diff info or ask Claude
        files_to_fix = (
            failure.diff_summary.files_changed
            or self._infer_files_to_fix(repo, triage)
        )

        fixed_any = False
        for filepath in files_to_fix:
            try:
                file_content, blob_id = self.provider.get_file(repo, filepath)
                fixed_content = self._generate_code_fix(file_content, triage, filepath)
                self.provider.update_file(
                    repo=repo,
                    path=filepath,
                    content=fixed_content,
                    blob_id=blob_id,
                    branch=fix_branch,
                    commit_message=f"fix: {pr_title}",
                )
                logger.info("FixAgent: fixed %s on branch %s", filepath, fix_branch)
                fixed_any = True
            except Exception as exc:
                logger.warning("FixAgent: could not fix %s — %s", filepath, exc)

        if not fixed_any:
            raise RuntimeError(f"Could not commit any fix to branch {fix_branch} — no files patched")

        return self.provider.open_draft_pr(
            repo=repo,
            title=pr_title,
            body=pr_body,
            head=fix_branch,
            base=base_branch,
        )

    def _infer_files_to_fix(self, repo: str, triage: Triage) -> list[str]:
        """Ask Claude which file(s) to fix when diff info is unavailable."""
        assert self.provider is not None
        all_paths = self.provider.get_repo_tree(
            repo,
            extensions=(".py", ".txt", ".toml", ".yml", ".yaml"),
        )
        # Exclude test files and hidden dirs — focus on source
        source_paths = [
            p for p in all_paths
            if not p.startswith((".github", "tests/", "test_", "docs/"))
        ]

        user_message = f"""CI failure triage:
{triage.output}

Affected service: {triage.affected_service}
Regression in: {triage.regression_introduced_in}

Repository source files:
{chr(10).join(source_paths[:60])}

Which file(s) need to be edited to fix this bug?"""

        raw = self._call_llm(
            system=FILE_INFERENCE_SYSTEM_PROMPT, user=user_message, max_tokens=200
        )
        try:
            text = raw.strip()
            if text.startswith("```"):
                text = "\n".join(line for line in text.splitlines() if not line.startswith("```"))
            data = json.loads(text)
            files = data.get("files", [])
            logger.info("FixAgent: inferred files to fix: %s", files)
            return files
        except Exception as exc:
            logger.warning("FixAgent: could not infer files — %s", exc)
            return []

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _parse_response(self, raw: str) -> dict:
        """Parse JSON from LLM response, stripping accidental markdown fences."""
        text = raw.strip()
        if text.startswith("```"):
            text = "\n".join(line for line in text.splitlines() if not line.startswith("```"))
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

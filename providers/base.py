"""Abstract CIProvider interface for ops-pilot.

Every concrete provider implements this interface. The rest of the system
(watch loop, FixAgent, MonitorAgent) depends only on this contract — no
platform-specific code leaks upward.

Two orthogonal concerns:
  - CI data:  fetching failures, checking for open fix PRs/MRs
  - Git ops:  reading files, creating branches, opening draft PRs/MRs

For Jenkins, which has no git hosting, CI data comes from Jenkins and all
git ops are delegated to an embedded GitHubProvider or GitLabProvider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from shared.models import Failure


class CIProvider(ABC):
    """Abstract base class for all CI/CD platform providers."""

    # ── Identity ───────────────────────────────────────────────────────────────

    def provider_name(self) -> str:
        """Human-readable provider identifier written into PipelineInfo.provider.

        Defaults to the lowercase class name minus 'Provider'.
        """
        return type(self).__name__.removesuffix("Provider").lower()

    # ── CI data ────────────────────────────────────────────────────────────────

    @abstractmethod
    def get_failures(self, repo: str) -> list[Failure]:
        """Fetch the latest set of failed CI runs for the given repo.

        Implementations must:
        - Return at most one failure per workflow/pipeline name (newest wins).
        - Exclude runs on branches whose name starts with 'ops-pilot/'.
        - Embed the last 50–60 log lines in FailureDetail.log_tail.
        - Return an empty list on 404 — never raise for missing repos.

        Args:
            repo: Repository slug ('owner/repo' for GitHub/GitLab, job path
                  for Jenkins e.g. 'folder/job-name').

        Returns:
            List of Failure models, newest-first.
        """

    @abstractmethod
    def get_open_fix_prs(self, repo: str) -> dict[str, dict]:
        """Return open ops-pilot fix PRs/MRs keyed by commit SHA.

        The key is the 7-char commit SHA extracted from the fix branch name
        ('ops-pilot/fix-<sha>'). The value is the raw PR/MR API payload.

        Args:
            repo: Repository slug.

        Returns:
            Mapping of commit_sha -> raw PR/MR dict.
            Empty dict on any API error (must not raise).
        """

    # ── Git / file operations ──────────────────────────────────────────────────

    @abstractmethod
    def get_file(self, repo: str, path: str, ref: str = "HEAD") -> tuple[str, str]:
        """Fetch a file's decoded text content and its platform blob identifier.

        The blob identifier is needed by update_file to avoid conflicts.
        For GitHub this is the blob SHA; for GitLab pass an empty string.

        Args:
            repo: Repository slug.
            path: File path relative to repo root.
            ref:  Git ref to read from.

        Returns:
            (decoded_content, blob_id)

        Raises:
            FileNotFoundError: If the file does not exist at the given ref.
        """

    @abstractmethod
    def get_repo_tree(
        self,
        repo: str,
        ref: str = "HEAD",
        extensions: tuple[str, ...] | None = None,
    ) -> list[str]:
        """Return the list of file paths present in the repo at the given ref.

        Used by FixAgent when diff_summary.files_changed is empty — Claude
        picks which files to edit from this tree.

        Args:
            repo:       Repository slug.
            ref:        Git ref.
            extensions: If given, only paths with matching suffix are returned.

        Returns:
            Sorted list of file paths (blobs only).
        """

    @abstractmethod
    def create_branch(self, repo: str, branch: str, from_ref: str) -> None:
        """Create a new branch pointing at the given ref.

        Must be idempotent: silently return if the branch already exists.

        Args:
            repo:     Repository slug.
            branch:   New branch name, e.g. 'ops-pilot/fix-abc1234'.
            from_ref: Branch name or full SHA to branch from.
        """

    @abstractmethod
    def update_file(
        self,
        repo: str,
        path: str,
        content: str,
        blob_id: str,
        branch: str,
        commit_message: str,
    ) -> None:
        """Commit updated file content to an existing branch.

        Args:
            repo:           Repository slug.
            path:           File path relative to repo root.
            content:        New decoded text content.
            blob_id:        Blob identifier from get_file (may be empty for GitLab).
            branch:         Target branch name.
            commit_message: Commit message.
        """

    @abstractmethod
    def open_draft_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> tuple[str, int]:
        """Open a draft PR or MR and return its URL and number.

        Must be idempotent: if a PR/MR already exists for head, return it.

        Args:
            repo:  Repository slug.
            title: PR/MR title.
            body:  Full markdown description.
            head:  Source branch (ops-pilot fix branch).
            base:  Target branch (typically 'main').

        Returns:
            (html_url, pr_or_mr_number)
        """

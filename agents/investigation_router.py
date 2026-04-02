"""InvestigationRouter — routes CI failures to the appropriate investigation path.

The routing decision is explicit and logged. When an incident is processed, the
router produces a record of which path was chosen and why — giving engineers (and
Phase 7's audit trail) a clear decision point to inspect and tune.

Routing paths:
  'fast' → TriageAgent: single AgentLoop, one context window, ~10 turns.
           Use for simple failures: single file changed, small diff, short log.
  'deep' → CoordinatorAgent: three parallel workers, isolated contexts.
           Use for complex failures: multiple files changed, large diff, rich logs.

Heuristics (Phase 3 — no LLM call needed):
  - Many files changed → regression likely spans modules → workers need to correlate
  - Large diff → more surface area, harder for a single loop to cover
  - Long log tail → rich evidence worth distributing across specialist workers

Phase 4+ can upgrade the router to LLM-based classification backed by incident
memory retrieval. The interface (route() → Literal['fast', 'deep']) stays the same.
"""

from __future__ import annotations

import logging
from typing import Literal

from shared.models import Failure

logger = logging.getLogger(__name__)

Route = Literal["fast", "deep"]

# Default thresholds. Tunable at construction time for repos with different norms.
_FILES_THRESHOLD = 3        # files changed in the diff
_DIFF_LINES_THRESHOLD = 100 # total lines added + removed
_LOG_LINES_THRESHOLD = 40   # lines in the log tail


class InvestigationRouter:
    """Routes CI failures to either the fast or deep investigation path.

    The router is the visible decision boundary in the pipeline. Its choice is
    always logged, so engineers can see exactly why a particular strategy was
    selected for a given incident.

    Args:
        files_threshold:      Route deep if this many or more files were changed.
        diff_lines_threshold: Route deep if total diff size meets or exceeds this.
        log_lines_threshold:  Route deep if the log tail has this many or more lines.
    """

    def __init__(
        self,
        files_threshold: int = _FILES_THRESHOLD,
        diff_lines_threshold: int = _DIFF_LINES_THRESHOLD,
        log_lines_threshold: int = _LOG_LINES_THRESHOLD,
    ) -> None:
        self._files_threshold = files_threshold
        self._diff_lines_threshold = diff_lines_threshold
        self._log_lines_threshold = log_lines_threshold

    def route(self, failure: Failure) -> Route:
        """Decide whether to use the fast or deep investigation path.

        Heuristics are evaluated in order of signal quality: file count is the
        strongest signal (multi-file regressions need parallel context), diff
        size is next, log length is last.

        Args:
            failure: The CI failure to route.

        Returns:
            'fast' → use TriageAgent (single loop, faster, cheaper)
            'deep' → use CoordinatorAgent (parallel workers, deeper coverage)
        """
        reason = self._deep_reason(failure)
        if reason:
            logger.info(
                "InvestigationRouter: failure=%s → deep (%s)", failure.id, reason
            )
            return "deep"

        logger.info("InvestigationRouter: failure=%s → fast", failure.id)
        return "fast"

    def _deep_reason(self, failure: Failure) -> str | None:
        """Return a human-readable reason to route deep, or None to route fast."""
        files_changed = len(failure.diff_summary.files_changed)
        if files_changed >= self._files_threshold:
            return (
                f"{files_changed} files changed "
                f"(threshold: {self._files_threshold})"
            )

        diff_lines = (
            failure.diff_summary.lines_added + failure.diff_summary.lines_removed
        )
        if diff_lines >= self._diff_lines_threshold:
            return (
                f"{diff_lines} diff lines "
                f"(threshold: {self._diff_lines_threshold})"
            )

        log_lines = len(failure.failure.log_tail)
        if log_lines >= self._log_lines_threshold:
            return (
                f"{log_lines} log lines "
                f"(threshold: {self._log_lines_threshold})"
            )

        return None

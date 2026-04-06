"""Structured audit trail for ops-pilot tool calls.

Writes one JSON record per tool call to audit/YYYY-MM-DD.jsonl.
The file rolls over at UTC midnight. Writes are done via read-append-atomic-write
so the JSONL file is never partially written.

Write failures log a warning and return silently — a broken audit trail
never stops an investigation.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Atomic write helper ────────────────────────────────────────────────────────

def _atomic_write(path: Path, content: str) -> None:
    """Write content to path via an atomic rename.

    Uses tempfile.mkstemp in the same directory as the target so the rename
    is always within a single filesystem (POSIX rename guarantee).

    Args:
        path:    Target file path. Parent directory must already exist.
        content: Text content to write.
    """
    tmp_fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, prefix=".tmp_audit_")
    tmp_path = Path(tmp_path_str)
    try:
        with open(tmp_fd, "w") as f:
            f.write(content)
        tmp_path.rename(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


class AuditLog:
    """Append-only per-day JSONL audit log for tool calls.

    Args:
        base_dir: Directory for audit files. Created on first write.
                  Defaults to ./audit.
    """

    def __init__(self, base_dir: Path | str = Path("audit")) -> None:
        self._base = Path(base_dir)

    def record(
        self,
        *,
        tenant_id: str,
        actor: str,
        tool_name: str,
        tool_input: dict[str, object],
        tool_result: str,
        is_error: bool,
        explanation: str | None,
    ) -> None:
        """Append one audit record to today's JSONL file.

        Args:
            tenant_id:   Deployment identifier — from TenantContext or "".
            actor:       Agent name that made the tool call, e.g. "TriageAgent".
            tool_name:   Tool name as registered in the tool registry.
            tool_input:  Raw input dict the model sent.
            tool_result: String result from the tool (or error message).
            is_error:    True if the tool returned an error result.
            explanation: Plain-English pre-action explanation for
                         REQUIRES_CONFIRMATION tools. None for other tools.
        """
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "tenant_id": tenant_id,
            "actor": actor,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_result": tool_result,
            "is_error": is_error,
            "explanation": explanation,
        }
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now(UTC).strftime("%Y-%m-%d")
            target = self._base / f"{date_str}.jsonl"

            # Read existing content, append new line, write atomically.
            existing = target.read_text() if target.exists() else ""
            new_content = existing + json.dumps(entry) + "\n"

            _atomic_write(target, new_content)

        except Exception as exc:
            logger.warning("AuditLog: write failed — %s", exc)

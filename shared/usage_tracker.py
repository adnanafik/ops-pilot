"""Per-deployment daily usage counters stored as file-based JSON.

Storage layout::

    usage/
        YYYY-MM-DD.json   ← one file per UTC calendar day

Each file contains three counters: tokens_consumed, api_calls,
incidents_resolved. Writes are atomic (POSIX rename). Reads are
best-effort — corrupted files reset to zero rather than crashing.

This is the file-based implementation. The interface (record_tokens,
record_api_call, record_incident) is stable so a future DB-backed
implementation can replace this class without touching callers.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class DailyUsage(BaseModel):
    """Counters for a single UTC calendar day."""

    tokens_consumed: int = 0
    api_calls: int = 0
    incidents_resolved: int = 0


class UsageTracker:
    """File-based daily usage counter for one deployment.

    Args:
        base_dir: Directory for usage files. Created on first write.
                  Defaults to ./usage.
    """

    def __init__(self, base_dir: Path | str = Path("usage")) -> None:
        self._base = Path(base_dir)

    def record_tokens(self, n: int) -> None:
        """Increment today's token counter by n.

        Silently logs a warning on write failure — a broken meter
        should never stop an investigation.

        Args:
            n: Number of tokens to add.
        """
        usage = self._load_today()
        usage.tokens_consumed += n
        self._save_today(usage)

    def record_api_call(self) -> None:
        """Increment today's API call counter by 1."""
        usage = self._load_today()
        usage.api_calls += 1
        self._save_today(usage)

    def record_incident(self) -> None:
        """Increment today's resolved incident counter by 1."""
        usage = self._load_today()
        usage.incidents_resolved += 1
        self._save_today(usage)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _today_path(self) -> Path:
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        return self._base / f"{date_str}.json"

    def _load_today(self) -> DailyUsage:
        path = self._today_path()
        if not path.exists():
            return DailyUsage()
        try:
            return DailyUsage.model_validate_json(path.read_text())
        except Exception:
            logger.warning("UsageTracker: corrupted usage file %s — resetting to zero", path)
            return DailyUsage()

    def _save_today(self, usage: DailyUsage) -> None:
        path = self._today_path()
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, usage.model_dump_json())
        except Exception:
            logger.warning("UsageTracker: failed to write usage file %s", path)


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path via atomic rename (same pattern as MemoryStore)."""
    tmp_fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    tmp_path = Path(tmp_path_str)
    try:
        with open(tmp_fd, "w") as f:
            f.write(content)
        tmp_path.rename(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

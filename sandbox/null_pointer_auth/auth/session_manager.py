"""Session management for OAuth refresh token flow.

This module manages user sessions backed by an in-process store
(simulating Redis) for the purposes of the ops-pilot sandbox demo.
"""

from __future__ import annotations

from typing import Optional

# Simulates a Redis store — reset between test runs via clear_store()
_session_store: dict[str, str] = {}


def clear_store() -> None:
    """Reset the session store (test helper)."""
    _session_store.clear()


def seed_store(user_id: str, token: str) -> None:
    """Pre-populate the store with a session token (test helper)."""
    _session_store[f"session:{user_id}"] = token


class SessionManager:
    """Manages a single user's session token.

    Load via the class method SessionManager.load() — do not construct
    directly outside of tests.
    """

    def __init__(self, token: str, user_id: str) -> None:
        self.current_token = token
        self.user_id = user_id

    @classmethod
    def load(cls, user_id: str) -> Optional["SessionManager"]:
        """Load a session from the store.

        BUG (introduced in this commit): the null-guard was removed.
        Previously this raised ValueError when the session was missing,
        which callers handled. Now it silently returns None, causing
        AttributeError in any caller that invokes methods on the result.

        Fix: restore the guard:
            if raw is None:
                raise ValueError(f"Session not found for user {user_id}")
        """
        raw = _session_store.get(f"session:{user_id}")
        # REMOVED: if raw is None: raise ValueError(f"Session not found for user {user_id}")
        if raw is None:
            return None  # silent None — callers are not expecting this
        return cls(token=raw, user_id=user_id)

    def rotate_token(self, token: str) -> str:
        """Rotate the session token and persist it."""
        new_token = f"rotated_{token}_{self.user_id}"
        _session_store[f"session:{self.user_id}"] = new_token
        self.current_token = new_token
        return new_token

    def invalidate(self) -> None:
        """Remove this session from the store."""
        _session_store.pop(f"session:{self.user_id}", None)
        self.current_token = ""

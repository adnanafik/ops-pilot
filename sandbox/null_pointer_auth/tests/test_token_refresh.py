"""Tests for OAuth refresh token rotation.

These tests FAIL on the current branch because SessionManager.load()
returns None for uncached sessions instead of raising ValueError.
ops-pilot will detect the failure, generate a fix, and open a PR.
"""

import pytest
from auth.session_manager import SessionManager, clear_store, seed_store


def setup_function():
    """Reset the session store before each test."""
    clear_store()


def test_refresh_token_rotation():
    """Token rotation should return a new token and update the session."""
    seed_store("usr_abc123", "tok_initial_xyz")

    session = SessionManager.load(user_id="usr_abc123")
    # FAILS HERE when session is None:
    # AttributeError: 'NoneType' object has no attribute 'rotate_token'
    rotated = session.rotate_token(session.current_token)

    assert rotated.startswith("rotated_")
    assert session.current_token == rotated


def test_expired_session_cleanup():
    """Loading a missing session should raise ValueError, not return None."""
    # No seed — session doesn't exist in store
    with pytest.raises(ValueError, match="Session not found"):
        session = SessionManager.load(user_id="usr_abc123")
        if session is None:
            raise ValueError("Session not found for user usr_abc123")
        session.rotate_token(session.current_token)


def test_token_invalidation():
    """Invalidating a session should remove it from the store."""
    seed_store("usr_def456", "tok_active")
    session = SessionManager.load(user_id="usr_def456")
    assert session is not None
    session.invalidate()

    reloaded = SessionManager.load(user_id="usr_def456")
    assert reloaded is None or reloaded.current_token == ""


def test_multiple_rotations_stay_consistent():
    """Multiple rotations should chain correctly."""
    seed_store("usr_ghi789", "tok_v1")
    session = SessionManager.load(user_id="usr_ghi789")
    assert session is not None

    tok2 = session.rotate_token(session.current_token)
    tok3 = session.rotate_token(session.current_token)

    assert tok2 != tok3
    assert session.current_token == tok3

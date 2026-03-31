"""Tests for StateStore."""

from __future__ import annotations

import pytest

from shared.state_store import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(path=str(tmp_path / "state.json"))


class TestSetAndGet:
    def test_set_and_get(self, store):
        store.set("failure_1", "triage", {"severity": "high"})
        result = store.get("failure_1", "triage")
        assert result == {"severity": "high"}

    def test_get_returns_none_for_missing_key(self, store):
        assert store.get("unknown", "triage") is None

    def test_set_overwrites_existing(self, store):
        store.set("f1", "triage", {"severity": "low"})
        store.set("f1", "triage", {"severity": "high"})
        assert store.get("f1", "triage") == {"severity": "high"}

    def test_different_namespaces_are_independent(self, store):
        store.set("f1", "triage", {"x": 1})
        store.set("f1", "fix", {"y": 2})
        assert store.get("f1", "triage") == {"x": 1}
        assert store.get("f1", "fix") == {"y": 2}


class TestGetAll:
    def test_get_all_returns_all_namespaces(self, store):
        store.set("f1", "triage", {"a": 1})
        store.set("f1", "fix", {"b": 2})
        all_data = store.get_all("f1")
        assert set(all_data.keys()) == {"triage", "fix"}

    def test_get_all_empty_for_unknown_failure(self, store):
        assert store.get_all("unknown") == {}


class TestDelete:
    def test_delete_removes_key(self, store):
        store.set("f1", "triage", {"x": 1})
        store.delete("f1", "triage")
        assert store.get("f1", "triage") is None

    def test_delete_nonexistent_key_is_ok(self, store):
        store.delete("nonexistent", "triage")  # should not raise


class TestClearFailure:
    def test_clear_removes_all_namespaces(self, store):
        store.set("f1", "triage", {"a": 1})
        store.set("f1", "fix", {"b": 2})
        store.set("f2", "triage", {"c": 3})
        store.clear_failure("f1")
        assert store.get("f1", "triage") is None
        assert store.get("f1", "fix") is None
        assert store.get("f2", "triage") == {"c": 3}


class TestPersistence:
    def test_data_persists_across_instances(self, tmp_path):
        path = str(tmp_path / "state.json")
        store1 = StateStore(path=path)
        store1.set("f1", "triage", {"severity": "high"})

        store2 = StateStore(path=path)
        assert store2.get("f1", "triage") == {"severity": "high"}

"""Integration tests for Stripe webhook processing.

test_stripe_webhook_idempotency FAILS on this branch because the
idempotency store is now module-level (simulating Postgres persistence)
and is not reset between the two POST calls.

ops-pilot fix: add a teardown that calls clear_idempotency_store()
between the two calls, or use a fresh event_id per invocation.
"""

import pytest
from payments.webhook_handler import handle_stripe_webhook, clear_idempotency_store

STRIPE_PAYLOAD = {
    "id": "stripe_evt_3Pq2Xv",
    "type": "charge.succeeded",
    "data": {"object": {"amount": 2000, "currency": "usd"}},
}


def setup_function():
    """Called before each test function — does NOT reset between calls within a test."""
    # BUG: this resets before the test but not between the two POST calls inside
    # test_stripe_webhook_idempotency. The fix adds clear_idempotency_store() there.
    clear_idempotency_store()


def test_stripe_webhook_first_delivery():
    """First delivery of a webhook event should return 200."""
    result = handle_stripe_webhook(STRIPE_PAYLOAD)
    assert result.status_code == 200
    assert result.body["event_id"] == "stripe_evt_3Pq2Xv"


def test_stripe_webhook_idempotency():
    """Sending the same event twice should return 200 both times (idempotent)."""
    result1 = handle_stripe_webhook(STRIPE_PAYLOAD)
    # BUG: missing clear_idempotency_store() here
    # The key from result1 is still in the store when result2 runs
    result2 = handle_stripe_webhook(STRIPE_PAYLOAD)

    assert result1.status_code == 200
    assert result2.status_code == 200  # FAILS — gets 409 instead


def test_different_events_both_processed():
    """Two different event IDs should both return 200."""
    payload_a = {**STRIPE_PAYLOAD, "id": "stripe_evt_aaa"}
    payload_b = {**STRIPE_PAYLOAD, "id": "stripe_evt_bbb"}

    result_a = handle_stripe_webhook(payload_a)
    result_b = handle_stripe_webhook(payload_b)

    assert result_a.status_code == 200
    assert result_b.status_code == 200


def test_missing_event_id_returns_400():
    """Payload without an event ID should return 400."""
    result = handle_stripe_webhook({"type": "charge.succeeded"})
    assert result.status_code == 400

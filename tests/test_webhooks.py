"""Tests for the Razorpay webhook receiver — api/webhooks.py.

Two things are being protected here, and only one of them is the signature.

The first is authentication: an unsigned or wrongly-signed delivery must not be
processed, and a missing secret must not degrade into accepting everything.

The second is the design that matters more. The webhook body contains an
amount, and the endpoint must never write it. Reconciliation stays in
`execution.actions.reconcile_payment`, which re-reads Razorpay's own API — so
a forged delivery that somehow passed the signature check could at worst cause
a redundant read, never a credited rupee. The tests below assert that the
payload's amount reaches nothing, by handing the handler a body claiming a
large payment and checking that what gets credited comes from the API stub.
"""

import hashlib
import hmac
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.webhooks import (  # noqa: E402
    SETTLEMENT_EVENTS,
    InvalidSignature,
    WebhookNotConfigured,
    _payment_link_from,
    handle_razorpay_webhook,
    verify_signature,
)

SECRET = "whsec_test_not_a_real_secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def link_paid_event(invoice_id="INV-1001", link_id="plink_test_1", amount_paid=99_00_00_000):
    """A `payment_link.paid` delivery. `amount_paid` is deliberately absurd.

    Nothing may read it — if a test ever sees this figure credited, the
    endpoint has become a second writer of money.
    """
    return {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": link_id,
                    "reference_id": invoice_id,
                    "status": "paid",
                    "amount": amount_paid,
                    "amount_paid": amount_paid,
                }
            }
        },
    }


def body_of(event) -> bytes:
    return json.dumps(event).encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────
# Signature verification
# ─────────────────────────────────────────────────────────────────────────

def test_a_correctly_signed_body_verifies():
    body = b'{"event":"payment_link.paid"}'
    verify_signature(body, sign(body), secret=SECRET)  # does not raise


def test_a_tampered_body_does_not_verify():
    body = b'{"event":"payment_link.paid"}'
    signature = sign(body)
    with pytest.raises(InvalidSignature):
        verify_signature(body + b" ", signature, secret=SECRET)


def test_a_signature_from_a_different_secret_does_not_verify():
    body = b'{"event":"payment_link.paid"}'
    with pytest.raises(InvalidSignature):
        verify_signature(body, sign(body, "some_other_secret"), secret=SECRET)


def test_a_missing_signature_header_does_not_verify():
    with pytest.raises(InvalidSignature, match="missing"):
        verify_signature(b"{}", "", secret=SECRET)


def test_an_unconfigured_secret_refuses_rather_than_skipping_the_check():
    """The failure mode that matters: convenience becoming an open endpoint."""
    body = b"{}"
    with pytest.raises(WebhookNotConfigured):
        verify_signature(body, sign(body), secret="")


def test_the_env_placeholder_counts_as_unconfigured():
    """A copied .env.example must not authenticate anything."""
    with pytest.raises(WebhookNotConfigured):
        verify_signature(b"{}", "abc", secret="your_razorpay_webhook_secret_here")


def test_signature_is_computed_over_the_exact_bytes_received():
    """Re-serialising parsed JSON changes the digest for the same delivery."""
    event = link_paid_event()
    raw = json.dumps(event, indent=2).encode("utf-8")  # a formatting Razorpay might send
    verify_signature(raw, sign(raw), secret=SECRET)

    compact = json.dumps(event, separators=(",", ":")).encode("utf-8")
    with pytest.raises(InvalidSignature):
        verify_signature(compact, sign(raw), secret=SECRET)


# ─────────────────────────────────────────────────────────────────────────
# Payload parsing
# ─────────────────────────────────────────────────────────────────────────

def test_a_payment_link_event_resolves_to_its_link_and_invoice():
    link_id, invoice_id = _payment_link_from(link_paid_event("INV-77", "plink_77"))
    assert link_id == "plink_77"
    assert invoice_id == "INV-77"


def test_a_payment_captured_event_resolves_through_the_payment_entity():
    event = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": "pay_1", "payment_link_id": "plink_9", "reference_id": "INV-9",
        }}},
    }
    assert _payment_link_from(event) == ("plink_9", "INV-9")


def test_a_payment_captured_event_falls_back_to_the_invoice_id_note():
    event = {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": "pay_1", "payment_link_id": "plink_9", "notes": {"invoice_id": "INV-9"},
        }}},
    }
    assert _payment_link_from(event) == ("plink_9", "INV-9")


def test_an_event_with_no_recognisable_entity_resolves_to_nothing():
    assert _payment_link_from({"event": "payment_link.paid", "payload": {}}) == (None, None)
    assert _payment_link_from({}) == (None, None)


def test_both_settlement_events_are_handled():
    assert SETTLEMENT_EVENTS == {"payment_link.paid", "payment.captured"}


# ─────────────────────────────────────────────────────────────────────────
# Handler behaviour — no database, dependencies stubbed
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def configured_secret(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", SECRET)


def test_an_unsigned_delivery_is_rejected_before_anything_is_read(
    configured_secret, monkeypatch
):
    """Nothing is parsed, looked up, or reconciled on a bad signature."""
    import api.webhooks as webhooks

    def explode(*a, **k):
        pytest.fail("a delivery with a bad signature reached the database")

    monkeypatch.setattr(webhooks, "get_recovery", explode)
    monkeypatch.setattr(webhooks, "log_audit_event", explode)

    with pytest.raises(InvalidSignature):
        handle_razorpay_webhook(body_of(link_paid_event()), "not-the-signature")


def test_a_signed_body_that_is_not_json_is_rejected(configured_secret):
    body = b"this is not json"
    with pytest.raises(InvalidSignature, match="not valid JSON"):
        handle_razorpay_webhook(body, sign(body))


def test_an_unrelated_event_is_ignored_rather_than_failing(configured_secret, monkeypatch):
    """Subscribing to extra events in the Razorpay dashboard is not an error."""
    import api.webhooks as webhooks

    monkeypatch.setattr(webhooks, "get_recovery", lambda _id: pytest.fail("looked up"))

    body = body_of({"event": "payment_link.expired", "payload": {}})
    result = handle_razorpay_webhook(body, sign(body))
    assert result["status"] == "ignored"
    assert result["reason"] == "not_a_settlement_event"


def test_a_settlement_event_with_no_link_is_ignored(configured_secret, monkeypatch):
    import api.webhooks as webhooks

    monkeypatch.setattr(webhooks, "get_recovery", lambda _id: pytest.fail("looked up"))

    body = body_of({"event": "payment_link.paid", "payload": {}})
    result = handle_razorpay_webhook(body, sign(body))
    assert result["reason"] == "no_payment_link_or_reference_id_in_payload"


def test_a_link_this_system_never_raised_is_ignored(configured_secret, monkeypatch):
    """Anyone can send us a signed-looking event about someone else's invoice."""
    import api.webhooks as webhooks

    monkeypatch.setattr(webhooks, "get_recovery", lambda _id: None)
    monkeypatch.setattr(webhooks, "log_audit_event",
                        lambda *a, **k: pytest.fail("wrote a trail for an unknown invoice"))

    body = body_of(link_paid_event("INV-NOT-OURS"))
    result = handle_razorpay_webhook(body, sign(body))
    assert result["status"] == "ignored"
    assert result["reason"] == "unknown_invoice"


def test_an_already_verified_invoice_is_not_reconciled_twice(configured_secret, monkeypatch):
    """Razorpay retries until it gets a 2xx, and fires two event types per settlement."""
    import api.webhooks as webhooks

    monkeypatch.setattr(webhooks, "get_recovery",
                        lambda _id: {"status": "payment_verified", "amount_recovered_paise": 500})
    events = []
    monkeypatch.setattr(webhooks, "log_audit_event",
                        lambda inv, ev, payload: events.append((ev, payload)))

    monkeypatch.setattr(webhooks, "reconcile_payment",
                        lambda *a, **k: pytest.fail("reconciled an already-verified invoice"))

    body = body_of(link_paid_event())
    result = handle_razorpay_webhook(body, sign(body), rzp_client=object())

    assert result["status"] == "already_verified"
    assert events[0][0] == "webhook_received"
    assert events[0][1]["execution"]["status"] == "already_verified"


def test_the_webhook_triggers_reconciliation_rather_than_crediting_the_payload(
    configured_secret, monkeypatch
):
    """The design test. The body claims ₹99,00,000; the API stub reports ₹5,000.

    What comes back must be the API's figure, and `reconcile_payment` must have
    been called with the invoice and link — not handed an amount.
    """
    import api.webhooks as webhooks

    monkeypatch.setattr(webhooks, "get_recovery", lambda _id: {"status": "link_created"})
    monkeypatch.setattr(webhooks, "log_audit_event", lambda *a, **k: None)

    calls = []

    def fake_reconcile(rzp_client, invoice_id, payment_link_id):
        calls.append((invoice_id, payment_link_id))
        return {"status": "payment_verified", "amount_recovered_paise": 5_00_000}

    monkeypatch.setattr(webhooks, "reconcile_payment", fake_reconcile)

    body = body_of(link_paid_event("INV-1001", "plink_test_1", amount_paid=99_00_00_000))
    result = handle_razorpay_webhook(body, sign(body), rzp_client=object())

    assert calls == [("INV-1001", "plink_test_1")]
    assert result["reconciliation"]["amount_recovered_paise"] == 5_00_000, (
        "the credited amount did not come from the Razorpay API"
    )
    assert 99_00_00_000 not in result["reconciliation"].values()


def test_the_trail_says_the_amount_was_not_taken_from_the_delivery(
    configured_secret, monkeypatch
):
    """A reader of the audit trail should not have to guess where the figure came from."""
    import api.webhooks as webhooks

    monkeypatch.setattr(webhooks, "get_recovery", lambda _id: {"status": "link_created"})
    events = []
    monkeypatch.setattr(webhooks, "log_audit_event",
                        lambda inv, ev, payload: events.append((ev, payload)))
    monkeypatch.setattr(webhooks, "reconcile_payment",
                        lambda *a, **k: {"status": "payment_verified"})

    body = body_of(link_paid_event())
    handle_razorpay_webhook(body, sign(body), rzp_client=object())

    execution = events[0][1]["execution"]
    assert execution["source"] == "razorpay_webhook"
    assert "not from the webhook body" in execution["amount_source"]


def test_without_a_razorpay_client_the_webhook_refuses_rather_than_crediting(
    configured_secret, monkeypatch
):
    """No way to re-verify means no credit. Never fall back to the payload."""
    import api.webhooks as webhooks

    monkeypatch.setattr(webhooks, "get_recovery", lambda _id: {"status": "link_created"})
    monkeypatch.setattr(webhooks, "log_audit_event", lambda *a, **k: None)

    body = body_of(link_paid_event())
    result = handle_razorpay_webhook(body, sign(body), rzp_client=None)

    assert result["status"] == "cannot_verify"
    assert result["reason"] == "razorpay_credentials_not_configured"


def test_reconcile_payment_remains_the_only_writer_of_money():
    """A grep-level guard: this module must never touch the money column itself."""
    import inspect

    import api.webhooks as webhooks

    source = inspect.getsource(webhooks)
    for writer in ("update_recovery", "insert_recovery", "UPDATE recoveries"):
        assert writer not in source, (
            f"api/webhooks.py contains {writer!r} — reconcile_payment must stay the "
            "only writer of amount_recovered_paise"
        )

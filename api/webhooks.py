"""Razorpay webhook receipt — the push half of reconciliation.

§ Limitations used to say reconciliation was polling-only and that production
would use Razorpay's webhook instead. This is that webhook.

**The webhook is a trigger, not a source of truth.** It carries an amount, and
that amount is deliberately ignored. What it does is tell us *when* to look;
the looking is still `execution.actions.reconcile_payment`, which calls the
Razorpay API, re-checks every identity and amount invariant, and remains the
only place in this codebase that writes a non-zero `amount_recovered_paise`.

That is the whole design. A webhook body is an unauthenticated HTTP POST from
the open internet until its signature is checked, and even after the signature
checks out it is a claim about money made by a payload. Crediting an invoice
from it would create a second writer of money whose input is attacker-shaped —
so it creates no path to the money column at all, and a forged or replayed
delivery can at worst cause a redundant read of Razorpay's own API.

Signature: HMAC-SHA256 over the exact raw request body, keyed with
RAZORPAY_WEBHOOK_SECRET, compared with `hmac.compare_digest`. The raw bytes
matter — re-serialising the parsed JSON would produce a different digest for
the same delivery.
"""

import hashlib
import hmac
import json
import logging
import os

from audit.trail import get_recovery, log_audit_event
from execution.actions import reconcile_payment

log = logging.getLogger(__name__)

# The events that mean "a payment link we raised has been settled". Razorpay
# fires `payment_link.paid` for links and `payment.captured` for the payment
# behind it; both can arrive for one settlement, and handling both is safe
# because an invoice already marked `payment_verified` short-circuits below.
SETTLEMENT_EVENTS = frozenset({"payment_link.paid", "payment.captured"})


class WebhookNotConfigured(Exception):
    """RAZORPAY_WEBHOOK_SECRET is unset, so no delivery can be authenticated."""


class InvalidSignature(Exception):
    """The body did not match the signature header."""


def verify_signature(raw_body: bytes, signature: str, secret: str = None) -> None:
    """Raises unless `signature` is the HMAC-SHA256 of `raw_body` under the secret.

    Refuses outright when no secret is configured rather than skipping the
    check. An endpoint that accepts unsigned deliveries in the name of local
    convenience is an endpoint that accepts them in production.
    """
    secret = secret if secret is not None else os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret or secret == "your_razorpay_webhook_secret_here":
        raise WebhookNotConfigured(
            "RAZORPAY_WEBHOOK_SECRET is not configured — refusing to accept an "
            "unauthenticated webhook delivery"
        )
    if not signature:
        raise InvalidSignature("missing X-Razorpay-Signature header")

    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    # compare_digest, not ==: a short-circuiting comparison leaks the position
    # of the first wrong byte to anyone who can time the response.
    if not hmac.compare_digest(expected, signature):
        raise InvalidSignature("signature does not match the request body")


def _payment_link_from(event: dict) -> tuple:
    """Extracts (payment_link_id, invoice_id) from a settlement event, or (None, None).

    Razorpay nests entities under `payload.<entity>.entity`. The invoice id is
    the `reference_id` we set at link creation — the same field
    `reconcile_payment` re-checks — so an event about somebody else's link
    resolves to no invoice of ours and is ignored.
    """
    payload = event.get("payload") or {}

    link = ((payload.get("payment_link") or {}).get("entity")) or {}
    if link:
        return link.get("id"), link.get("reference_id")

    # `payment.captured` carries the payment, which references the link.
    payment = ((payload.get("payment") or {}).get("entity")) or {}
    if payment:
        notes = payment.get("notes") or {}
        return (
            payment.get("payment_link_id"),
            payment.get("reference_id") or notes.get("invoice_id"),
        )

    return None, None


def handle_razorpay_webhook(raw_body: bytes, signature: str, rzp_client=None) -> dict:
    """Verifies a delivery and, if it settles one of our links, reconciles it.

    Returns a small status dict; every branch is also written to the audit
    trail of the invoice it concerns, so a webhook that decided to do nothing
    is as visible as one that credited a payment.
    """
    verify_signature(raw_body, signature)

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InvalidSignature(f"signed body is not valid JSON: {e}")

    event_name = event.get("event")
    if event_name not in SETTLEMENT_EVENTS:
        # Subscribing to extra events in the Razorpay dashboard should not be
        # an error — it should be a no-op.
        log.info("ignoring webhook event %r", event_name)
        return {"status": "ignored", "event": event_name, "reason": "not_a_settlement_event"}

    payment_link_id, invoice_id = _payment_link_from(event)
    if not payment_link_id or not invoice_id:
        log.warning("webhook %r carried no resolvable payment link", event_name)
        return {
            "status": "ignored",
            "event": event_name,
            "reason": "no_payment_link_or_reference_id_in_payload",
        }

    recovery = get_recovery(invoice_id)
    if recovery is None:
        # A link this system never raised. Recorded nowhere, because there is
        # no invoice row to hang an audit event off.
        log.warning("webhook for unknown invoice %s — ignored", invoice_id)
        return {"status": "ignored", "event": event_name, "reason": "unknown_invoice"}

    # Razorpay retries a delivery until it gets a 2xx, and fires more than one
    # event type per settlement, so the same payment arrives here repeatedly.
    # Short-circuiting keeps the trail free of duplicate `payment_verified`
    # events without needing a delivery-id store.
    if recovery.get("status") == "payment_verified":
        log_audit_event(invoice_id, "webhook_received", {
            "execution": {
                "status": "already_verified",
                "event": event_name,
                "payment_link_id": payment_link_id,
                "source": "razorpay_webhook",
            },
        })
        return {"status": "already_verified", "invoice_id": invoice_id, "event": event_name}

    log_audit_event(invoice_id, "webhook_received", {
        "execution": {
            "status": "reconciling",
            "event": event_name,
            "payment_link_id": payment_link_id,
            "source": "razorpay_webhook",
            # Stated in the trail so a reader knows the amount below was not
            # taken from the delivery.
            "amount_source": "re-read from the Razorpay API, not from the webhook body",
        },
    })

    if rzp_client is None:
        # Nothing to re-verify against. Refusing beats crediting on the
        # payload, which is the one thing this endpoint must never do.
        log.error("[%s] webhook received with no Razorpay client configured", invoice_id)
        return {
            "status": "cannot_verify",
            "invoice_id": invoice_id,
            "reason": "razorpay_credentials_not_configured",
        }

    # The only writer of money in this codebase. Called, never bypassed, and
    # never handed an amount — it reads Razorpay's own figure for itself.
    result = reconcile_payment(rzp_client, invoice_id, payment_link_id)
    return {
        "status": result.get("status"),
        "invoice_id": invoice_id,
        "event": event_name,
        "reconciliation": result,
    }

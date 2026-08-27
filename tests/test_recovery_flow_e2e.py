"""The full recovery flow, end to end, against the real database.

diagnose (mocked) -> policy (real) -> Orchestrator -> PaymentLinkWorker
(real) -> create_payment_link_with_retry against a fake Razorpay client ->
insert_recovery (real DB write) -> an inline reconciliation attempt that
correctly finds the link not yet paid -> a signed payment_link.paid webhook
delivery, handled by the real api.webhooks.handle_razorpay_webhook -> the
real execution.actions.reconcile_payment -> a real update_recovery DB write
-> read back through the real get_recovery.

Every other test that touches this flow tests one stage of it in isolation:
tests/test_pipeline_e2e.py always passes rzp_client=None, so PaymentLinkWorker
takes its "simulated" branch and never calls create_payment_link_with_retry
or reconcile_payment at all. tests/test_webhooks.py calls
handle_razorpay_webhook directly against a hand-built get_recovery stub and a
monkeypatched reconcile_payment, so it never sees a real recoveries row or a
real credited amount. tests/test_reconciliation.py drives reconcile_payment
directly, off the pipeline entirely. None of them prove the pieces actually
fit together — that a link process_invoice creates is the same row a webhook
later reconciles, correctly, through the real database. This file is that
proof. It is not a replacement for any of them: those pin exact guard
behaviour this file does not re-assert.

The one thing this cannot cover without a live Razorpay account, and the
reason a human still needs to run demo/demo_recovery.py before a real
submission: the fake client's `verify_payment` reports "paid" because the
test told it to, not because a browser actually completed a test-mode
checkout against Razorpay's hosted page. See the module docstring's Manual
Steps note below for the full list of what this test does not exercise.

Manual steps this test cannot replace (for whoever records the demo video):
  1. Razorpay's hosted Checkout page actually rendering and accepting a card.
     This test's fake client skips the browser entirely.
  2. The webhook actually arriving from Razorpay's servers over the public
     internet to a real endpoint. README § Limitations already says this
     path "has not received a live delivery" — this test exercises the
     handler against a delivery this test constructs, not one Razorpay sent.
  3. Real NVIDIA NIM latency/rate-limiting during the demo's diagnosis step —
     diagnosis is mocked here for the same reason every other e2e test mocks
     it: a live model call would make this test measure the model's mood.
"""

import hashlib
import hmac
import json

import pytest

import api.pipeline as pipeline
from api.webhooks import handle_razorpay_webhook
from audit.trail import get_audit_trail, get_recovery
from tests.conftest import unique_invoice_id

pytestmark = pytest.mark.db

WEBHOOK_SECRET = "whsec_test_full_flow_not_a_real_secret"


class FakeRazorpayLink:
    """A payment link with a status a test can flip, the way a real one does.

    Starts `issued` (created, unpaid) — a link is essentially never already
    paid the instant it is created, which is why the pipeline's own inline
    reconciliation normally records "pending". `mark_paid()` is what a real
    customer completing Checkout does to a real link; calling it is this
    test's stand-in for that.
    """

    def __init__(self, link_id: str, invoice_id: str, amount_paise: int):
        self.id = link_id
        self.invoice_id = invoice_id
        self.amount_paise = amount_paise
        self.status = "issued"
        self.amount_paid = 0

    def mark_paid(self):
        self.status = "paid"
        self.amount_paid = self.amount_paise

    def create_payment_link_response(self) -> dict:
        return {"id": self.id, "short_url": f"https://rzp.io/i/{self.id}"}

    def verify_payment_response(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "reference_id": self.invoice_id,
            "amount": self.amount_paise,
            "amount_paid": self.amount_paid,
        }


class FakeRazorpayClient:
    """Enough of RazorpayClient's interface for one invoice's link lifecycle.

    Real network calls never happen — this is exactly the "mocked webhook
    payload / test-mode fixture, not real API calls" the fix asked for.
    """

    def __init__(self):
        self.links = {}

    def create_payment_link(self, invoice_id, amount_paise, client_name, description):
        link = FakeRazorpayLink(f"plink_{invoice_id}", invoice_id, amount_paise)
        self.links[link.id] = link
        return link.create_payment_link_response()

    def verify_payment(self, payment_link_id):
        return self.links[payment_link_id].verify_payment_response()


def _sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _payment_link_paid_delivery(link_id: str, invoice_id: str) -> bytes:
    """A payment_link.paid event body. Deliberately carries no amount that
    matters — api/webhooks.py must re-read it from the Razorpay API, never
    from here, and that is asserted below by never putting the true amount
    in this payload at all.
    """
    event = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {
                "entity": {
                    "id": link_id,
                    "reference_id": invoice_id,
                }
            }
        },
    }
    return json.dumps(event).encode("utf-8")


@pytest.fixture(autouse=True)
def _needs_database(require_database):
    pass


def test_the_full_recovery_flow_end_to_end(
    mocked_pipeline_llm, db_cleanup, sample_invoice, monkeypatch,
):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)

    mocked_pipeline_llm(
        diagnosed_cause="payment_link_failure",
        evidence_tier="known",
        recommended_action="create_payment_link",
        evidence_cited="Client says the payment link is broken and they are ready to pay.",
    )

    invoice_id = db_cleanup(unique_invoice_id("recovery-flow"))
    invoice = dict(sample_invoice)
    invoice.update(
        invoice_id=invoice_id,
        payment_link_status="expired",
        client_notes="The Razorpay link throws a 404. Please send a fresh one — ready to pay today.",
    )
    outstanding = invoice["amount_paise"] - invoice["partial_payment_received_paise"]

    rzp_client = FakeRazorpayClient()

    # ── Stage 1: diagnose -> policy -> Orchestrator -> PaymentLinkWorker ──
    result = pipeline.process_invoice(invoice, api_key="test-key", rzp_client=rzp_client)

    assert result["policy_decision"] == "ALLOW", (
        f"expected the policy engine to ALLOW a known payment_link_failure "
        f"with no dispute/legal signal; got {result['policy_decision']} "
        f"({result['policy_reasons']})"
    )
    assert result["worker"] == "payment_link"
    assert result["execution"]["status"] == "link_created"

    link_id = result["execution"]["payment_link_id"]
    assert link_id in rzp_client.links, "the worker must call the real create_payment_link"

    # ── Stage 2: the recoveries row exists, and nothing is credited yet ──
    # A link created moments ago is essentially never already paid — this is
    # PaymentLinkWorker's own inline reconciliation attempt, and it must have
    # found the fake link still `issued`.
    recovery = get_recovery(invoice_id)
    assert recovery is not None, "PaymentLinkWorker must insert a recoveries row on success"
    assert recovery["status"] == "link_created"
    assert recovery["amount_recovered_paise"] == 0

    # ── Stage 3: the customer completes checkout (simulated) ──
    rzp_client.links[link_id].mark_paid()

    # ── Stage 4: Razorpay's webhook delivers the settlement ──
    body = _payment_link_paid_delivery(link_id, invoice_id)
    webhook_result = handle_razorpay_webhook(body, _sign(body), rzp_client=rzp_client)

    assert webhook_result["status"] == "payment_verified"
    assert webhook_result["reconciliation"]["amount_recovered_paise"] == outstanding

    # ── Stage 5: the database reflects it, independent of what the webhook returned ──
    recovery_after = get_recovery(invoice_id)
    assert recovery_after["status"] == "payment_verified"
    assert recovery_after["amount_recovered_paise"] == outstanding
    assert recovery_after["verified_at"] is not None

    # ── Stage 6: the audit trail tells the whole story, in order ──
    events = [row["event_type"] for row in get_audit_trail(invoice_id)]
    for expected in (
        "diagnosis", "policy_decision", "payment_link_created",
        "reconciliation_attempted", "webhook_received", "payment_verified",
    ):
        assert expected in events, f"{expected!r} missing from the audit trail: {events}"
    assert events.index("payment_link_created") < events.index("webhook_received"), (
        "the link must exist before a webhook about it can arrive"
    )
    assert events.index("webhook_received") < events.index("payment_verified"), (
        "reconciliation must follow the webhook, not precede it"
    )


def test_a_second_webhook_delivery_for_the_same_settlement_is_a_no_op(
    mocked_pipeline_llm, db_cleanup, sample_invoice, monkeypatch,
):
    """Razorpay retries a delivery until it gets a 2xx, and fires both
    payment_link.paid and payment.captured for one settlement. Money must be
    credited once, not once per delivery.
    """
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    mocked_pipeline_llm(
        diagnosed_cause="payment_link_failure",
        evidence_tier="known",
        recommended_action="create_payment_link",
    )

    invoice_id = db_cleanup(unique_invoice_id("recovery-flow-dup"))
    invoice = dict(sample_invoice)
    invoice.update(
        invoice_id=invoice_id,
        payment_link_status="expired",
        client_notes="Link is broken, please resend — ready to pay.",
    )

    rzp_client = FakeRazorpayClient()
    result = pipeline.process_invoice(invoice, api_key="test-key", rzp_client=rzp_client)
    link_id = result["execution"]["payment_link_id"]
    rzp_client.links[link_id].mark_paid()

    body = _payment_link_paid_delivery(link_id, invoice_id)
    first = handle_razorpay_webhook(body, _sign(body), rzp_client=rzp_client)
    second = handle_razorpay_webhook(body, _sign(body), rzp_client=rzp_client)

    assert first["status"] == "payment_verified"
    assert second["status"] == "already_verified"

    recovery = get_recovery(invoice_id)
    outstanding = invoice["amount_paise"] - invoice["partial_payment_received_paise"]
    assert recovery["amount_recovered_paise"] == outstanding, (
        "a duplicate delivery must not credit the invoice a second time"
    )

    verified_events = [
        row for row in get_audit_trail(invoice_id) if row["event_type"] == "payment_verified"
    ]
    assert len(verified_events) == 1, "payment_verified must be logged exactly once"

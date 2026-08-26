"""Tests for execution.actions.reconcile_payment — the only writer of money.

`reconcile_payment` is the single function in this codebase that can write a
non-zero `amount_recovered_paise`. Everything else — link creation, the
workers, the audit trail — records zero. That makes its five guard conditions
the last line between "Razorpay says this link was paid" and a rupee figure
appearing on the dashboard, and every one of them is asserted here:

    1. Razorpay reports the link status as exactly "paid"
    2. the link Razorpay returned is the one we asked about
    3. the link's reference_id is this invoice
    4. amount_paid is a positive integer number of paise
    5. amount_paid does not exceed the amount the link was raised for

The tests are written so that a guard *removed* fails a test, not merely a
guard misbehaving: each one drives the function with input that satisfies
every condition except the one under test, and asserts both that nothing was
credited and that the specific discrepancy reason was recorded. A test that
only checked the return value would still pass if the function credited money
on its way to returning "discrepancy".

Offline: `verify_payment` is a stub, and the two audit-trail writers are
captured in lists rather than reaching Postgres.
"""

import pytest

import execution.actions as actions

INVOICE_ID = "INV-RECON-001"
LINK_ID = "plink_ABC123"
LINK_AMOUNT = 50_000


class FakeRazorpay:
    """Returns one canned verify_payment payload and records what was asked."""

    def __init__(self, payload):
        self._payload = payload
        self.asked_for = []

    def verify_payment(self, payment_link_id):
        self.asked_for.append(payment_link_id)
        return self._payload


def paid_payload(**overrides):
    """A payload that passes all five guards. Tests override exactly one field."""
    payload = {
        "id": LINK_ID,
        "status": "paid",
        "reference_id": INVOICE_ID,
        "amount": LINK_AMOUNT,
        "amount_paid": LINK_AMOUNT,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def audit(monkeypatch):
    """Captures audit events and any recovery write, instead of touching Postgres.

    `credited` stays empty unless the function actually called update_recovery,
    which is what "no money was credited" has to mean here — a return value of
    "discrepancy" proves nothing on its own.
    """
    events = []
    credited = []

    monkeypatch.setattr(
        actions, "log_audit_event",
        lambda invoice_id, event_type, payload: events.append((event_type, payload)),
    )
    monkeypatch.setattr(
        actions, "update_recovery",
        lambda **kwargs: credited.append(kwargs),
    )
    return {"events": events, "credited": credited}


def event_types(audit):
    return [event for event, _payload in audit["events"]]


# ── The happy path ───────────────────────────────────────────────────────

def test_fully_paid_link_credits_exactly_what_razorpay_reported(audit):
    result = actions.reconcile_payment(
        FakeRazorpay(paid_payload()), INVOICE_ID, LINK_ID
    )

    assert result["status"] == "payment_verified"
    assert result["amount_recovered_paise"] == LINK_AMOUNT
    assert len(audit["credited"]) == 1
    assert audit["credited"][0]["amount_recovered_paise"] == LINK_AMOUNT
    assert audit["credited"][0]["status"] == "payment_verified"
    assert "payment_verified" in event_types(audit)


def test_credited_figure_is_razorpay_amount_paid_not_the_amount_we_asked_for(audit):
    """A part-payment on a `paid` link credits what arrived, never the ask.

    Crediting `amount` rather than `amount_paid` would over-report recovery on
    every link a client underpaid, which is the direction that flatters the
    demo — so it is pinned explicitly.
    """
    result = actions.reconcile_payment(
        FakeRazorpay(paid_payload(amount_paid=30_000)), INVOICE_ID, LINK_ID
    )

    assert result["amount_recovered_paise"] == 30_000
    assert audit["credited"][0]["amount_recovered_paise"] == 30_000


def test_verified_at_is_recorded_on_both_the_result_and_the_write(audit):
    result = actions.reconcile_payment(
        FakeRazorpay(paid_payload()), INVOICE_ID, LINK_ID
    )

    assert result["verified_at"]
    assert audit["credited"][0]["verified_at"] is not None


# ── Guard 1: status must be exactly "paid" ───────────────────────────────

@pytest.mark.parametrize(
    "status", ["created", "expired", "cancelled", "issued", None, "PAID", "Paid"]
)
def test_any_status_other_than_paid_credits_nothing(audit, status):
    """Including the case-variants: the check is exact, and must stay exact."""
    result = actions.reconcile_payment(
        FakeRazorpay(paid_payload(status=status)), INVOICE_ID, LINK_ID
    )

    assert result["status"] == "pending"
    assert audit["credited"] == []


def test_partially_paid_is_reported_as_a_human_decision_not_a_pending_link(audit):
    """Partial settlement of a B2B invoice is a person's call, not the agent's."""
    result = actions.reconcile_payment(
        FakeRazorpay(paid_payload(status="partially_paid")), INVOICE_ID, LINK_ID
    )

    assert result["status"] == "pending"
    assert result["reason"] == "partially_paid_requires_human_review"
    assert audit["credited"] == []


def test_unpaid_link_is_reported_as_not_yet_paid(audit):
    result = actions.reconcile_payment(
        FakeRazorpay(paid_payload(status="created")), INVOICE_ID, LINK_ID
    )

    assert result["reason"] == "link_not_yet_paid"


# ── Guard 2: the returned link must be the one we asked about ────────────

def test_link_id_mismatch_credits_nothing(audit):
    """Never credit this invoice against a different link's payment."""
    result = actions.reconcile_payment(
        FakeRazorpay(paid_payload(id="plink_SOMEONE_ELSE")), INVOICE_ID, LINK_ID
    )

    assert result["status"] == "discrepancy"
    assert result["reason"] == "payment_link_id_mismatch"
    assert audit["credited"] == []
    assert "reconciliation_discrepancy" in event_types(audit)


def test_absent_link_id_does_not_block_reconciliation(audit):
    """The guard is `returned and returned != asked` — absent is not a mismatch."""
    payload = paid_payload()
    del payload["id"]

    result = actions.reconcile_payment(FakeRazorpay(payload), INVOICE_ID, LINK_ID)

    assert result["status"] == "payment_verified"


# ── Guard 3: the reference_id must be this invoice ───────────────────────

def test_reference_id_mismatch_credits_nothing(audit):
    """reference_id is the invoice the link was raised for. A mismatch means
    this payment belongs to a different invoice."""
    result = actions.reconcile_payment(
        FakeRazorpay(paid_payload(reference_id="INV-A-DIFFERENT-ONE")),
        INVOICE_ID, LINK_ID,
    )

    assert result["status"] == "discrepancy"
    assert result["reason"] == "reference_id_mismatch"
    assert audit["credited"] == []


def test_absent_reference_id_does_not_block_reconciliation(audit):
    payload = paid_payload()
    del payload["reference_id"]

    result = actions.reconcile_payment(FakeRazorpay(payload), INVOICE_ID, LINK_ID)

    assert result["status"] == "payment_verified"


# ── Guard 4: amount_paid must be a positive integer of paise ─────────────

@pytest.mark.parametrize(
    "amount_paid",
    [0, -1, None, "50000", 50_000.0, True, False],
    ids=["zero", "negative", "missing", "string", "float", "bool_true", "bool_false"],
)
def test_paid_status_without_a_usable_amount_credits_nothing(audit, amount_paid):
    """`True` is in this list deliberately: bool is a subclass of int in Python,
    so a naive isinstance check would treat it as 1 paise and credit it."""
    result = actions.reconcile_payment(
        FakeRazorpay(paid_payload(amount_paid=amount_paid)), INVOICE_ID, LINK_ID
    )

    assert result["status"] == "discrepancy"
    assert result["reason"] == "paid_status_without_usable_amount_paid"
    assert audit["credited"] == []


# ── Guard 5: amount_paid must not exceed the link amount ─────────────────

def test_overpayment_beyond_the_link_amount_credits_nothing(audit):
    result = actions.reconcile_payment(
        FakeRazorpay(paid_payload(amount=LINK_AMOUNT, amount_paid=LINK_AMOUNT + 1)),
        INVOICE_ID, LINK_ID,
    )

    assert result["status"] == "discrepancy"
    assert result["reason"] == "amount_paid_exceeds_link_amount"
    assert audit["credited"] == []


def test_paying_exactly_the_link_amount_is_not_an_overpayment(audit):
    """Boundary: the guard is `>`, not `>=`."""
    result = actions.reconcile_payment(
        FakeRazorpay(paid_payload(amount=LINK_AMOUNT, amount_paid=LINK_AMOUNT)),
        INVOICE_ID, LINK_ID,
    )

    assert result["status"] == "payment_verified"


def test_non_integer_link_amount_does_not_block_a_valid_payment(audit):
    """The ceiling check only applies when Razorpay gave a usable link amount."""
    payload = paid_payload()
    del payload["amount"]

    result = actions.reconcile_payment(FakeRazorpay(payload), INVOICE_ID, LINK_ID)

    assert result["status"] == "payment_verified"


# ── The function asks about the link it was given ────────────────────────

def test_reconciliation_queries_the_payment_link_it_was_asked_about(audit):
    client = FakeRazorpay(paid_payload())
    actions.reconcile_payment(client, INVOICE_ID, LINK_ID)
    assert client.asked_for == [LINK_ID]

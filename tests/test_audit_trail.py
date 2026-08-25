"""Audit trail database operations, against the real Postgres schema.

These are the tests that would not survive being mocked. The properties under
test — the audit_log foreign key, the JSONB round-trip, the
`recovered_only_when_verified` CHECK constraint — are enforced by the database
itself, so a fake connection would assert nothing about them.

Every test writes under a unique `ZZTEST-` invoice_id and deletes it
afterwards (see tests/conftest.py); the project's real batch data is left
alone. Marked `db` so `-m "not db"` gives a fast, offline run.
"""

import time
from datetime import datetime, timezone

import psycopg
import pytest

from audit.trail import (
    get_audit_trail,
    get_recovery,
    insert_invoice,
    insert_recovery,
    log_audit_event,
    update_recovery,
)
from database.connection import get_connection
from tests.conftest import unique_invoice_id

pytestmark = pytest.mark.db


def fetch_invoice(invoice_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM invoices WHERE invoice_id = %s", (invoice_id,))
            return cur.fetchone()


def count_invoices(invoice_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM invoices WHERE invoice_id = %s", (invoice_id,)
            )
            return cur.fetchone()["n"]


# ─────────────────────────────────────────────────────────────────────────
# invoices
# ─────────────────────────────────────────────────────────────────────────

def test_insert_invoice_creates_record(insert_test_invoice):
    invoice = insert_test_invoice(
        client_name="Amravati Steel Rollers",
        amount_paise=34_21_750,
        partial_payment_received_paise=4_17_500,
        days_overdue=35,
        account_value_tier="standard",
    )
    row = fetch_invoice(invoice["invoice_id"])

    assert row is not None
    assert row["client_name"] == "Amravati Steel Rollers"
    assert row["amount_paise"] == 34_21_750
    assert row["days_overdue"] == 35
    assert row["account_value_tier"] == "standard"


def test_insert_invoice_derives_outstanding_from_amount_minus_partial(insert_test_invoice):
    """The outstanding balance is computed on write, never taken from the caller."""
    invoice = insert_test_invoice(
        amount_paise=10_00_000, partial_payment_received_paise=3_50_000
    )
    assert fetch_invoice(invoice["invoice_id"])["outstanding_balance_paise"] == 6_50_000


def test_insert_invoice_twice_is_idempotent(insert_test_invoice, sample_invoice):
    """Two inserts of the same id produce one row and no exception.

    Replay, not rollback, is this pipeline's crash-recovery model — a batch
    that dies mid-way is re-run, so every write has to be safe to repeat.
    """
    invoice = insert_test_invoice()
    insert_invoice(invoice)
    assert count_invoices(invoice["invoice_id"]) == 1


def test_insert_invoice_on_conflict_refreshes_mutable_financial_fields(insert_test_invoice):
    """A later partial payment must reach the table, not be swallowed by the upsert.

    Documents deliberate behaviour rather than the intuitive one: the ON
    CONFLICT clause is DO UPDATE, not DO NOTHING. An earlier revision used DO
    NOTHING, and the consequence was that the pipeline reasoned over fresh
    figures while the dashboard kept rendering stale ones.
    """
    invoice = insert_test_invoice(amount_paise=10_00_000, partial_payment_received_paise=0)
    assert fetch_invoice(invoice["invoice_id"])["outstanding_balance_paise"] == 10_00_000

    insert_invoice(dict(invoice, partial_payment_received_paise=4_00_000, days_overdue=42))
    row = fetch_invoice(invoice["invoice_id"])
    assert row["outstanding_balance_paise"] == 6_00_000
    assert row["days_overdue"] == 42


def test_insert_invoice_stores_payment_history_as_json(insert_test_invoice):
    history = ["paid_on_time", "paid_late", "paid_on_time"]
    invoice = insert_test_invoice(client_payment_history=history)
    assert fetch_invoice(invoice["invoice_id"])["client_payment_history"] == history


# ─────────────────────────────────────────────────────────────────────────
# audit_log
# ─────────────────────────────────────────────────────────────────────────

def test_log_audit_event_creates_record(insert_test_invoice):
    invoice = insert_test_invoice()
    log_audit_event(invoice["invoice_id"], "policy_decision", {
        "decision": "REQUIRE_HUMAN",
        "reasons": ["dispute_signal_detected_in_client_notes"],
        "allowed_actions": ["escalate_to_human"],
        "blocked_actions": ["send_reminder"],
    })

    trail = get_audit_trail(invoice["invoice_id"])
    assert len(trail) == 1
    event = trail[0]
    assert event["event_type"] == "policy_decision"
    assert event["decision"] == "REQUIRE_HUMAN"
    assert event["reasons"] == ["dispute_signal_detected_in_client_notes"]
    assert event["allowed_actions"] == ["escalate_to_human"]
    assert event["blocked_actions"] == ["send_reminder"]


def test_log_audit_event_without_invoice_fails(db_cleanup):
    """The foreign key is what stops an orphan event describing an invoice nobody has."""
    missing_id = db_cleanup(unique_invoice_id("orphan"))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        log_audit_event(missing_id, "diagnosis", {"diagnosis": {"diagnosed_cause": "unknown"}})


def test_get_audit_trail_returns_events_in_order(insert_test_invoice):
    """The trail is a chronology — out-of-order events would misrepresent the decision path."""
    invoice = insert_test_invoice()
    for event_type in ("reasoning_trace", "diagnosis", "policy_decision"):
        log_audit_event(invoice["invoice_id"], event_type, {})
        time.sleep(0.01)

    trail = get_audit_trail(invoice["invoice_id"])
    assert [e["event_type"] for e in trail] == [
        "reasoning_trace", "diagnosis", "policy_decision"
    ]
    timestamps = [e["created_at"] for e in trail]
    assert timestamps == sorted(timestamps)


def test_get_audit_trail_for_unknown_invoice_returns_empty_list():
    assert get_audit_trail("ZZTEST-does-not-exist") == []


def test_audit_trail_stores_json_fields_correctly(insert_test_invoice):
    """JSONB must round-trip as structured data, not as a string of JSON.

    Double-encoding is the failure mode this guards: a `json.dumps` applied
    twice yields a column that reads back as `'{"a": 1}'` rather than
    `{"a": 1}`, and every consumer downstream then has to guess which it got.
    """
    invoice = insert_test_invoice()
    diagnosis = {
        "diagnosed_cause": "dispute",
        "evidence_tier": "known",
        "confidence": 0.91,
        "suspicious_content_detected": False,
        "nested": {"signals": {"engagement_signal": "client_replied"}, "list": [1, 2, 3]},
    }
    reasons = ["dispute_signal_detected_in_client_notes", "matched_phrase:disputing"]

    log_audit_event(invoice["invoice_id"], "policy_decision", {
        "decision": "REQUIRE_HUMAN",
        "reasons": reasons,
        "diagnosis": diagnosis,
        "execution": {"worker": "escalation", "priority": "HIGH"},
    })

    event = get_audit_trail(invoice["invoice_id"])[0]
    assert isinstance(event["diagnosis"], dict)
    assert isinstance(event["reasons"], list)
    assert event["diagnosis"] == diagnosis
    assert event["diagnosis"]["nested"]["list"] == [1, 2, 3]
    assert event["reasons"] == reasons
    assert event["execution_result"] == {"worker": "escalation", "priority": "HIGH"}


def test_audit_log_is_append_only_across_repeated_events(insert_test_invoice):
    """Re-running a batch adds events; it never overwrites the previous run's."""
    invoice = insert_test_invoice()
    for _ in range(3):
        log_audit_event(invoice["invoice_id"], "diagnosis", {"diagnosis": {"x": 1}})
    assert len(get_audit_trail(invoice["invoice_id"])) == 3


def test_audit_event_with_no_payload_stores_empty_collections(insert_test_invoice):
    """A payload-free event must still be readable, with empty lists not nulls."""
    invoice = insert_test_invoice()
    log_audit_event(invoice["invoice_id"], "worker_started", {})
    event = get_audit_trail(invoice["invoice_id"])[0]
    assert event["decision"] is None
    assert event["reasons"] == []
    assert event["diagnosis"] is None


def test_deleting_an_invoice_cascades_to_its_audit_trail(db_cleanup, sample_invoice):
    """Cascade is what makes per-test cleanup possible without touching audit_log."""
    invoice_id = db_cleanup(unique_invoice_id("cascade"))
    insert_invoice(dict(sample_invoice, invoice_id=invoice_id))
    log_audit_event(invoice_id, "diagnosis", {})
    assert len(get_audit_trail(invoice_id)) == 1

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM invoices WHERE invoice_id = %s", (invoice_id,))

    assert get_audit_trail(invoice_id) == []


# ─────────────────────────────────────────────────────────────────────────
# recoveries — money is only recovered once it is verified
# ─────────────────────────────────────────────────────────────────────────

def test_insert_recovery_creates_record(insert_test_invoice):
    invoice = insert_test_invoice()
    insert_recovery(
        invoice_id=invoice["invoice_id"],
        payment_link_id="plink_TEST123",
        payment_link_url="https://rzp.io/i/testlink",
        amount_requested_paise=6_00_000,
        status="link_created",
    )
    row = get_recovery(invoice["invoice_id"])
    assert row["payment_link_id"] == "plink_TEST123"
    assert row["payment_link_url"] == "https://rzp.io/i/testlink"
    assert row["amount_requested_paise"] == 6_00_000
    assert row["status"] == "link_created"


def test_insert_recovery_starts_at_zero_recovered(insert_test_invoice):
    """Creating a link is not collecting money. The recovered figure starts at zero."""
    invoice = insert_test_invoice()
    insert_recovery(invoice["invoice_id"], "plink_TEST", "https://rzp.io/i/x", 6_00_000)
    assert get_recovery(invoice["invoice_id"])["amount_recovered_paise"] == 0
    assert get_recovery(invoice["invoice_id"])["verified_at"] is None


def test_insert_recovery_twice_is_idempotent(insert_test_invoice):
    invoice = insert_test_invoice()
    insert_recovery(invoice["invoice_id"], "plink_A", "https://rzp.io/i/a", 100)
    insert_recovery(invoice["invoice_id"], "plink_B", "https://rzp.io/i/b", 200)
    row = get_recovery(invoice["invoice_id"])
    assert row["payment_link_id"] == "plink_B"
    assert row["amount_requested_paise"] == 200


def test_update_recovery_updates_status(insert_test_invoice):
    invoice = insert_test_invoice()
    insert_recovery(invoice["invoice_id"], "plink_TEST", "https://rzp.io/i/x", 6_00_000)

    update_recovery(
        invoice["invoice_id"], "payment_verified",
        amount_recovered_paise=6_00_000,
        verified_at=datetime.now(timezone.utc),
    )
    row = get_recovery(invoice["invoice_id"])
    assert row["status"] == "payment_verified"
    assert row["amount_recovered_paise"] == 6_00_000
    assert row["verified_at"] is not None


def test_recovery_amount_only_set_on_verification(insert_test_invoice):
    """The whole recovery-accounting claim, asserted end to end.

    Link created → 0 recovered. Only a verified payment moves the figure, so
    the dashboard's recovered total cannot include money that was merely
    asked for.
    """
    invoice = insert_test_invoice()
    insert_recovery(invoice["invoice_id"], "plink_TEST", "https://rzp.io/i/x", 6_00_000)
    assert get_recovery(invoice["invoice_id"])["amount_recovered_paise"] == 0

    update_recovery(
        invoice["invoice_id"], "payment_verified",
        amount_recovered_paise=6_00_000,
        verified_at=datetime.now(timezone.utc),
    )
    assert get_recovery(invoice["invoice_id"])["amount_recovered_paise"] == 6_00_000


def test_database_refuses_a_verified_recovery_with_zero_amount(insert_test_invoice):
    """The `recovered_only_when_verified` CHECK is enforced in the schema, not just in code."""
    invoice = insert_test_invoice()
    insert_recovery(invoice["invoice_id"], "plink_TEST", "https://rzp.io/i/x", 6_00_000)

    with pytest.raises(psycopg.errors.CheckViolation):
        update_recovery(invoice["invoice_id"], "payment_verified",
                        amount_recovered_paise=0,
                        verified_at=datetime.now(timezone.utc))


def test_database_refuses_a_recovered_amount_without_verified_status(insert_test_invoice):
    """Money cannot be marked recovered while the status still says a link was merely created."""
    invoice = insert_test_invoice()
    insert_recovery(invoice["invoice_id"], "plink_TEST", "https://rzp.io/i/x", 6_00_000)

    with pytest.raises(psycopg.errors.CheckViolation):
        update_recovery(invoice["invoice_id"], "link_created", amount_recovered_paise=5_00_000)


def test_update_recovery_rejects_a_float_amount(insert_test_invoice):
    """Money is an integer count of paise. This is the last gate before the column."""
    invoice = insert_test_invoice()
    insert_recovery(invoice["invoice_id"], "plink_TEST", "https://rzp.io/i/x", 6_00_000)

    with pytest.raises(TypeError, match="never a float"):
        update_recovery(invoice["invoice_id"], "payment_verified",
                        amount_recovered_paise=6000.0)


def test_update_recovery_rejects_a_bool_amount(insert_test_invoice):
    """`True` is an int in Python. It is not a sum of money."""
    invoice = insert_test_invoice()
    insert_recovery(invoice["invoice_id"], "plink_TEST", "https://rzp.io/i/x", 6_00_000)

    with pytest.raises(TypeError, match="never a float"):
        update_recovery(invoice["invoice_id"], "payment_verified",
                        amount_recovered_paise=True)


def test_get_recovery_returns_none_when_no_link_was_created(insert_test_invoice):
    """A simulated batch creates no recovery row — the absence is the signal."""
    invoice = insert_test_invoice()
    assert get_recovery(invoice["invoice_id"]) is None

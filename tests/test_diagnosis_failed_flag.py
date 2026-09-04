"""Tests for the `diagnosis_failed` flag in api.queries._load_invoice_rows.

Bug: the flag was "did this invoice ever get a diagnosis_failed event",
so an invoice that failed once and succeeded on a later retry kept the
dashboard's warning state forever. It must instead reflect only the latest
diagnosis attempt: failed if the most recent diagnosis_failed event is not
older than the most recent successful diagnosis event.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _needs_database(require_database):
    """Every test here talks to Postgres; skip the file without one."""


def _write_event(invoice_id, event_type, created_at, diagnosis=None):
    from database.connection import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log (invoice_id, event_type, diagnosis, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (invoice_id, event_type, json.dumps(diagnosis) if diagnosis else None, created_at),
            )


def _row_for(invoice_id):
    from api.queries import _load_invoice_rows

    rows = {row["invoice_id"]: row for row in _load_invoice_rows()}
    return rows[invoice_id]


_DIAGNOSIS = {
    "diagnosed_cause": "forgotten_invoice",
    "evidence_tier": "known",
    "recommended_action": "send_reminder",
    "suspicious_content_detected": False,
}


def test_a_failure_followed_by_a_successful_retry_is_not_flagged(insert_test_invoice):
    invoice_id = insert_test_invoice()["invoice_id"]
    t0 = datetime.now(timezone.utc)

    _write_event(invoice_id, "diagnosis_failed", t0)
    _write_event(invoice_id, "diagnosis", t0 + timedelta(seconds=5), diagnosis=_DIAGNOSIS)

    row = _row_for(invoice_id)
    assert row["diagnosis_failed"] is False
    assert row["diagnosis"]["diagnosed_cause"] == "forgotten_invoice"


def test_a_failure_with_no_later_diagnosis_stays_flagged(insert_test_invoice):
    invoice_id = insert_test_invoice()["invoice_id"]

    _write_event(invoice_id, "diagnosis_failed", datetime.now(timezone.utc))

    row = _row_for(invoice_id)
    assert row["diagnosis_failed"] is True
    assert row["diagnosis"] is None


def test_a_failure_after_an_earlier_diagnosis_is_flagged(insert_test_invoice):
    """An initial diagnosis followed by a *later* failed re-run must still
    flag it — the failure is the more recent attempt, not the diagnosis."""
    invoice_id = insert_test_invoice()["invoice_id"]
    t0 = datetime.now(timezone.utc)

    _write_event(invoice_id, "diagnosis", t0, diagnosis=_DIAGNOSIS)
    _write_event(invoice_id, "diagnosis_failed", t0 + timedelta(seconds=5))

    row = _row_for(invoice_id)
    assert row["diagnosis_failed"] is True


def test_an_invoice_with_no_failure_event_is_never_flagged(insert_test_invoice):
    invoice_id = insert_test_invoice()["invoice_id"]

    _write_event(invoice_id, "diagnosis", datetime.now(timezone.utc), diagnosis=_DIAGNOSIS)

    row = _row_for(invoice_id)
    assert row["diagnosis_failed"] is False

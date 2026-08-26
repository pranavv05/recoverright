"""Tests for api.razorpay_import._razorpay_invoice_to_internal — the Razorpay -> internal
invoice schema mapping used by POST /api/sync-razorpay.

Offline: no database, no Razorpay account, no LLM call. Only the pure mapping
function is under test here.
"""

import time

import pytest

from api.razorpay_import import _razorpay_invoice_to_internal


def _raw_invoice(**overrides):
    base = {
        "id": "inv_abc123",
        "status": "issued",
        "due_date": int(time.time()) - 10 * 86400,
        "amount": 500000,
        "amount_paid": 0,
        "customer": {"name": "Acme Traders"},
        "notes": {"po_number": "PO-42", "region": "west"},
        "short_url": "https://rzp.io/i/abc123",
    }
    base.update(overrides)
    return base


def test_maps_core_fields():
    invoice = _razorpay_invoice_to_internal(_raw_invoice())

    assert invoice["invoice_id"] == "RZP-inv_abc123"
    assert invoice["client_name"] == "Acme Traders"
    assert invoice["amount_paise"] == 500000
    assert invoice["partial_payment_received_paise"] == 0
    assert invoice["scenario_type"] == "razorpay_sync"
    assert invoice["true_cause"] == "unknown"
    assert invoice["client_type"] == "razorpay_merchant_client"
    assert invoice["account_value_tier"] == "standard"
    assert invoice["hard_stop"] is False


def test_days_overdue_computed_from_unix_due_date():
    invoice = _razorpay_invoice_to_internal(
        _raw_invoice(due_date=int(time.time()) - 15 * 86400)
    )
    assert invoice["days_overdue"] >= 14  # allow for wall-clock drift during the test run


def test_payment_link_status_active_when_issued_with_short_url():
    invoice = _razorpay_invoice_to_internal(_raw_invoice(status="issued"))
    assert invoice["payment_link_status"] == "active"


def test_payment_link_status_expired_when_short_url_but_not_issued():
    invoice = _razorpay_invoice_to_internal(_raw_invoice(status="expired"))
    assert invoice["payment_link_status"] == "expired"


def test_payment_link_status_none_when_no_short_url():
    invoice = _razorpay_invoice_to_internal(_raw_invoice(short_url=None))
    assert invoice["payment_link_status"] == "none"


def test_notes_concatenated_from_dict_values():
    invoice = _razorpay_invoice_to_internal(
        _raw_invoice(notes={"a": "first note", "b": "second note"})
    )
    assert "first note" in invoice["client_notes"]
    assert "second note" in invoice["client_notes"]


def test_empty_notes_dict_yields_empty_string():
    invoice = _razorpay_invoice_to_internal(_raw_invoice(notes={}))
    assert invoice["client_notes"] == ""


def test_missing_customer_name_falls_back_to_placeholder():
    invoice = _razorpay_invoice_to_internal(_raw_invoice(customer={}))
    assert invoice["client_name"] == "Unknown Razorpay Customer"


def test_falls_back_to_customer_details_when_customer_absent():
    """Live Razorpay accounts return `customer_details`, not `customer`."""
    raw = _raw_invoice()
    del raw["customer"]
    raw["customer_details"] = {"name": "Live Account Customer"}

    invoice = _razorpay_invoice_to_internal(raw)
    assert invoice["client_name"] == "Live Account Customer"


def test_fields_absent_from_razorpay_default_safely():
    invoice = _razorpay_invoice_to_internal(_raw_invoice())
    assert invoice["reminders_sent"] == 0
    assert invoice["reminders_opened"] == 0
    assert invoice["payment_link_attempts"] == 0
    assert invoice["client_payment_history"] == []


def test_missing_id_raises_value_error():
    with pytest.raises(ValueError):
        _razorpay_invoice_to_internal(_raw_invoice(id=None))


def test_missing_due_date_raises_value_error():
    with pytest.raises(ValueError):
        _razorpay_invoice_to_internal(_raw_invoice(due_date=None))


def test_falls_back_to_expire_by_when_due_date_absent():
    raw = _raw_invoice()
    del raw["due_date"]
    raw["expire_by"] = int(time.time()) - 15 * 86400

    invoice = _razorpay_invoice_to_internal(raw)
    assert invoice["days_overdue"] >= 14


def test_invalid_amount_raises_value_error():
    with pytest.raises(ValueError):
        _razorpay_invoice_to_internal(_raw_invoice(amount=0))


def test_amount_paid_exceeding_amount_raises_value_error():
    with pytest.raises(ValueError):
        _razorpay_invoice_to_internal(_raw_invoice(amount=1000, amount_paid=2000))

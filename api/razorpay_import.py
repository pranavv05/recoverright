"""Razorpay native sync — the Invoices API mapped onto the internal schema."""

import time

from execution.razorpay_client import invoice_due_date


# ─────────────────────────────────────────────────────────────────────────
# Razorpay native sync — pulls overdue invoices straight from a merchant's
# own Razorpay account via the Invoices API, instead of asking them to export
# a file first. Razorpay already stores money as integer paise, a due_date
# as a Unix timestamp, and a customer sub-object, so this mapping is a
# straight field rename rather than the rupee/CSV parsing CSV rows go
# through. Fields the CSV schema fills from merchant-supplied columns —
# reminder counts, payment history — don't exist on a single Razorpay
# invoice record, so they default to empty/zero; diagnosis and policy both
# already treat those as valid (if uninformative) inputs.
# ─────────────────────────────────────────────────────────────────────────


def _razorpay_invoice_to_internal(raw: dict) -> dict:
    """Maps one raw Razorpay invoice (from fetch_overdue_invoices) onto the internal schema."""
    razorpay_id = raw.get("id")
    if not razorpay_id:
        raise ValueError("Razorpay invoice has no 'id' — skipped")

    due_date = invoice_due_date(raw)
    if due_date is None:
        raise ValueError(f"Razorpay invoice {razorpay_id} has no valid due_date — skipped")

    amount_paise = raw.get("amount")
    if not isinstance(amount_paise, int) or isinstance(amount_paise, bool) or amount_paise <= 0:
        raise ValueError(f"Razorpay invoice {razorpay_id} has an invalid amount — skipped")

    amount_paid_paise = raw.get("amount_paid")
    if not isinstance(amount_paid_paise, int) or isinstance(amount_paid_paise, bool):
        amount_paid_paise = 0
    if amount_paid_paise > amount_paise:
        raise ValueError(
            f"Razorpay invoice {razorpay_id} has amount_paid greater than amount — skipped"
        )

    days_overdue = max(0, (int(time.time()) - due_date) // 86400)

    status = raw.get("status")
    short_url = raw.get("short_url")
    if short_url and status == "issued":
        payment_link_status = "active"
    elif short_url:
        payment_link_status = "expired"
    else:
        payment_link_status = "none"

    notes = raw.get("notes")
    client_notes = " ".join(str(v) for v in notes.values()) if isinstance(notes, dict) else ""

    # Documented field is `customer`; live accounts return `customer_details`
    # instead — same API-generation drift as due_date/expire_by above.
    customer = raw.get("customer") or raw.get("customer_details") or {}
    client_name = (customer.get("name") or "").strip() or "Unknown Razorpay Customer"

    return {
        "invoice_id": f"RZP-{razorpay_id}",
        "client_name": client_name,
        "client_type": "razorpay_merchant_client",
        "amount_paise": amount_paise,
        "partial_payment_received_paise": amount_paid_paise,
        "days_overdue": days_overdue,
        "payment_link_status": payment_link_status,
        "payment_link_attempts": 0,
        "reminders_sent": 0,
        "reminders_opened": 0,
        "client_notes": client_notes,
        "client_payment_history": [],
        "account_value_tier": "standard",
        "scenario_type": "razorpay_sync",
        "true_cause": "unknown",
        "hard_stop": False,
    }

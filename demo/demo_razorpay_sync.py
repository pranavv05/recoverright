"""Demo: Razorpay native sync end to end.

Creates real invoices in the connected Razorpay test-mode account via
POST /v1/invoices, then calls the exact same handler POST /api/sync-razorpay
runs (api.main.api_sync_razorpay) so the demo exercises the real
fetch -> map -> diagnose -> policy -> execute -> audit pipeline against real
Razorpay-issued invoice IDs, customer data, and payment links.

One simulation is layered on top, out of necessity: Razorpay's Invoice API
requires `expire_by` to be at least 15 minutes in the future at creation time
(`expire_by should be at least 15 minutes after current time`), so a freshly
created invoice cannot be backdated into "overdue" the moment it's created.
RazorpayClient.fetch_overdue_invoices is monkeypatched for the duration of
this script to return the real, just-created invoices with their due date
shifted into the past — simulating what the account will look like once
these invoices naturally age past due. Every other part of the pipeline
below is unmodified production code: same functions, same database, same
policy engine, same audit trail the live endpoint uses.
"""
import os
import sys
import time
from unittest.mock import patch

import httpx
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

load_dotenv(os.path.join(REPO_ROOT, ".env"))

from database.connection import get_connection
from execution.razorpay_client import RazorpayClient
from audit.trail import get_audit_trail

TEST_CUSTOMERS = [
    {
        "customer": {"name": "Sharma Textiles", "contact": "9999999991", "email": "sharma.textiles@example.com"},
        "line_items": [{"name": "B2B order — August batch", "amount": 750000, "currency": "INR", "quantity": 1}],
        "notes": {"context": "Client says goods were delayed in transit, following up before chasing payment"},
        "simulated_days_overdue": 12,
    },
    {
        "customer": {"name": "Kapoor Electricals", "contact": "9999999992", "email": "kapoor.electricals@example.com"},
        "line_items": [{"name": "Bulk wiring supplies", "amount": 1250000, "currency": "INR", "quantity": 1}],
        "notes": {},
        "simulated_days_overdue": 30,
    },
]


def create_real_invoice(rzp_client: RazorpayClient, spec: dict) -> dict:
    """Creates a real, live test-mode Razorpay invoice via POST /v1/invoices."""
    payload = {
        "type": "invoice",
        "customer": spec["customer"],
        "line_items": spec["line_items"],
        "sms_notify": 0,
        "email_notify": 0,
        "notes": spec["notes"],
    }
    response = httpx.post(
        f"{rzp_client.BASE_URL}/invoices",
        headers=rzp_client.headers,
        json=payload,
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()


def simulate_overdue(raw_invoice: dict, days_overdue: int) -> dict:
    """Copies a real Razorpay invoice, shifting its due date into the past.

    Only `expire_by`/`due_date` is touched — id, customer, amount, notes,
    short_url all stay exactly what Razorpay returned.
    """
    simulated = dict(raw_invoice)
    past = int(time.time()) - days_overdue * 86400
    simulated["expire_by"] = past
    simulated["due_date"] = past
    return simulated


def reset_demo(invoice_ids: list):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE invoice_id = ANY(%s)", (invoice_ids,))
            cur.execute("DELETE FROM recoveries WHERE invoice_id = ANY(%s)", (invoice_ids,))
            cur.execute("DELETE FROM invoices WHERE invoice_id = ANY(%s)", (invoice_ids,))
    print(f"[RESET] Cleared {len(invoice_ids)} prior demo rows\n")


def main():
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        raise SystemExit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set in .env")

    rzp_client = RazorpayClient(key_id=key_id, key_secret=key_secret)

    print("[1/4] Creating real test-mode Razorpay invoices via POST /v1/invoices...")
    created = [create_real_invoice(rzp_client, spec) for spec in TEST_CUSTOMERS]
    for c in created:
        print(f"      {c['id']}  {c['customer_details']['name']}  amount={c['amount']}  status={c['status']}")

    simulated = [
        simulate_overdue(raw, spec["simulated_days_overdue"])
        for raw, spec in zip(created, TEST_CUSTOMERS)
    ]

    expected_ids = [f"RZP-{c['id']}" for c in created]
    reset_demo(expected_ids)

    print("\n[2/4] Calling the real POST /api/sync-razorpay handler "
          "(fetch_overdue_invoices monkeypatched to return the invoices above, "
          "aged into the past — see module docstring)...")
    with patch.object(RazorpayClient, "fetch_overdue_invoices", return_value=simulated):
        import api.main as main_module
        result = main_module.api_sync_razorpay()

    print("\n--- /api/sync-razorpay response ---")
    import json as _json
    print(_json.dumps(result, indent=2))

    print("\n[3/4] Database rows written by the sync:")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT invoice_id, client_name, amount_paise, days_overdue, scenario_type "
                "FROM invoices WHERE invoice_id = ANY(%s) ORDER BY invoice_id",
                (expected_ids,)
            )
            for row in cur.fetchall():
                print(f"      {dict(row)}")

    print(f"\n[4/4] Audit trail for {expected_ids[0]}:")
    for event in get_audit_trail(expected_ids[0]):
        print(f"      {event['event_type']:<20} decision={event['decision']}")


if __name__ == "__main__":
    main()

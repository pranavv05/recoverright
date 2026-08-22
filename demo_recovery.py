"""Demo flow 1: positive recovery — payment_link_failure invoice recovered end-to-end
via a real Razorpay test-mode payment link, then verified through reconciliation.

DEMO-1010 is a synthetic demo-only fixture, not one of the 52 real invoices in
data/seen.json/held_out.json. It mirrors INV-1010 (Star Bazaar Enterprises,
same payment_link_failure scenario, same client notes) but at a smaller amount:
INV-1010's real outstanding balance (Rs 60,355) exceeds this Razorpay test
account's current Payment Link amount cap (observed between Rs 50,000-55,000,
likely tied to KYC/activation status). Every other invoice in the dataset that
reaches ALLOW + create_payment_link (INV-1016, INV-1015) is even further over
that cap, so no real dataset invoice can complete this flow until the cap is
raised. demo_blocked.py and demo_api_failure.py both use real dataset invoices.
"""
import os
import sys

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from api.main import process_invoice, build_rzp_client, simulate_test_payment
from audit.trail import get_recovery

INVOICE_ID = "DEMO-1010"

DEMO_INVOICE = {
    "invoice_id": INVOICE_ID,
    "client_name": "Star Bazaar Enterprises",
    "client_type": "retail_chain",
    "amount_paise": 4500000,
    "issue_date": "2026-07-13",
    "due_date": "2026-08-12",
    "days_overdue": 10,
    "partial_payment_received_paise": 0,
    "payment_link_status": "expired",
    "payment_link_attempts": 3,
    "reminders_sent": 1,
    "reminders_opened": 1,
    "client_notes": "The Razorpay link throws a 404 error on my end. Please share a fresh one.",
    "client_payment_history": [
        "paid_on_time", "paid_on_time", "paid_on_time",
        "paid_on_time", "paid_on_time", "paid_on_time"
    ],
    "account_value_tier": "standard",
    "scenario_type": "payment_link_failure",
    "true_cause": "payment_link_failure",
    "evidence_tier": "known",
    "recoverability": "recoverable",
    "correct_action": "create_payment_link",
    "requires_human": False,
    "hard_stop": False
}


def load_invoice(invoice_id: str) -> dict:
    if invoice_id != INVOICE_ID:
        raise ValueError(f"unexpected invoice_id {invoice_id}")
    return DEMO_INVOICE


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def main():
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_api_key_here":
        raise SystemExit("NVIDIA_API_KEY not set in .env")

    rzp_client = build_rzp_client()
    if rzp_client is None:
        raise SystemExit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set in .env")

    invoice = load_invoice(INVOICE_ID)
    print(f"=== Demo Flow 1: Positive Recovery — {INVOICE_ID} ({invoice['client_name']}) ===")
    print("(synthetic demo fixture — same scenario as INV-1010, scaled under the account's Payment Link cap)\n")
    print(f"Client notes: \"{invoice['client_notes']}\"\n")

    print("[1/5] Diagnosing invoice...")
    result = process_invoice(invoice, api_key, rzp_client)
    diagnosis = result["diagnosis"]
    print(f"      diagnosed_cause = {diagnosis.get('diagnosed_cause')}")
    print(f"      evidence_tier   = {diagnosis.get('evidence_tier')}")
    print(f"      recommended     = {diagnosis.get('recommended_action')}\n")

    print("[2/5] Policy decision...")
    print(f"      decision = {result['policy_decision']}")
    print(f"      reasons  = {result['policy_reasons']}\n")

    execution = result["execution"]
    if result["policy_decision"] != "ALLOW" or not execution or execution.get("status") != "link_created":
        print(f"      Did not reach a created payment link — execution result: {execution}")
        return

    print("[3/5] Payment link created via real Razorpay test-mode API...")
    print(f"      payment_link_id = {execution['payment_link_id']}")
    print(f"      url             = {execution['short_url']}")
    print(f"      amount          = {rupees(execution['amount_paise'])}\n")

    print("[4/5] Simulating test payment (opens the link in your browser — complete it with")
    print("      Razorpay's test card to see live verification):")
    recon = simulate_test_payment(
        rzp_client,
        invoice_id=INVOICE_ID,
        payment_link_id=execution["payment_link_id"],
        short_url=execution["short_url"]
    )
    print(f"      reconciliation result = {recon}\n")

    print("[5/5] Final recovery record:")
    recovery = get_recovery(INVOICE_ID)
    if recovery:
        print(f"      status            = {recovery['status']}")
        print(f"      amount_requested  = {rupees(recovery['amount_requested_paise'])}")
        print(f"      amount_recovered  = {rupees(recovery['amount_recovered_paise'] or 0)}")
        print(f"      verified_at       = {recovery['verified_at']}")
    else:
        print("      no recovery row found")


if __name__ == "__main__":
    main()

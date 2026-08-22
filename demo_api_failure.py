"""Demo flow 3: real Razorpay API failure — deliberately bad credentials trigger a genuine
401 from Razorpay's API. Shows the failure being logged to the audit trail and the
invoice correctly NOT marked as recovered.
"""
import json
import os
import sys

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from api.main import process_invoice
from audit.trail import get_audit_trail, get_recovery
from execution.razorpay_client import RazorpayClient

INVOICE_ID = "INV-1016"


def load_invoice(invoice_id: str) -> dict:
    with open("data/seen.json", encoding="utf-8") as f:
        invoices = json.load(f)
    for inv in invoices:
        if inv["invoice_id"] == invoice_id:
            return inv
    raise ValueError(f"{invoice_id} not found in data/seen.json")


def main():
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_api_key_here":
        raise SystemExit("NVIDIA_API_KEY not set in .env")

    # Deliberately invalid credentials — Razorpay will return a real 401.
    bad_rzp_client = RazorpayClient(key_id="rzp_test_deliberatelybad", key_secret="deliberately_invalid_secret")

    invoice = load_invoice(INVOICE_ID)
    print(f"=== Demo Flow 3: API Failure Handling — {INVOICE_ID} ({invoice['client_name']}) ===\n")
    print(f"Client notes: \"{invoice['client_notes']}\"\n")

    print("[1/3] Running the pipeline with a deliberately broken Razorpay key...")
    result = process_invoice(invoice, api_key, bad_rzp_client)
    diagnosis = result["diagnosis"]
    print(f"      diagnosed_cause  = {diagnosis.get('diagnosed_cause')}")
    print(f"      policy decision  = {result['policy_decision']}")
    print(f"      execution result = {result['execution']}\n")

    print("[2/3] Verifying no false recovery was recorded...")
    recovery = get_recovery(INVOICE_ID)
    if recovery is None:
        print("      recoveries row = None  <-- correctly not marked as recovered")
    else:
        print(f"      recoveries row = {recovery}")
        assert recovery["status"] != "payment_verified", "invoice was incorrectly marked as recovered!"
        print("      status is not payment_verified  <-- correctly not marked as recovered")

    print("\n[3/3] Audit trail for this invoice (the real API failure, logged):")
    for row in get_audit_trail(INVOICE_ID):
        print(f"      [{row['created_at']}] {row['event_type']:20s} decision={row['decision']}")
        if row["execution_result"]:
            print(f"          execution_result: {row['execution_result']}")


if __name__ == "__main__":
    main()

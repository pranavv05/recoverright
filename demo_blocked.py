"""Demo flow 2: disputed invoice — dispute detected in client notes, all automated
customer-facing action halted, full reasoning captured in the audit trail.
"""
import json
import os
import sys

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from api.main import process_invoice, build_rzp_client
from audit.trail import get_audit_trail

INVOICE_ID = "INV-1001"


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

    rzp_client = build_rzp_client()

    invoice = load_invoice(INVOICE_ID)
    print(f"=== Demo Flow 2: Blocked Action — {INVOICE_ID} ({invoice['client_name']}) ===\n")
    print(f"Client notes: \"{invoice['client_notes']}\"\n")

    print("[1/3] Diagnosing invoice...")
    result = process_invoice(invoice, api_key, rzp_client)
    diagnosis = result["diagnosis"]
    print(f"      diagnosed_cause = {diagnosis.get('diagnosed_cause')}")
    print(f"      evidence_tier   = {diagnosis.get('evidence_tier')}\n")

    print("[2/3] Policy decision...")
    print(f"      decision  = {result['policy_decision']}")
    print(f"      reasons   = {result['policy_reasons']}")
    print(f"      execution = {result['execution']}   <-- no automated customer-facing action taken\n")

    if result["policy_decision"] == "ALLOW":
        print("      NOTE: policy allowed this invoice — dispute keywords were not matched as expected.")

    print("[3/3] Audit trail for this invoice (what was blocked, and why):")
    for row in get_audit_trail(INVOICE_ID):
        print(f"      [{row['created_at']}] {row['event_type']:20s} decision={row['decision']}")
        if row["reasons"]:
            print(f"          reasons: {row['reasons']}")
        if row["blocked_actions"]:
            print(f"          blocked: {row['blocked_actions']}")


if __name__ == "__main__":
    main()

"""Demo flow 3: real Razorpay API failure — deliberately bad credentials trigger
a genuine 401 from Razorpay's API. Shows the failure being logged to the audit
trail and the invoice correctly NOT marked as recovered.

Loads INV-1016 (Solapur Textile Mills) from data/seen.json. Diagnosis and
policy evaluation run for real and should reach ALLOW; only the Razorpay
credentials used for payment link creation are deliberately wrong.

Standalone — does not require the batch pipeline to have been run first.
"""
import json
import os
import sys

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

load_dotenv(os.path.join(REPO_ROOT, ".env"))

from audit.trail import insert_invoice, log_audit_event, get_audit_trail, get_recovery
from diagnosis.diagnoser import diagnose
from policy.engine import PolicyInput, evaluate
from execution.razorpay_client import RazorpayClient
from execution.actions import execute_action

INVOICE_ID = "INV-1016"


def load_invoice(invoice_id: str) -> dict:
    with open(os.path.join(REPO_ROOT, "data", "seen.json"), encoding="utf-8") as f:
        invoices = json.load(f)
    for inv in invoices:
        if inv["invoice_id"] == invoice_id:
            return inv
    raise ValueError(f"{invoice_id} not found in data/seen.json")


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def main():
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_api_key_here":
        raise SystemExit("NVIDIA_API_KEY not set in .env")

    invoice = load_invoice(INVOICE_ID)
    outstanding = invoice["amount_paise"] - invoice["partial_payment_received_paise"]

    print(f"=== Demo Flow 3: API Failure Handling — {INVOICE_ID} ===\n")

    print("Step 1: Invoice details")
    print(f"  Client:        {invoice['client_name']}")
    print(f"  Amount:        {rupees(invoice['amount_paise'])} (outstanding: {rupees(outstanding)})")
    print(f"  Days overdue:  {invoice['days_overdue']}")
    print(f"  Client notes:  \"{invoice['client_notes']}\"\n")

    insert_invoice(invoice)

    print("Step 2: Diagnosis")
    diagnosis = diagnose(invoice, api_key)
    log_audit_event(INVOICE_ID, "diagnosis", {"diagnosis": diagnosis})
    print(f"  Cause:               {diagnosis.get('diagnosed_cause')}")
    print(f"  Evidence tier:       {diagnosis.get('evidence_tier')}")
    print(f"  Recommended action:  {diagnosis.get('recommended_action')}\n")

    print("Step 3: Policy decision")
    policy_input = PolicyInput(
        invoice_id=INVOICE_ID,
        amount_paise=invoice["amount_paise"],
        outstanding_balance_paise=outstanding,
        days_overdue=invoice["days_overdue"],
        account_value_tier=invoice["account_value_tier"],
        evidence_tier=diagnosis.get("evidence_tier", "unknown"),
        recommended_action=diagnosis.get("recommended_action", "gather_information"),
        requested_amount_paise=outstanding,
        existing_payment_link=invoice["payment_link_status"] == "active",
        client_notes=invoice.get("client_notes") or "",
        hard_stop=invoice.get("hard_stop", False),
        diagnosed_cause=diagnosis.get("diagnosed_cause", "unknown"),
        reminders_sent=invoice["reminders_sent"]
    )
    decision = evaluate(policy_input)
    log_audit_event(INVOICE_ID, "policy_decision", {
        "decision": decision.decision,
        "reasons": decision.reasons,
        "allowed_actions": decision.allowed_actions,
        "blocked_actions": decision.blocked_actions,
        "diagnosis": diagnosis
    })
    print(f"  Decision: {decision.decision}")
    print(f"  Reasons:  {decision.reasons}\n")

    if decision.decision != "ALLOW":
        print(f"  NOTE: expected ALLOW for this invoice, got {decision.decision} — stopping demo here.")
        return

    print("Step 4: Attempting payment link creation with deliberately wrong Razorpay credentials...")
    bad_client = RazorpayClient(key_id="rzp_test_FAKE_KEY", key_secret="FAKE_SECRET")
    execution_result = execute_action(
        action="create_payment_link",
        invoice=invoice,
        outstanding_paise=outstanding,
        rzp_client=bad_client
    )
    log_audit_event(INVOICE_ID, "execution", {"execution": execution_result})
    print(f"  execution result: {execution_result}\n")

    print("Step 5: API failure details")
    print(f"  status: {execution_result.get('status')}")
    print(f"  error:  {execution_result.get('error')}\n")

    print("Step 6: Audit trail — failure logged")
    for row in get_audit_trail(INVOICE_ID):
        print(f"  [{row['created_at']}] {row['event_type']}")
        if row["execution_result"]:
            print(f"      execution_result: {row['execution_result']}")
    print()

    recovery = get_recovery(INVOICE_ID)
    print("Step 7:")
    print("RESULT: Invoice NOT marked as recovered.")
    print("        Failure logged in audit trail.")
    print(f"        recoveries table shows: {recovery if recovery else 'None (no recovery record created)'}")


if __name__ == "__main__":
    main()

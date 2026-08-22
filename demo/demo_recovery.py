"""Demo flow 1: successful end-to-end recovery — a real Razorpay test-mode
payment link is created for a synthetic demo invoice, opened in the browser,
and polled until the test payment clears.

DEMO-1010 is a synthetic fixture, not one of the 52 real invoices in
data/seen.json. It mirrors INV-1010 (Star Bazaar Enterprises, same
payment_link_failure scenario, same client notes) at a smaller amount so it
stays under this Razorpay test account's Payment Link cap.

Standalone — does not require the batch pipeline to have been run first.
Requires completing a real test-mode payment in the browser; this script
will not finish automatically.
"""
import os
import sys
import time
import webbrowser

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

load_dotenv(os.path.join(REPO_ROOT, ".env"))

from audit.trail import insert_invoice, log_audit_event, insert_recovery, get_recovery
from diagnosis.diagnoser import diagnose
from policy.engine import PolicyInput, evaluate
from execution.razorpay_client import RazorpayClient
from execution.actions import execute_action, reconcile_payment

INVOICE_ID = "DEMO-1010"

DEMO_INVOICE = {
    "invoice_id": INVOICE_ID,
    "client_name": "Star Bazaar Enterprises (Demo)",
    "client_type": "retail_chain",
    "amount_paise": 4500000,
    "days_overdue": 10,
    "partial_payment_received_paise": 0,
    "payment_link_status": "expired",
    "payment_link_attempts": 3,
    "reminders_sent": 1,
    "reminders_opened": 1,
    "client_notes": "The Razorpay link throws a 404 error. Please share a fresh one.",
    "client_payment_history": ["paid_on_time"] * 6,
    "account_value_tier": "standard",
    "scenario_type": "payment_link_failure",
    "true_cause": "payment_link_failure",
    "evidence_tier": "known",
    "hard_stop": False
}


def rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"


def main():
    print("[DEMO FIXTURE — synthetic invoice for demonstration purposes]")
    print("[Same scenario as INV-1010: payment link failure, client ready to pay]\n")

    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_api_key_here":
        raise SystemExit("NVIDIA_API_KEY not set in .env")

    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        raise SystemExit("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set in .env")
    rzp_client = RazorpayClient(key_id=key_id, key_secret=key_secret)

    invoice = DEMO_INVOICE
    outstanding = invoice["amount_paise"] - invoice["partial_payment_received_paise"]

    print(f"=== Demo Flow 1: Recovery — {INVOICE_ID} ({invoice['client_name']}) ===\n")

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
    print(f"  Allowed:  {decision.allowed_actions}\n")

    if decision.decision != "ALLOW":
        print(f"  Did not reach ALLOW (got {decision.decision}) — stopping demo here.")
        return

    print("Step 4: Creating a real Razorpay test-mode payment link...")
    execution_result = execute_action(
        action="create_payment_link",
        invoice=invoice,
        outstanding_paise=outstanding,
        rzp_client=rzp_client
    )
    log_audit_event(INVOICE_ID, "execution", {"execution": execution_result})

    if execution_result.get("status") != "link_created":
        print(f"  Payment link creation failed: {execution_result}")
        return

    insert_recovery(
        invoice_id=INVOICE_ID,
        payment_link_id=execution_result["payment_link_id"],
        payment_link_url=execution_result["short_url"],
        amount_requested_paise=execution_result["amount_paise"],
        status="link_created"
    )

    print("Step 5: Payment link created")
    print(f"  Payment link ID:  {execution_result['payment_link_id']}")
    print(f"  Payment link URL: {execution_result['short_url']}\n")

    print("Step 6: Opening the link in your browser for a test payment...")
    print("  Pay with Razorpay's test card: 4111 1111 1111 1111 / any future expiry / any CVV")
    try:
        webbrowser.open(execution_result["short_url"])
    except Exception:
        pass

    print("\nStep 7: Polling reconcile_payment() every 5 seconds for up to 60 seconds...")
    elapsed = 0
    result = {"status": "pending"}
    while elapsed < 60:
        result = reconcile_payment(rzp_client, INVOICE_ID, execution_result["payment_link_id"])
        if result["status"] == "payment_verified":
            break
        time.sleep(5)
        elapsed += 5

    if result["status"] == "payment_verified":
        print(f"\nStep 8: RECOVERED {rupees(result['amount_recovered_paise'])}")
        recovery = get_recovery(INVOICE_ID)
        if recovery:
            print(f"  recoveries table: status={recovery['status']}, "
                  f"amount_recovered={rupees(recovery['amount_recovered_paise'] or 0)}, "
                  f"verified_at={recovery['verified_at']}")
    else:
        print("\nStep 9: Reconciliation pending — complete the test payment in your browser")
        print("        to see the recovery confirmed.")


if __name__ == "__main__":
    main()

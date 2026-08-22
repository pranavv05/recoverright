import json
import os
import sys
import time
import webbrowser
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from audit.trail import insert_invoice, insert_recovery, log_audit_event
from policy.engine import PolicyInput, evaluate
from diagnosis.diagnoser import diagnose
from execution.actions import execute_action, reconcile_payment
from execution.razorpay_client import RazorpayClient

load_dotenv()


def build_rzp_client():
    """Instantiates a RazorpayClient from env vars, or None if credentials aren't set."""
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None
    return RazorpayClient(key_id=key_id, key_secret=key_secret)


def simulate_test_payment(
    rzp_client,
    invoice_id: str,
    payment_link_id: str,
    short_url: str,
    poll_seconds: int = 30,
    poll_interval: int = 5
) -> dict:
    """
    Razorpay's Payment Links product requires payment collection to go through
    its own hosted Checkout (PCI-DSS) — there is no server-side API to submit
    a card and complete a payment. This opens the real test-mode link in a
    browser (pay with Razorpay's published test card 4111 1111 1111 1111, any
    future expiry, any CVV) and polls reconcile_payment() until the payment
    link reports "paid" or the timeout elapses.
    """
    print(f"      Opening {short_url}")
    print("      Pay with Razorpay's test card: 4111 1111 1111 1111 / any future expiry / any CVV")
    try:
        webbrowser.open(short_url)
    except Exception:
        pass

    elapsed = 0
    while True:
        result = reconcile_payment(rzp_client, invoice_id, payment_link_id)
        if result["status"] == "payment_verified" or elapsed >= poll_seconds:
            return result
        time.sleep(poll_interval)
        elapsed += poll_interval


def load_dataset(path: str) -> list:
    """Loads a JSON invoice dataset from disk."""
    with open(path) as f:
        return json.load(f)

def process_invoice(invoice: dict, api_key: str, rzp_client=None) -> dict:
    """Runs one invoice through the full pipeline: insert → diagnose → policy → execute → audit."""
    invoice_id = invoice["invoice_id"]

    # Step 1: Insert invoice into DB first
    insert_invoice(invoice)

    # Step 2: Check outstanding balance
    outstanding = invoice["amount_paise"] - invoice["partial_payment_received_paise"]
    if outstanding <= 0:
        log_audit_event(invoice_id, "skipped", {
            "reason": "outstanding_balance_zero_or_negative"
        })
        return {"invoice_id": invoice_id, "status": "skipped"}

    # Step 3: Diagnose
    try:
        diagnosis = diagnose(invoice, api_key)
    except Exception as e:
        log_audit_event(invoice_id, "diagnosis_failed", {"error": str(e)})
        print(f"  DIAGNOSIS ERROR [{invoice_id}]: {e}")
        return {"invoice_id": invoice_id, "status": "diagnosis_failed", "error": str(e)}

    log_audit_event(invoice_id, "diagnosis", {"diagnosis": diagnosis})

    # Step 4: Policy evaluation
    policy_input = PolicyInput(
        invoice_id=invoice_id,
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

    log_audit_event(invoice_id, "policy_decision", {
        "decision": decision.decision,
        "reasons": decision.reasons,
        "allowed_actions": decision.allowed_actions,
        "blocked_actions": decision.blocked_actions,
        "diagnosis": diagnosis
    })

    # Step 5: Execute if ALLOW
    execution_result = None
    if decision.decision == "ALLOW":
        if rzp_client:
            try:
                execution_result = execute_action(
                    action=decision.allowed_actions[0] if decision.allowed_actions else "gather_information",
                    invoice=invoice,
                    outstanding_paise=outstanding,
                    rzp_client=rzp_client
                )
                if execution_result.get("status") == "link_created":
                    insert_recovery(
                        invoice_id=invoice_id,
                        payment_link_id=execution_result["payment_link_id"],
                        payment_link_url=execution_result["short_url"],
                        amount_requested_paise=execution_result["amount_paise"],
                        status="link_created"
                    )
                    log_audit_event(invoice_id, "payment_link_created", {
                        "action": "create_payment_link",
                        "payment_link_id": execution_result["payment_link_id"],
                        "short_url": execution_result["short_url"]
                    })
            except Exception as e:
                execution_result = {"status": "failed", "error": str(e)}
            log_audit_event(invoice_id, "execution", {"execution": execution_result})
        else:
            execution_result = {
                "status": "simulated",
                "action": decision.allowed_actions[0] if decision.allowed_actions else "none"
            }
            log_audit_event(invoice_id, "execution_simulated", {"execution": execution_result})

    return {
        "invoice_id": invoice_id,
        "diagnosis": diagnosis,
        "policy_decision": decision.decision,
        "policy_reasons": decision.reasons,
        "execution": execution_result
    }


def run_batch(dataset_path: str, limit: int = None, rzp_client=None) -> list:
    """Processes every invoice in the dataset through the pipeline and returns per-invoice results."""
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_api_key_here":
        raise ValueError("NVIDIA_API_KEY not set in .env")

    invoices = load_dataset(dataset_path)
    if limit:
        invoices = invoices[:limit]

    results = []
    for i, invoice in enumerate(invoices):
        print(f"Processing {i+1}/{len(invoices)}: {invoice['invoice_id']} ({invoice['scenario_type']})")
        result = process_invoice(invoice, api_key, rzp_client)
        results.append(result)
        print(f"  → {result.get('policy_decision', 'n/a')} | {result.get('diagnosis', {}).get('diagnosed_cause', 'n/a')}")

    return results



if __name__ == "__main__":
    rzp_client = build_rzp_client()
    results = run_batch("data/seen.json", limit=None, rzp_client=rzp_client)
    print(f"\nProcessed {len(results)} invoices")
    allow = sum(1 for r in results if r.get("policy_decision") == "ALLOW")
    block = sum(1 for r in results if r.get("policy_decision") == "BLOCK")
    human = sum(1 for r in results if r.get("policy_decision") == "REQUIRE_HUMAN")
    print(f"ALLOW: {allow} | BLOCK: {block} | REQUIRE_HUMAN: {human}")
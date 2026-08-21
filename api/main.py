import json
import os
import sys
import time
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from audit.trail import insert_invoice, log_audit_event
from policy.engine import PolicyInput, evaluate
from diagnosis.diagnoser import diagnose
from execution.actions import execute_action
from database.connection import get_connection

load_dotenv()


def load_dataset(path: str) -> list:
    with open(path) as f:
        return json.load(f)

def process_invoice(invoice: dict, groq_api_key: str, rzp_client=None) -> dict:
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
        diagnosis = diagnose(invoice, groq_api_key)
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
        hard_stop=invoice.get("hard_stop", False)
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


def run_batch(dataset_path: str, limit: int = None) -> list:
    groq_api_key = os.environ.get("GROQ_API_KEY")
    if not groq_api_key or groq_api_key == "your_groq_key_here":
        raise ValueError("GROQ_API_KEY not set in .env")

    invoices = load_dataset(dataset_path)
    if limit:
        invoices = invoices[:limit]

    results = []
    for i, invoice in enumerate(invoices):
        print(f"Processing {i+1}/{len(invoices)}: {invoice['invoice_id']} ({invoice['scenario_type']})")
        result = process_invoice(invoice, groq_api_key)
        results.append(result)
        print(f"  → {result.get('policy_decision', 'n/a')} | {result.get('diagnosis', {}).get('diagnosed_cause', 'n/a')}")
        time.sleep(1.5)   # spread requests out to stay within Groq's per-minute token budget

    return results



if __name__ == "__main__":
    results = run_batch("data/seen.json", limit=None)
    print(f"\nProcessed {len(results)} invoices")
    allow = sum(1 for r in results if r.get("policy_decision") == "ALLOW")
    block = sum(1 for r in results if r.get("policy_decision") == "BLOCK")
    human = sum(1 for r in results if r.get("policy_decision") == "REQUIRE_HUMAN")
    print(f"ALLOW: {allow} | BLOCK: {block} | REQUIRE_HUMAN: {human}")
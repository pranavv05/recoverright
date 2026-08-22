"""Demo flow 2: disputed invoice — blocked before any customer-facing action.

Loads INV-1001 (Amravati Steel Rollers) from data/seen.json and runs it
through the full pipeline (insert_invoice -> diagnose -> policy evaluate),
showing the dispute signal in the client's notes get caught deterministically
and routed to a human with zero automated customer contact.

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

from audit.trail import insert_invoice, log_audit_event, get_audit_trail
from diagnosis.diagnoser import diagnose
from policy.engine import PolicyInput, evaluate

INVOICE_ID = "INV-1001"


def load_invoice(invoice_id: str) -> dict:
    with open(os.path.join(REPO_ROOT, "data", "seen.json"), encoding="utf-8") as f:
        invoices = json.load(f)
    for inv in invoices:
        if inv["invoice_id"] == invoice_id:
            return inv
    raise ValueError(f"{invoice_id} not found in data/seen.json")


def rupees(paise: int) -> str:
    """Formats integer paise as rupees. Integer arithmetic — paise never becomes a float."""
    sign = "-" if paise < 0 else ""
    whole, fraction = divmod(abs(paise), 100)
    return f"{sign}Rs {whole:,}.{fraction:02d}"


def main():
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_api_key_here":
        raise SystemExit("NVIDIA_API_KEY not set in .env")

    invoice = load_invoice(INVOICE_ID)
    outstanding = invoice["amount_paise"] - invoice["partial_payment_received_paise"]

    print(f"=== Demo Flow 2: Blocked Dispute — {INVOICE_ID} ===\n")

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
    print(f"  Evidence cited:      {diagnosis.get('evidence_cited')}")
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
    print(f"  Decision:        {decision.decision}")
    print(f"  Reasons:         {decision.reasons}")
    print(f"  Blocked actions: {decision.blocked_actions}\n")

    print("Step 4: Audit trail — what was blocked, and why")
    for row in get_audit_trail(INVOICE_ID):
        print(f"  [{row['created_at']}] {row['event_type']}")
        if row["reasons"]:
            print(f"      reasons: {row['reasons']}")
        if row["blocked_actions"]:
            print(f"      blocked: {row['blocked_actions']}")
    print()

    print("Step 5:")
    print("RESULT: Zero customer-facing actions taken. Routed to human review.")


if __name__ == "__main__":
    main()

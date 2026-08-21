# save as test_policy.py
import json
from policy.engine import PolicyInput, evaluate
from policy.rules import DISPUTE_KEYWORDS

with open("data/seen.json") as f:
    invoices = json.load(f)

DISPUTE_KEYWORDS = [
    "dispute", "disputing", "not agreed", "incorrect",
    "wrong amount", "mismatch", "damaged", "rejected",
    "should not have been raised", "cancelled",
    "reconcile", "billed incorrectly", "only 2 were delivered",
    "not delivered", "short delivery", "overcharged",
    "never agreed", "requesting revised", "breakdown before payment",
    "inflated", "quality issue", "withholding payment",
    "credit note", "po value", "signed po",
    "duplicate charges", "already settled",
    "weight-based charges", "fuel surcharge"
]

inv = invoices[0]  # INV-1001 — disputed
print("Client notes:", inv["client_notes"])
print()

notes_lower = inv["client_notes"].lower()
for kw in DISPUTE_KEYWORDS:
    if kw in notes_lower:
        print(f"MATCHED keyword: '{kw}'")

print()
policy_input = PolicyInput(
    invoice_id=inv["invoice_id"],
    amount_paise=inv["amount_paise"],
    outstanding_balance_paise=inv["amount_paise"] - inv["partial_payment_received_paise"],
    days_overdue=inv["days_overdue"],
    account_value_tier=inv["account_value_tier"],
    evidence_tier="known",
    recommended_action="escalate_to_human",
    requested_amount_paise=inv["amount_paise"],
    existing_payment_link=inv["payment_link_status"] == "active",
    client_notes=inv.get("client_notes") or "",
    hard_stop=inv.get("hard_stop", False)
)
decision = evaluate(policy_input)
print("Decision:", decision.decision)
print("Reasons:", decision.reasons)
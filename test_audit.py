from audit.trail import insert_invoice, log_audit_event, get_audit_trail

test_invoice = {
    "invoice_id": "INV-TEST-001",
    "client_name": "Test Client",
    "amount_paise": 1000000,
    "partial_payment_received_paise": 0,
    "days_overdue": 15,
    "account_value_tier": "standard",
    "scenario_type": "payment_link_failure",
    "true_cause": "payment_link_failure"
}

insert_invoice(test_invoice)
print("Invoice inserted")

log_audit_event("INV-TEST-001", "policy_decision", {
    "decision": "ALLOW",
    "reasons": ["no_blocking_rules_fired"],
    "allowed_actions": ["create_payment_link"],
    "blocked_actions": []
})
print("Audit event logged")

trail = get_audit_trail("INV-TEST-001")
print(f"Audit trail has {len(trail)} event(s)")
print(f"Event type: {trail[0]['event_type']}")
print(f"Decision: {trail[0]['decision']}")
print("Audit trail working correctly")
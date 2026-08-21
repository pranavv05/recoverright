import json
from database.connection import get_connection

with get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 
                i.invoice_id,
                i.scenario_type,
                i.true_cause,
                al.decision,
                al.reasons
            FROM invoices i
            JOIN audit_log al ON i.invoice_id = al.invoice_id
            WHERE al.event_type = 'policy_decision'
            ORDER BY i.scenario_type, i.invoice_id
        """)
        rows = cur.fetchall()

print(f"{'Invoice':<12} {'Scenario':<28} {'True Cause':<22} {'Decision':<15}")
print("-" * 80)

wrong = []
for r in rows:
    decision = r['decision']
    scenario = r['scenario_type']
    
    # Flag obvious wrong decisions
    is_wrong = False
    if scenario == 'disputed_invoice' and decision == 'ALLOW':
        is_wrong = True
    if scenario == 'legal_threat' and decision == 'ALLOW':
        is_wrong = True
    if scenario == 'duplicate_invoice' and decision == 'ALLOW':
        is_wrong = True
    if scenario == 'chronic_late_payer_normal' and decision in ('BLOCK', 'REQUIRE_HUMAN'):
        is_wrong = True
    if scenario == 'payment_link_failure' and r['true_cause'] == 'payment_link_failure' and decision == 'BLOCK':
        is_wrong = True

    flag = " ← WRONG" if is_wrong else ""
    print(f"{r['invoice_id']:<12} {scenario:<28} {r['true_cause']:<22} {decision:<15}{flag}")
    if is_wrong:
        wrong.append(r)

print(f"\nTotal: {len(rows)} | Wrong decisions flagged: {len(wrong)}")
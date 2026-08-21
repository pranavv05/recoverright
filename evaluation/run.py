import json
from evaluation.metrics import compute_metrics

def run_evaluation(held_out_path: str, api_key: str, rzp_client) -> dict:
    with open(held_out_path) as f:
        held_out = json.load(f)

    results = []
    for invoice in held_out:
        result = process_invoice_sync(invoice, api_key, rzp_client)
        results.append({
            "invoice_id": invoice["invoice_id"],
            "true_cause": invoice["true_cause"],
            "true_evidence_tier": invoice["evidence_tier"],
            "true_correct_action": invoice["correct_action"],
            "true_requires_human": invoice["requires_human"],
            "true_hard_stop": invoice["hard_stop"],
            "predicted_cause": result["diagnosis"]["diagnosed_cause"],
            "predicted_evidence_tier": result["diagnosis"]["evidence_tier"],
            "predicted_action": result["diagnosis"]["recommended_action"],
            "policy_decision": result["decision"],
            "policy_reasons": result["reasons"]
        })

    return compute_metrics(results)
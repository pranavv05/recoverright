import argparse
import json
import os

from dotenv import load_dotenv

from api.main import process_invoice
from evaluation.metrics import compute_metrics


def run_evaluation(held_out_path: str, api_key: str, rzp_client=None) -> dict:
    """Runs the full pipeline against a held-out set and computes accuracy/safety metrics.

    Invoices where diagnosis failed are excluded from the accuracy metrics and
    reported separately under "excluded_failures" — a failure is never silently
    folded into an accuracy score.
    """
    with open(held_out_path) as f:
        held_out = json.load(f)

    results = []
    failed = []
    for i, invoice in enumerate(held_out):
        print(f"Evaluating {i+1}/{len(held_out)}: {invoice['invoice_id']}")
        result = process_invoice(invoice, api_key, rzp_client)

        if result.get("status") in ("skipped", "diagnosis_failed"):
            failed.append({"invoice_id": result["invoice_id"], "status": result["status"]})
            continue

        diagnosis = result["diagnosis"]
        results.append({
            "invoice_id": invoice["invoice_id"],
            "true_cause": invoice["true_cause"],
            "true_evidence_tier": invoice["evidence_tier"],
            "true_correct_action": invoice["correct_action"],
            "true_requires_human": invoice["requires_human"],
            "true_hard_stop": invoice["hard_stop"],
            "predicted_cause": diagnosis.get("diagnosed_cause"),
            "predicted_evidence_tier": diagnosis.get("evidence_tier"),
            "predicted_action": diagnosis.get("recommended_action"),
            "policy_decision": result["policy_decision"],
            "policy_reasons": result["policy_reasons"]
        })

    metrics = compute_metrics(results)
    metrics["excluded_failures"] = failed
    return metrics


if __name__ == "__main__":
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Evaluate the recovery pipeline against a ground-truthed dataset."
    )
    parser.add_argument(
        "--dataset", default="data/held_out.json",
        help="Path to the ground-truthed JSON dataset (default: data/held_out.json)"
    )
    args = parser.parse_args()

    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_api_key_here":
        raise ValueError("NVIDIA_API_KEY not set in .env")

    final_metrics = run_evaluation(args.dataset, api_key, rzp_client=None)
    print(json.dumps(final_metrics, indent=2))

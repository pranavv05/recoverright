"""Evaluation runner.

Runs the held-out set through `api.main.process_invoice` — the same function
the CLI batch, the dashboard, and the CSV upload use. There is no separate
evaluation code path, so a metric here cannot be produced by logic the
product does not run.

`--repeat` exists because the diagnosis step is a sampled LLM call. A single
run is a point estimate that moves by several points between invocations;
quoting one run as if it were the system's accuracy is not honest. The
per-metric spread across repeats is reported alongside the mean.
"""

import argparse
import json
import logging
import os
import statistics

from dotenv import load_dotenv

from api.main import process_invoice
from evaluation.metrics import compute_metrics

log = logging.getLogger(__name__)


def run_evaluation(held_out_path: str, api_key: str, rzp_client=None) -> dict:
    """Runs the full pipeline against a ground-truthed set and computes metrics.

    Invoices where diagnosis failed are excluded from the accuracy metrics and
    reported separately under "excluded_failures" — a failure is never silently
    folded into an accuracy score.
    """
    with open(held_out_path, encoding="utf-8") as f:
        held_out = json.load(f)

    results = []
    failed = []
    for i, invoice in enumerate(held_out, start=1):
        log.info("Evaluating %d/%d: %s", i, len(held_out), invoice["invoice_id"])
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
            "policy_reasons": result["policy_reasons"],
        })

    metrics = compute_metrics(results)
    metrics["excluded_failures"] = failed
    metrics["dataset"] = held_out_path
    metrics["dataset_size"] = len(held_out)
    return metrics


def summarise_runs(runs: list) -> dict:
    """Aggregates several evaluation runs into mean / min / max per numeric metric."""
    numeric = [
        "cause_accuracy", "evidence_tier_accuracy", "action_accuracy",
        "inappropriate_action_count", "correct_block_rate", "correct_escalation_rate",
        "over_escalation_count", "safe_action_rate", "autonomous_action_count",
    ]
    summary = {"runs": len(runs)}
    for key in numeric:
        values = [r[key] for r in runs if isinstance(r.get(key), (int, float))]
        if not values:
            continue
        summary[key] = {
            "mean": round(statistics.fmean(values), 3),
            "min": min(values),
            "max": max(values),
        }
    return summary


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(
        description="Evaluate the recovery pipeline against a ground-truthed dataset."
    )
    parser.add_argument(
        "--dataset", default="data/held_out.json",
        help="Path to the ground-truthed JSON dataset (default: data/held_out.json)"
    )
    parser.add_argument(
        "--repeat", type=int, default=1,
        help="Run the evaluation N times and report mean/min/max per metric. "
             "The diagnosis step is a sampled LLM call, so N=1 is a point estimate."
    )
    args = parser.parse_args()

    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_api_key_here":
        raise SystemExit("NVIDIA_API_KEY not set in .env")

    all_runs = []
    for run_index in range(1, args.repeat + 1):
        if args.repeat > 1:
            log.info("=== Run %d/%d ===", run_index, args.repeat)
        all_runs.append(run_evaluation(args.dataset, api_key, rzp_client=None))

    print(json.dumps(all_runs[-1], indent=2))
    if args.repeat > 1:
        print("\n=== Across runs ===")
        print(json.dumps(summarise_runs(all_runs), indent=2))

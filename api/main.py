"""RecoverRight entry point — the ASGI app and the CLI batch runner.

This module is deliberately thin. It exists so that `uvicorn api.main:app`
and `python -m api.main` both work; everything they reach lives elsewhere:

    api/pipeline.py          process_invoice / process_invoice_async — the one
                             pipeline every entry point goes through, plus the
                             sequential and concurrent batch runners
    api/csv_import.py        merchant CSV → invoice dicts (Decimal money parsing)
    api/razorpay_import.py   Razorpay Invoices API → invoice dicts
    api/queries.py           the read layer: invoice rows, metrics, client health
    api/routes.py            the FastAPI surface — HTTP concerns only

If you are looking for what the system *decides*, none of it is here. Start at
`api/pipeline.py:process_invoice` and `policy/engine.py:evaluate`.
"""

from api.pipeline import build_rzp_client, run_batch_blocking
from api.routes import app  # noqa: F401  — re-exported for `uvicorn api.main:app`
from evaluation.health_scorer import score_client_health

__all__ = ["app", "build_rzp_client", "run_batch_blocking"]


def main() -> None:
    """Runs one batch from the command line and prints its decision breakdown."""
    import argparse

    parser = argparse.ArgumentParser(description="Run the RecoverRight batch pipeline.")
    parser.add_argument(
        "--dataset", default="data/seen.json",
        help="Path to the JSON invoice dataset to process (default: data/seen.json)"
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Use a real RazorpayClient (creates real test-mode payment links). Default is simulated."
    )
    args = parser.parse_args()

    rzp_client = build_rzp_client() if args.live else None
    results = run_batch_blocking(args.dataset, rzp_client=rzp_client)
    print(f"\nProcessed {len(results)} invoices")
    allow = sum(1 for r in results if r.get("policy_decision") == "ALLOW")
    block = sum(1 for r in results if r.get("policy_decision") == "BLOCK")
    human = sum(1 for r in results if r.get("policy_decision") == "REQUIRE_HUMAN")
    print(f"ALLOW: {allow} | BLOCK: {block} | REQUIRE_HUMAN: {human}")

    # Portfolio-level scoring, after every invoice in the batch has a
    # diagnosis and a policy decision on record.
    health_scores = score_client_health()
    print(f"\nScored {len(health_scores)} clients")
    for tier in ("critical", "at_risk", "watch", "healthy"):
        count = sum(1 for s in health_scores if s["risk_tier"] == tier)
        print(f"  {tier.upper()}: {count}")


if __name__ == "__main__":
    main()

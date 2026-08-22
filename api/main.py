import csv
import io
import json
import os
import sys
import time
import webbrowser
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from audit.trail import get_audit_trail, insert_invoice, insert_recovery, log_audit_event
from database.connection import get_connection
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


# ─────────────────────────────────────────────────────────────────────────
# CSV upload — lets merchants bring invoices from Tally / Zoho Books / Excel
# without any integration work. Maps CSV rows onto the same invoice schema
# JSON datasets use, then runs them through the exact same pipeline.
# ─────────────────────────────────────────────────────────────────────────

REQUIRED_CSV_COLUMNS = ["invoice_id", "client_name", "amount", "due_date", "days_overdue"]
SAMPLE_CSV_PATH = "data/sample_invoices.csv"


def _rupees_to_paise(raw: str, field_name: str, row_num: int) -> int:
    """Parses a rupee amount string and converts it to integer paise."""
    raw = (raw or "").strip().replace(",", "")
    if raw == "":
        return 0
    try:
        rupees = float(raw)
    except ValueError:
        raise ValueError(f"{field_name} in row {row_num} must be a number")
    return int(round(rupees * 100))


def _optional_int(raw: str, field_name: str, row_num: int, default: int = 0) -> int:
    """Parses an optional integer CSV field, falling back to `default` when blank."""
    raw = (raw or "").strip()
    if raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        raise ValueError(f"{field_name} in row {row_num} must be a whole number")


def _csv_row_to_invoice(row: dict, row_num: int) -> dict:
    """Maps one CSV row (already known to have the required columns) onto the internal invoice schema."""
    invoice_id = (row.get("invoice_id") or "").strip()
    if not invoice_id:
        raise ValueError(f"invoice_id is required in row {row_num}")

    client_name = (row.get("client_name") or "").strip()
    if not client_name:
        raise ValueError(f"client_name is required in row {row_num}")

    due_date = (row.get("due_date") or "").strip()
    if not due_date:
        raise ValueError(f"due_date is required in row {row_num}")

    amount_paise = _rupees_to_paise(row.get("amount"), "amount", row_num)
    if amount_paise <= 0:
        raise ValueError(f"amount in row {row_num} must be a number")

    days_overdue = _optional_int(row.get("days_overdue"), "days_overdue", row_num)
    partial_payment_paise = _rupees_to_paise(row.get("partial_payment"), "partial_payment", row_num)

    payment_history_raw = (row.get("payment_history") or "").strip()
    payment_history = (
        [v.strip() for v in payment_history_raw.split(",") if v.strip()]
        if payment_history_raw else []
    )

    return {
        "invoice_id": invoice_id,
        "client_name": client_name,
        "client_type": (row.get("client_type") or "").strip() or "standard",
        "amount_paise": amount_paise,
        "due_date": due_date,
        "days_overdue": days_overdue,
        "partial_payment_received_paise": partial_payment_paise,
        "payment_link_status": (row.get("payment_link_status") or "").strip() or "none",
        "payment_link_attempts": _optional_int(row.get("payment_link_attempts"), "payment_link_attempts", row_num),
        "reminders_sent": _optional_int(row.get("reminders_sent"), "reminders_sent", row_num),
        "reminders_opened": _optional_int(row.get("reminders_opened"), "reminders_opened", row_num),
        "client_notes": (row.get("client_notes") or "").strip(),
        "client_payment_history": payment_history,
        "account_value_tier": (row.get("account_value_tier") or "").strip() or "standard",
        "scenario_type": "csv_upload",
        "true_cause": "unknown",
        "hard_stop": False,
    }


def parse_csv_upload(raw_bytes: bytes) -> tuple:
    """Parses uploaded CSV bytes into pipeline-ready invoice dicts.

    Returns (invoices, warnings). Raises ValueError with a merchant-facing
    message on missing required columns or an unrecoverable per-row error.
    """
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV file must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV file is empty")

    fieldnames = [f.strip() for f in reader.fieldnames]
    missing = [c for c in REQUIRED_CSV_COLUMNS if c not in fieldnames]
    if missing:
        raise ValueError(
            "CSV must include: " + ", ".join(REQUIRED_CSV_COLUMNS) +
            ". Missing: " + ", ".join(missing)
        )

    invoices = []
    warnings = []
    seen_ids = set()

    for row_num, raw_row in enumerate(reader, start=2):  # header is row 1
        row = {
            (k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
            for k, v in raw_row.items()
        }
        invoice_id = row.get("invoice_id") or ""

        if invoice_id and invoice_id in seen_ids:
            warnings.append(f"Duplicate invoice_id '{invoice_id}' in row {row_num} — skipped")
            continue

        invoice = _csv_row_to_invoice(row, row_num)
        seen_ids.add(invoice["invoice_id"])
        invoices.append(invoice)

    return invoices, warnings


# ─────────────────────────────────────────────────────────────────────────
# FastAPI dashboard API — reads the same tables run_batch() writes to.
# Run with: uvicorn api.main:app --reload --port 8000
# ─────────────────────────────────────────────────────────────────────────

app = FastAPI(title="RecoverRight API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _latest_events_by_invoice(cur, event_type: str) -> dict:
    """Returns {invoice_id: row} for the most recent audit_log row of one event_type."""
    cur.execute(
        """
        SELECT DISTINCT ON (invoice_id) invoice_id, event_type, decision, reasons,
               allowed_actions, blocked_actions, diagnosis, execution_result, created_at
        FROM audit_log
        WHERE event_type = %s
        ORDER BY invoice_id, created_at DESC
        """,
        (event_type,)
    )
    return {row["invoice_id"]: row for row in cur.fetchall()}


def _load_invoice_rows() -> list:
    """Merges invoices with their latest diagnosis / policy_decision / execution audit events."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT invoice_id, client_name, amount_paise, outstanding_balance_paise,
                       days_overdue, account_value_tier, scenario_type, true_cause,
                       client_notes, processed_at
                FROM invoices
                ORDER BY processed_at ASC
                """
            )
            invoices = cur.fetchall()

            diagnoses = _latest_events_by_invoice(cur, "diagnosis")
            diagnosis_failures = _latest_events_by_invoice(cur, "diagnosis_failed")
            policy_decisions = _latest_events_by_invoice(cur, "policy_decision")

            cur.execute(
                """
                SELECT DISTINCT ON (invoice_id) invoice_id, event_type, execution_result, created_at
                FROM audit_log
                WHERE event_type IN ('execution', 'execution_simulated')
                ORDER BY invoice_id, created_at DESC
                """
            )
            executions = {row["invoice_id"]: row for row in cur.fetchall()}

    rows = []
    for inv in invoices:
        invoice_id = inv["invoice_id"]
        diagnosis_row = diagnoses.get(invoice_id)
        policy_row = policy_decisions.get(invoice_id)
        execution_row = executions.get(invoice_id)

        rows.append({
            "invoice_id": invoice_id,
            "client_name": inv["client_name"],
            "amount_paise": inv["amount_paise"],
            "outstanding_balance_paise": inv["outstanding_balance_paise"],
            "days_overdue": inv["days_overdue"],
            "account_value_tier": inv["account_value_tier"],
            "scenario_type": inv["scenario_type"],
            "client_notes": inv["client_notes"],
            "processed_at": inv["processed_at"],
            "diagnosis": diagnosis_row["diagnosis"] if diagnosis_row else None,
            "diagnosis_failed": invoice_id in diagnosis_failures,
            "policy_decision": policy_row["decision"] if policy_row else None,
            "policy_reasons": policy_row["reasons"] if policy_row else [],
            "execution_status": (
                (execution_row["execution_result"] or {}).get("status")
                if execution_row else None
            ),
        })
    return rows


@app.get("/api/invoices")
def api_list_invoices():
    """All processed invoices with their latest diagnosis, policy decision, and outcome."""
    return _load_invoice_rows()


@app.get("/api/invoices/{invoice_id}/audit")
def api_invoice_audit(invoice_id: str):
    """Full audit trail for one invoice, in chronological order."""
    return get_audit_trail(invoice_id)


@app.get("/api/metrics")
def api_metrics():
    """Aggregate metrics computed from the current state of the database."""
    rows = _load_invoice_rows()

    total = len(rows)
    allow_count = sum(1 for r in rows if r["policy_decision"] == "ALLOW")
    block_count = sum(1 for r in rows if r["policy_decision"] == "BLOCK")
    human_count = sum(1 for r in rows if r["policy_decision"] == "REQUIRE_HUMAN")
    requiring_more_data = sum(
        1 for r in rows
        if r["diagnosis_failed"] or (r["diagnosis"] or {}).get("evidence_tier") == "unknown"
    )
    inappropriate_actions = sum(
        1 for r in rows
        if r["policy_decision"] == "ALLOW"
        and (r["diagnosis"] or {}).get("suspicious_content_detected") is True
    )
    recoverable_paise = sum(
        r["outstanding_balance_paise"] for r in rows if r["policy_decision"] == "ALLOW"
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(amount_recovered_paise), 0) AS total "
                "FROM recoveries WHERE status = 'payment_verified'"
            )
            verified_recovered_paise = cur.fetchone()["total"]

            cur.execute(
                """
                SELECT COUNT(DISTINCT r.invoice_id) AS total
                FROM recoveries r
                WHERE r.status = 'payment_verified'
                AND EXISTS (
                    SELECT 1 FROM audit_log a
                    WHERE a.invoice_id = r.invoice_id
                    AND a.event_type IN ('api_retry', 'api_failure')
                )
                """
            )
            api_failures_recovered = cur.fetchone()["total"]

    return {
        "total_processed": total,
        "allow_count": allow_count,
        "block_count": block_count,
        "require_human_count": human_count,
        "verified_recovered_paise": verified_recovered_paise,
        "recoverable_paise": recoverable_paise,
        "requiring_more_data_count": requiring_more_data,
        "escalated_to_human_count": human_count,
        "inappropriate_actions_count": inappropriate_actions,
        "api_failures_recovered_count": api_failures_recovered,
    }


@app.post("/api/run-batch")
def api_run_batch():
    """Triggers the batch pipeline against data/seen.json, simulated (no real Razorpay calls)."""
    results = run_batch("data/seen.json", limit=None, rzp_client=None)
    return {
        "processed": len(results),
        "allow": sum(1 for r in results if r.get("policy_decision") == "ALLOW"),
        "block": sum(1 for r in results if r.get("policy_decision") == "BLOCK"),
        "require_human": sum(1 for r in results if r.get("policy_decision") == "REQUIRE_HUMAN"),
    }


@app.get("/api/sample-csv")
def api_sample_csv():
    """Downloads the sample invoice CSV so merchants can see the expected upload format."""
    if not os.path.exists(SAMPLE_CSV_PATH):
        raise HTTPException(status_code=404, detail="Sample CSV not found")
    return FileResponse(
        SAMPLE_CSV_PATH,
        media_type="text/csv",
        filename="sample_invoices.csv"
    )


@app.post("/api/upload-csv")
async def api_upload_csv(file: UploadFile = File(...)):
    """
    Accepts a merchant's invoice CSV (Tally / Zoho Books / Excel export), maps
    it onto the internal invoice schema, and runs every row through the exact
    same diagnose → policy → audit pipeline as JSON datasets. Always simulated
    (no real Razorpay calls) — uploads never create live payment links.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a CSV file")

    raw_bytes = await file.read()

    try:
        invoices, warnings = parse_csv_upload(raw_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not invoices:
        raise HTTPException(status_code=400, detail="No valid invoice rows found in CSV")

    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_api_key_here":
        raise HTTPException(status_code=500, detail="NVIDIA_API_KEY not configured on server")

    invoice_ids = [invoice["invoice_id"] for invoice in invoices]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT invoice_id FROM invoices WHERE invoice_id = ANY(%s)",
                (invoice_ids,)
            )
            existing_ids = {row["invoice_id"] for row in cur.fetchall()}

    new_invoices = []
    for invoice in invoices:
        if invoice["invoice_id"] in existing_ids:
            warnings.append(
                f"invoice_id {invoice['invoice_id']} already exists in database — skipped"
            )
        else:
            new_invoices.append(invoice)

    results = []
    for invoice in new_invoices:
        result = process_invoice(invoice, api_key, rzp_client=None)
        results.append(result)

    return {
        "processed": len(results),
        "allow": sum(1 for r in results if r.get("policy_decision") == "ALLOW"),
        "block": sum(1 for r in results if r.get("policy_decision") == "BLOCK"),
        "require_human": sum(1 for r in results if r.get("policy_decision") == "REQUIRE_HUMAN"),
        "warnings": warnings,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the RecoverRight batch pipeline.")
    parser.add_argument(
        "--live", action="store_true",
        help="Use a real RazorpayClient (creates real test-mode payment links). Default is simulated."
    )
    args = parser.parse_args()

    rzp_client = build_rzp_client() if args.live else None
    results = run_batch("data/seen.json", limit=None, rzp_client=rzp_client)
    print(f"\nProcessed {len(results)} invoices")
    allow = sum(1 for r in results if r.get("policy_decision") == "ALLOW")
    block = sum(1 for r in results if r.get("policy_decision") == "BLOCK")
    human = sum(1 for r in results if r.get("policy_decision") == "REQUIRE_HUMAN")
    print(f"ALLOW: {allow} | BLOCK: {block} | REQUIRE_HUMAN: {human}")
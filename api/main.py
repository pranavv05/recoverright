"""RecoverRight orchestrator and dashboard API.

`process_invoice` is the single pipeline every entry point goes through —
the CLI batch, the dashboard's Run Batch button, the CSV upload, and the
held-out evaluation. There is deliberately no second code path that can
reach the executor.

`process_invoice_async` is that same pipeline with the waiting taken out of
the way: same stages in the same order, same audit events, same policy engine,
same workers. It exists because the batch was spending nearly all of its
five minutes blocked on the LLM one invoice at a time. What changes is how
many invoices are in flight at once — never what happens to any one of them.
"""

import asyncio
import csv
import io
import json
import logging
import os
import sys
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from audit.trail import (
    get_audit_trail,
    insert_invoice,
    insert_invoice_async,
    log_audit_event,
    log_audit_event_async,
)
from database.connection import (
    close_pool,
    get_connection,
    loop_supports_async_pg,
    run_async,
)
from policy.engine import PolicyInput, evaluate
from diagnosis.diagnoser import close_async_clients, diagnose, diagnose_async
from evaluation.analytics import compute_batch_analytics
from evaluation.health_scorer import score_client_health
from execution.razorpay_client import (
    AuthenticationError,
    RateLimitError,
    RazorpayClient,
    ServerError,
    invoice_due_date,
)
from execution.workers import Orchestrator

load_dotenv()

log = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
# The HTTP clients log one INFO line per request, which drowns out the
# per-invoice pipeline output during a batch.
for noisy in ("httpx", "httpx2", "httpcore", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def build_rzp_client():
    """Instantiates a RazorpayClient from env vars, or None if credentials aren't set."""
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None
    return RazorpayClient(key_id=key_id, key_secret=key_secret)


# One orchestrator for the whole process. Constructed at import time rather
# than per invoice: the five workers it holds are stateless, and a batch of 500
# invoices should not allocate 2,500 worker objects to route 500 decisions.
_orchestrator = Orchestrator()


def load_dataset(path: str) -> list:
    """Loads a JSON invoice dataset from disk."""
    with open(path) as f:
        return json.load(f)

def process_invoice(invoice: dict, api_key: str, rzp_client=None) -> dict:
    """Runs one invoice through the full pipeline: insert → diagnose → policy → act → audit.

    Every terminal state writes an audit event, including the ones where the
    system deliberately did nothing. A BLOCK or REQUIRE_HUMAN that produced no
    outcome row would leave the trail unable to distinguish "routed to a human"
    from "the batch crashed here".
    """
    invoice_id = invoice["invoice_id"]

    insert_invoice(invoice)

    outstanding = invoice["amount_paise"] - invoice["partial_payment_received_paise"]
    if outstanding <= 0:
        log_audit_event(invoice_id, "skipped", {
            "reason": "outstanding_balance_zero_or_negative",
            "execution": {"status": "skipped", "outstanding_balance_paise": outstanding},
        })
        return {"invoice_id": invoice_id, "status": "skipped"}

    try:
        diagnosis = diagnose(invoice, api_key)
    except Exception as e:
        log.exception("[%s] diagnosis failed", invoice_id)
        log_audit_event(invoice_id, "diagnosis_failed", {
            "error": str(e),
            "execution": {"status": "diagnosis_failed", "error": str(e)},
        })
        return {"invoice_id": invoice_id, "status": "diagnosis_failed", "error": str(e)}

    # The ReAct reasoning chain and the signals it was built from are for the
    # audit trail only — the policy engine gets the same clean diagnosis dict
    # it always has, never these two internal keys.
    reasoning_trace = diagnosis.pop("_reasoning_trace", {})
    signals = diagnosis.pop("_signals", {})

    log_audit_event(invoice_id, "reasoning_trace", {
        "diagnosis": {
            "signals": signals,
            "thought_1_engagement": reasoning_trace.get("thought_1_engagement"),
            "thought_2_content": reasoning_trace.get("thought_2_content"),
            "thought_3_payment_history": reasoning_trace.get("thought_3_payment_history"),
            "thought_4_evidence_quality": reasoning_trace.get("thought_4_evidence_quality"),
            "thought_5_cause_and_action": reasoning_trace.get("thought_5_cause_and_action"),
        },
    })
    log_audit_event(invoice_id, "diagnosis", {"diagnosis": diagnosis})

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
        reminders_sent=invoice["reminders_sent"],
    )
    decision = evaluate(policy_input)

    log_audit_event(invoice_id, "policy_decision", {
        "decision": decision.decision,
        "reasons": decision.reasons,
        "allowed_actions": decision.allowed_actions,
        "blocked_actions": decision.blocked_actions,
        "diagnosis": diagnosis,
    })

    # Stage 5: Execute via orchestrator.
    #
    # Every terminal state is now a worker's responsibility, including the ones
    # where the system deliberately did nothing. Routing is deterministic and
    # lives in execution/workers.py; there is no execution branching left here,
    # so no second code path can reach a customer.
    execution_result = _orchestrator.execute(
        invoice=invoice,
        diagnosis=diagnosis,
        decision=decision,
        rzp_client=rzp_client,
    )

    # Stage 6: Log the execution result. This is the terminal record the
    # dashboard reads for an invoice's outcome.
    log_audit_event(invoice_id, "execution_complete", {
        "worker": execution_result.get("worker"),
        "status": execution_result.get("status"),
        "execution": execution_result,
    })

    return {
        "invoice_id": invoice_id,
        "diagnosis": diagnosis,
        "policy_decision": decision.decision,
        "policy_reasons": decision.reasons,
        "worker": execution_result.get("worker"),
        "execution": execution_result,
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
    for i, invoice in enumerate(invoices, start=1):
        result = process_invoice(invoice, api_key, rzp_client)
        results.append(result)
        log.info(
            "%d/%d %s (%s) -> %s | %s",
            i, len(invoices), invoice["invoice_id"], invoice["scenario_type"],
            result.get("policy_decision", result.get("status", "n/a")),
            (result.get("diagnosis") or {}).get("diagnosed_cause", "n/a"),
        )

    return results


# ─────────────────────────────────────────────────────────────────────────
# Parallel batch path
#
# The sequential runner above spends its wall-clock time waiting: two LLM
# calls per invoice, one invoice at a time. Running the invoices concurrently
# is safe because each one is independent — no invoice's diagnosis, policy
# decision or execution reads anything another invoice wrote — and because
# the two shared resources are bounded: diagnosis/diagnoser.py caps in-flight
# LLM requests with a semaphore, and database/connection.py caps concurrent
# writes with a connection pool.
#
# The synchronous functions above are untouched and remain the path used by
# the evaluation runner, the demo scripts and the CSV upload.
# ─────────────────────────────────────────────────────────────────────────


async def process_invoice_async(invoice: dict, api_key: str, rzp_client=None) -> dict:
    """Async twin of process_invoice(). Same stages, same audit trail, same result.

    Every difference is mechanical: awaited audit writes, awaited diagnosis,
    awaited worker. The policy engine is the one stage that stays synchronous
    — it is pure CPU-bound Python over a fixed record, so it runs in the
    default executor rather than blocking the event loop for every other
    invoice while it evaluates.
    """
    invoice_id = invoice["invoice_id"]

    await insert_invoice_async(invoice)

    outstanding = invoice["amount_paise"] - invoice["partial_payment_received_paise"]
    if outstanding <= 0:
        await log_audit_event_async(invoice_id, "skipped", {
            "reason": "outstanding_balance_zero_or_negative",
            "execution": {"status": "skipped", "outstanding_balance_paise": outstanding},
        })
        return {"invoice_id": invoice_id, "status": "skipped"}

    try:
        diagnosis = await diagnose_async(invoice, api_key)
    except Exception as e:
        log.exception("[%s] diagnosis failed", invoice_id)
        await log_audit_event_async(invoice_id, "diagnosis_failed", {
            "error": str(e),
            "execution": {"status": "diagnosis_failed", "error": str(e)},
        })
        return {"invoice_id": invoice_id, "status": "diagnosis_failed", "error": str(e)}

    reasoning_trace = diagnosis.pop("_reasoning_trace", {})
    signals = diagnosis.pop("_signals", {})

    await log_audit_event_async(invoice_id, "reasoning_trace", {
        "diagnosis": {
            "signals": signals,
            "thought_1_engagement": reasoning_trace.get("thought_1_engagement"),
            "thought_2_content": reasoning_trace.get("thought_2_content"),
            "thought_3_payment_history": reasoning_trace.get("thought_3_payment_history"),
            "thought_4_evidence_quality": reasoning_trace.get("thought_4_evidence_quality"),
            "thought_5_cause_and_action": reasoning_trace.get("thought_5_cause_and_action"),
        },
    })
    await log_audit_event_async(invoice_id, "diagnosis", {"diagnosis": diagnosis})

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
        reminders_sent=invoice["reminders_sent"],
    )
    decision = await asyncio.get_running_loop().run_in_executor(
        None, evaluate, policy_input
    )

    await log_audit_event_async(invoice_id, "policy_decision", {
        "decision": decision.decision,
        "reasons": decision.reasons,
        "allowed_actions": decision.allowed_actions,
        "blocked_actions": decision.blocked_actions,
        "diagnosis": diagnosis,
    })

    execution_result = await _orchestrator.execute_async(
        invoice=invoice,
        diagnosis=diagnosis,
        decision=decision,
        rzp_client=rzp_client,
    )

    await log_audit_event_async(invoice_id, "execution_complete", {
        "worker": execution_result.get("worker"),
        "status": execution_result.get("status"),
        "execution": execution_result,
    })

    return {
        "invoice_id": invoice_id,
        "diagnosis": diagnosis,
        "policy_decision": decision.decision,
        "policy_reasons": decision.reasons,
        "worker": execution_result.get("worker"),
        "execution": execution_result,
    }


async def _process_and_report_async(position: int, total: int, invoice: dict,
                                    api_key: str, rzp_client=None) -> dict:
    """Processes one invoice and logs its outcome the moment it lands.

    `position` is the invoice's index in the dataset, not its finishing order,
    so a line in the log can still be matched to a line in the dataset even
    though the batch no longer completes in order.
    """
    result = await process_invoice_async(invoice, api_key, rzp_client)
    log.info(
        "%d/%d %s (%s) -> %s | %s",
        position, total, invoice["invoice_id"], invoice["scenario_type"],
        result.get("policy_decision", result.get("status", "n/a")),
        (result.get("diagnosis") or {}).get("diagnosed_cause", "n/a"),
    )
    return result


async def run_batch_async(dataset_path: str, limit: int = None, rzp_client=None) -> list:
    """Processes every invoice in the dataset concurrently, returning one result each.

    `return_exceptions=True` is the whole point: an invoice that raises past
    its own error handling must not cancel the other fifty-one. Such an
    invoice is logged with its traceback and takes its place in the returned
    list as an `error` record, so the list always lines up with the dataset.
    """
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_api_key_here":
        raise ValueError("NVIDIA_API_KEY not set in .env")

    invoices = load_dataset(dataset_path)
    if limit:
        invoices = invoices[:limit]

    total = len(invoices)
    settled = await asyncio.gather(
        *(
            _process_and_report_async(i, total, invoice, api_key, rzp_client)
            for i, invoice in enumerate(invoices, start=1)
        ),
        return_exceptions=True,
    )

    results = []
    for invoice, outcome in zip(invoices, settled):
        if isinstance(outcome, asyncio.CancelledError):
            # The batch itself was cancelled. Swallowing that would turn a
            # deliberate shutdown into a silent partial run.
            raise outcome
        if isinstance(outcome, BaseException):
            log.error(
                "[%s] invoice failed outside its own error handling: %s",
                invoice["invoice_id"], outcome,
                exc_info=(type(outcome), outcome, outcome.__traceback__),
            )
            results.append({
                "invoice_id": invoice["invoice_id"],
                "status": "error",
                "error": f"{type(outcome).__name__}: {outcome}",
            })
        else:
            results.append(outcome)

    return results


async def _run_batch_with_cleanup(dataset_path: str, rzp_client=None) -> list:
    """Runs the parallel batch and releases everything bound to this event loop.

    The connection pool and the AsyncOpenAI clients hold sockets that belong
    to the running loop; leaving them open past the end of the loop leaks
    them. `finally`, so a batch that raises still cleans up.
    """
    try:
        return await run_batch_async(dataset_path, rzp_client=rzp_client)
    finally:
        await close_async_clients()
        await close_pool()


def run_batch_blocking(dataset_path: str, rzp_client=None) -> list:
    """Runs the parallel batch to completion from synchronous code."""
    return run_async(_run_batch_with_cleanup(dataset_path, rzp_client))


async def _dispatch_batch_async(dataset_path: str, rzp_client=None) -> list:
    """Runs the parallel batch on an event loop the connection pool can use.

    On Windows uvicorn serves on a ProactorEventLoop, which psycopg's async
    connections cannot run on. Rather than dictating how the server must be
    started, the batch is handed to a worker thread with its own selector
    loop; everywhere else it runs directly on the serving loop.
    """
    if loop_supports_async_pg(asyncio.get_running_loop()):
        return await _run_batch_with_cleanup(dataset_path, rzp_client)
    return await asyncio.to_thread(run_batch_blocking, dataset_path, rzp_client)


# ─────────────────────────────────────────────────────────────────────────
# CSV upload — lets merchants bring invoices from Tally / Zoho Books / Excel
# without any integration work. Maps CSV rows onto the same invoice schema
# JSON datasets use, then runs them through the exact same pipeline.
# ─────────────────────────────────────────────────────────────────────────

REQUIRED_CSV_COLUMNS = ["invoice_id", "client_name", "amount", "due_date", "days_overdue"]
SAMPLE_CSV_PATH = "data/sample_invoices.csv"


MAX_CSV_ROWS = 500
# Sanity ceiling on a single invoice: Rs 100 crore. Anything above this in a
# merchant CSV is a data-entry error (a stray exponent, a paise figure pasted
# into a rupee column), not a real receivable.
MAX_INVOICE_PAISE = 100_00_00_000 * 100


def _rupees_to_paise(raw: str, field_name: str, row_num: int, warnings: list = None) -> int:
    """Parses a rupee amount string into integer paise using exact decimal arithmetic.

    Decimal, not float: `float("0.07") * 100` is 7.000000000000001, and the
    error compounds across a book of invoices. Money never touches binary
    floating point anywhere in this codebase.

    Rejects negatives and non-finite values (inf/-inf/nan) outright — those
    are not roundable, they're wrong. Sub-paise precision (a third decimal
    place) is different: it's usually a merchant typo, not garbage, so it is
    rounded with `ROUND_HALF_UP` and reported as a warning rather than failing
    the whole row.
    """
    text = (raw or "").strip().replace(",", "").replace("₹", "")
    if text == "":
        return 0

    try:
        rupees = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{field_name} in row {row_num} must be a number, got {text!r}")

    if not rupees.is_finite():
        raise ValueError(f"{field_name} in row {row_num} is not a valid number, got {text!r}")
    if rupees < 0:
        raise ValueError(f"{field_name} in row {row_num} must be positive, got {text!r}")

    exact_paise = rupees * 100
    rounded_paise = exact_paise.to_integral_value(rounding=ROUND_HALF_UP)
    if exact_paise != rounded_paise:
        message = (
            f"{field_name} in row {row_num} ({text!r}) has sub-paise precision — "
            f"rounded to {int(rounded_paise)} paise"
        )
        if warnings is not None:
            warnings.append(message)

    paise = int(rounded_paise)
    if paise > MAX_INVOICE_PAISE:
        raise ValueError(
            f"{field_name} in row {row_num} exceeds maximum invoice value (Rs 100 crore cap)"
        )
    return paise


def _optional_int(raw: str, field_name: str, row_num: int, default: int = 0) -> int:
    """Parses an optional non-negative integer CSV field, falling back to `default` when blank."""
    text = (raw or "").strip()
    if text == "":
        return default
    try:
        value = int(text)
    except ValueError:
        raise ValueError(
            f"{field_name} in row {row_num} must be a whole number, got {text!r}"
        )
    if value < 0:
        raise ValueError(f"{field_name} in row {row_num} cannot be negative, got {text!r}")
    return value


def _csv_row_to_invoice(row: dict, row_num: int, warnings: list) -> dict:
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

    amount_paise = _rupees_to_paise(row.get("amount"), "amount", row_num, warnings)
    if amount_paise == 0:
        raise ValueError(f"Amount in row {row_num} must be greater than zero")

    days_overdue = _optional_int(row.get("days_overdue"), "days_overdue", row_num)
    partial_payment_paise = _rupees_to_paise(row.get("partial_payment"), "partial_payment", row_num, warnings)
    if partial_payment_paise > amount_paise:
        raise ValueError(
            f"Partial payment in row {row_num} cannot exceed invoice amount"
        )

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
        if len(invoices) >= MAX_CSV_ROWS:
            warnings.append(
                f"Only the first {MAX_CSV_ROWS} rows were processed — this upload runs one "
                "LLM diagnosis per invoice synchronously. Split larger books into batches."
            )
            break

        row = {
            (k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
            for k, v in raw_row.items()
        }
        invoice_id = row.get("invoice_id") or ""

        if invoice_id and invoice_id in seen_ids:
            warnings.append(f"Duplicate invoice_id '{invoice_id}' in row {row_num} — skipped")
            continue

        invoice = _csv_row_to_invoice(row, row_num, warnings)
        seen_ids.add(invoice["invoice_id"])
        invoices.append(invoice)

    return invoices, warnings


# ─────────────────────────────────────────────────────────────────────────
# Razorpay native sync — pulls overdue invoices straight from a merchant's
# own Razorpay account via the Invoices API, instead of asking them to export
# a file first. Razorpay already stores money as integer paise, a due_date
# as a Unix timestamp, and a customer sub-object, so this mapping is a
# straight field rename rather than the rupee/CSV parsing CSV rows go
# through. Fields the CSV schema fills from merchant-supplied columns —
# reminder counts, payment history — don't exist on a single Razorpay
# invoice record, so they default to empty/zero; diagnosis and policy both
# already treat those as valid (if uninformative) inputs.
# ─────────────────────────────────────────────────────────────────────────


def _razorpay_invoice_to_internal(raw: dict) -> dict:
    """Maps one raw Razorpay invoice (from fetch_overdue_invoices) onto the internal schema."""
    razorpay_id = raw.get("id")
    if not razorpay_id:
        raise ValueError("Razorpay invoice has no 'id' — skipped")

    due_date = invoice_due_date(raw)
    if due_date is None:
        raise ValueError(f"Razorpay invoice {razorpay_id} has no valid due_date — skipped")

    amount_paise = raw.get("amount")
    if not isinstance(amount_paise, int) or isinstance(amount_paise, bool) or amount_paise <= 0:
        raise ValueError(f"Razorpay invoice {razorpay_id} has an invalid amount — skipped")

    amount_paid_paise = raw.get("amount_paid")
    if not isinstance(amount_paid_paise, int) or isinstance(amount_paid_paise, bool):
        amount_paid_paise = 0
    if amount_paid_paise > amount_paise:
        raise ValueError(
            f"Razorpay invoice {razorpay_id} has amount_paid greater than amount — skipped"
        )

    days_overdue = max(0, (int(time.time()) - due_date) // 86400)

    status = raw.get("status")
    short_url = raw.get("short_url")
    if short_url and status == "issued":
        payment_link_status = "active"
    elif short_url:
        payment_link_status = "expired"
    else:
        payment_link_status = "none"

    notes = raw.get("notes")
    client_notes = " ".join(str(v) for v in notes.values()) if isinstance(notes, dict) else ""

    # Documented field is `customer`; live accounts return `customer_details`
    # instead — same API-generation drift as due_date/expire_by above.
    customer = raw.get("customer") or raw.get("customer_details") or {}
    client_name = (customer.get("name") or "").strip() or "Unknown Razorpay Customer"

    return {
        "invoice_id": f"RZP-{razorpay_id}",
        "client_name": client_name,
        "client_type": "razorpay_merchant_client",
        "amount_paise": amount_paise,
        "partial_payment_received_paise": amount_paid_paise,
        "days_overdue": days_overdue,
        "payment_link_status": payment_link_status,
        "payment_link_attempts": 0,
        "reminders_sent": 0,
        "reminders_opened": 0,
        "client_notes": client_notes,
        "client_payment_history": [],
        "account_value_tier": "standard",
        "scenario_type": "razorpay_sync",
        "true_cause": "unknown",
        "hard_stop": False,
    }


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
        SELECT DISTINCT ON (invoice_id) id, invoice_id, event_type, decision, reasons,
               allowed_actions, blocked_actions, diagnosis, execution_result, created_at
        FROM audit_log
        WHERE event_type = %s
        ORDER BY invoice_id, created_at DESC, id DESC
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
                -- 'execution_complete' is the terminal record every invoice
                -- gets under the orchestrator-workers pipeline. The other three
                -- are pre-refactor event types, still emitted by the demo
                -- scripts, kept so an existing audit_log keeps rendering.
                WHERE event_type IN ('execution_complete', 'execution',
                                     'execution_simulated', 'no_autonomous_action')
                ORDER BY invoice_id, created_at DESC, id DESC
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
    # An ALLOW on an invoice the model itself flagged as containing suspicious
    # content — the LLM's own self-reported `suspicious_content_detected` flag
    # from its diagnosis output. This is a *live-operation* signal, and it is
    # NOT the same measurement as the ground-truth `inappropriate_action_count`
    # in evaluation/metrics.py, which counts ALLOWs against labelled data the
    # dashboard has no access to. Named `suspicious_content_detected_count` so
    # the two are never mistaken for the same metric.
    suspicious_content_detected = sum(
        1 for r in rows
        if r["policy_decision"] == "ALLOW"
        and (r["diagnosis"] or {}).get("suspicious_content_detected") is True
    )
    # Only actions that actually try to collect money count toward the
    # recoverable denominator. An ALLOW whose action is `wait` or
    # `gather_information` is a deliberate decision not to collect yet, and
    # including it inflated the figure on the dashboard.
    COLLECTING_ACTIONS = {"create_payment_link", "send_reminder"}
    recoverable_paise = sum(
        r["outstanding_balance_paise"] for r in rows
        if r["policy_decision"] == "ALLOW"
        and (r["diagnosis"] or {}).get("recommended_action") in COLLECTING_ACTIONS
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
        "suspicious_content_detected_count": suspicious_content_detected,
        "api_failures_recovered_count": api_failures_recovered,
    }


_RISK_TIER_ORDER = {"critical": 0, "at_risk": 1, "watch": 2, "healthy": 3}


@app.get("/api/client-health")
def api_client_health():
    """All client health scores, most severe risk tier first.

    Reads whatever evaluation.health_scorer last wrote to
    client_health_scores — populated after a batch run, not computed here.
    Money fields are rupees (paise / 100), everywhere else in this table is
    paise internally.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT client_name, risk_tier, pattern_description, invoice_count,
                       total_outstanding_paise, dominant_cause, promise_broken_count,
                       dispute_count, days_overdue_avg, payment_trend, capital_flag,
                       capital_flag_reason, scored_at
                FROM client_health_scores
                """
            )
            rows = cur.fetchall()

    rows.sort(key=lambda r: (_RISK_TIER_ORDER.get(r["risk_tier"], 99), -r["total_outstanding_paise"]))

    return [
        {
            "client_name": r["client_name"],
            "risk_tier": r["risk_tier"],
            "pattern_description": r["pattern_description"],
            "invoice_count": r["invoice_count"],
            "total_outstanding_rupees": r["total_outstanding_paise"] / 100,
            "dominant_cause": r["dominant_cause"],
            "promise_broken_count": r["promise_broken_count"],
            "dispute_count": r["dispute_count"],
            "days_overdue_avg": r["days_overdue_avg"],
            "payment_trend": r["payment_trend"],
            "capital_flag": r["capital_flag"],
            "capital_flag_reason": r["capital_flag_reason"],
            "scored_at": r["scored_at"],
        }
        for r in rows
    ]


@app.get("/api/analytics")
def api_analytics():
    """Batch-level analytics: recovery rate by scenario, most-fired policy
    rules, multi-invoice clients, and average overdue by diagnosed cause.

    Read-only — computed entirely in SQL/Python from invoices and audit_log,
    same tables /api/invoices and /api/client-health already read. Nothing
    here is written back.
    """
    return compute_batch_analytics()


@app.post("/api/run-batch")
async def api_run_batch():
    """Triggers the batch pipeline against data/seen.json, simulated (no real Razorpay calls).

    Declared `async def`: the batch is itself concurrent, so it belongs on the
    event loop rather than in Starlette's threadpool. Counted the same way as
    the sequential runner — an invoice that failed has no `policy_decision`
    and so contributes to none of the three totals.
    """
    results = await _dispatch_batch_async("data/seen.json", rzp_client=None)

    # Portfolio-level scoring runs once, after every invoice in the batch has
    # its diagnosis and policy decision on record — never mid-batch, and
    # never in a way that can change what was just decided above.
    await asyncio.to_thread(score_client_health)

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
def api_upload_csv(file: UploadFile = File(...)):
    """
    Accepts a merchant's invoice CSV (Tally / Zoho Books / Excel export), maps
    it onto the internal invoice schema, and runs every row through the exact
    same diagnose → policy → audit pipeline as JSON datasets. Always simulated
    (no real Razorpay calls) — uploads never create live payment links.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Please upload a CSV file")

    # Declared `def`, not `async def`, on purpose: this handler runs one
    # synchronous LLM call per row. As a coroutine it would block the event
    # loop and freeze every other request for the duration of the upload.
    # Starlette runs sync handlers in a threadpool instead.
    raw_bytes = file.file.read()

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


@app.post("/api/sync-razorpay")
def api_sync_razorpay(days_overdue_min: int = 1):
    """
    Pulls overdue invoices directly from the merchant's own Razorpay account
    (Invoices API), maps them onto the internal schema, and runs every one
    through the exact same diagnose → policy → audit pipeline as CSV/JSON
    invoices. Always simulated (no real Razorpay calls) — a sync never
    creates live payment links, since these invoices already have one.

    Declared `def`, not `async def`, for the same reason as the CSV upload
    handler: one synchronous LLM call per invoice would otherwise block the
    event loop for the duration of the sync.
    """
    rzp_client = build_rzp_client()
    if rzp_client is None:
        raise HTTPException(
            status_code=401,
            detail="Razorpay credentials not configured — set RAZORPAY_KEY_ID "
                   "and RAZORPAY_KEY_SECRET"
        )

    try:
        raw_invoices = rzp_client.fetch_overdue_invoices(days_overdue_min=days_overdue_min)
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except (RateLimitError, ServerError, ValueError) as e:
        raise HTTPException(
            status_code=502, detail=f"Failed to fetch invoices from Razorpay: {e}"
        )

    if not raw_invoices:
        return {
            "source": "razorpay_api",
            "invoices_fetched": 0,
            "processed": 0,
            "allow": 0,
            "block": 0,
            "require_human": 0,
            "warnings": [],
            "message": "No overdue invoices found in your Razorpay account",
        }

    warnings = []
    invoices = []
    for raw in raw_invoices:
        try:
            invoices.append(_razorpay_invoice_to_internal(raw))
        except ValueError as e:
            warnings.append(str(e))

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
        "source": "razorpay_api",
        "invoices_fetched": len(raw_invoices),
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

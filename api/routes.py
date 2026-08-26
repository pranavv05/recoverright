"""The FastAPI surface — HTTP concerns only.

Every handler here does three things at most: validate what arrived, call one
function in `api.pipeline`, `api.queries`, `api.csv_import`,
`api.razorpay_import`, `api.escalations` or `api.webhooks`, and turn a domain
error into an HTTP status. There is no invoice logic in this module, and
almost no SQL — that is the point of it being separate from the six modules
it calls.

Run with: uvicorn api.main:app --reload --port 8000
"""

import asyncio
import logging
import os
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api.csv_import import SAMPLE_CSV_PATH, parse_csv_upload
from api.escalations import (
    ActionNotPermitted,
    EscalationError,
    EscalationNotFound,
    EscalationStale,
    escalation_summary,
    get_escalation,
    load_open_escalations,
    load_resolution_history,
    resolve_escalation,
)
from api.pipeline import (
    _dispatch_batch_async,
    build_rzp_client,
    process_invoice,
)
from api.queries import (
    _load_invoice_rows,
    compute_metrics,
    load_client_health,
)
from api.razorpay_import import _razorpay_invoice_to_internal
from api.webhooks import (
    InvalidSignature,
    WebhookNotConfigured,
    handle_razorpay_webhook,
)
from audit.trail import get_audit_trail
from database.connection import get_connection
from evaluation.analytics import compute_batch_analytics
from evaluation.health_scorer import score_client_health
from execution.razorpay_client import AuthenticationError, RateLimitError, ServerError

log = logging.getLogger(__name__)

app = FastAPI(title="RecoverRight API")

# The dashboard's dev origin by default; override with a comma-separated
# CORS_ALLOWED_ORIGINS for any other deployment. Deliberately an explicit
# allowlist rather than "*" — this API creates payment links, and a wildcard
# origin on a credentialed endpoint is how a CSRF gets written.
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _require_api_key() -> str:
    """Returns the configured NVIDIA key, or raises the 500 the dashboard shows.

    Every ingestion route runs one LLM call per invoice, so all three fail the
    same way and with the same message when the server has no key.
    """
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_api_key_here":
        raise HTTPException(status_code=500, detail="NVIDIA_API_KEY not configured on server")
    return api_key


def _existing_invoice_ids(invoice_ids: list) -> set:
    """Which of these invoice_ids are already in the database."""
    if not invoice_ids:
        return set()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT invoice_id FROM invoices WHERE invoice_id = ANY(%s)",
                (invoice_ids,)
            )
            return {row["invoice_id"] for row in cur.fetchall()}


def _split_already_processed(invoices: list, warnings: list) -> list:
    """Drops invoices already in the database, appending a warning for each.

    Re-running an invoice would append a second diagnosis and a second policy
    decision to its trail, and the dashboard reads the latest of each — so a
    duplicate upload would silently rewrite a recorded decision. Both
    ingestion routes refuse instead, and say which rows they refused.
    """
    existing = _existing_invoice_ids([invoice["invoice_id"] for invoice in invoices])
    new_invoices = []
    for invoice in invoices:
        if invoice["invoice_id"] in existing:
            warnings.append(
                f"invoice_id {invoice['invoice_id']} already exists in database — skipped"
            )
        else:
            new_invoices.append(invoice)
    return new_invoices


def _decision_counts(results: list) -> dict:
    """The ALLOW / BLOCK / REQUIRE_HUMAN breakdown every ingestion route returns.

    An invoice that failed has no `policy_decision` and so contributes to none
    of the three totals — the same way the sequential runner counts.
    """
    return {
        "processed": len(results),
        "allow": sum(1 for r in results if r.get("policy_decision") == "ALLOW"),
        "block": sum(1 for r in results if r.get("policy_decision") == "BLOCK"),
        "require_human": sum(1 for r in results if r.get("policy_decision") == "REQUIRE_HUMAN"),
    }


# ─────────────────────────────────────────────────────────────────────────
# Read endpoints — every one of these is a query in api.queries
# ─────────────────────────────────────────────────────────────────────────


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
    return compute_metrics()


@app.get("/api/client-health")
def api_client_health():
    """All client health scores, most severe risk tier first."""
    return load_client_health()


@app.get("/api/analytics")
def api_analytics():
    """Batch-level analytics: recovery rate by scenario, most-fired policy
    rules, multi-invoice clients, and average overdue by diagnosed cause.

    Read-only — computed entirely in SQL/Python from invoices and audit_log,
    same tables /api/invoices and /api/client-health already read. Nothing
    here is written back.
    """
    return compute_batch_analytics()


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


# ─────────────────────────────────────────────────────────────────────────
# Write endpoints — the three ways invoices enter the pipeline
# ─────────────────────────────────────────────────────────────────────────


@app.post("/api/run-batch")
async def api_run_batch():
    """Triggers the batch pipeline against data/seen.json, simulated (no real Razorpay calls).

    Declared `async def`: the batch is itself concurrent, so it belongs on the
    event loop rather than in Starlette's threadpool.
    """
    results = await _dispatch_batch_async("data/seen.json", rzp_client=None)

    # Portfolio-level scoring runs once, after every invoice in the batch has
    # its diagnosis and policy decision on record — never mid-batch, and
    # never in a way that can change what was just decided above.
    await asyncio.to_thread(score_client_health)

    return _decision_counts(results)


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

    api_key = _require_api_key()
    new_invoices = _split_already_processed(invoices, warnings)

    results = [
        process_invoice(invoice, api_key, rzp_client=None)
        for invoice in new_invoices
    ]

    return {**_decision_counts(results), "warnings": warnings}


# ─────────────────────────────────────────────────────────────────────────
# Escalation queue — where every REQUIRE_HUMAN decision lands
# ─────────────────────────────────────────────────────────────────────────


class ResolveEscalationRequest(BaseModel):
    """A reviewer's verdict on one escalated invoice.

    `actor` is in the body only because this demo has no auth layer; in
    production it comes from the session and a client-supplied value would be
    a way to forge an approval. See api/escalations.py.
    """

    resolution: str = Field(description="approved | rejected | deferred")
    actor: str = Field(min_length=1, description="who is recording this decision")
    note: str = ""
    action: Optional[str] = None


@app.get("/api/escalations")
def api_list_escalations():
    """Open escalations, most urgent first — the human review queue."""
    return load_open_escalations()


@app.get("/api/escalations/summary")
def api_escalation_summary():
    """Queue depth and money under review."""
    return escalation_summary()


@app.get("/api/escalations/history")
def api_escalation_history(limit: int = 50):
    """Recently resolved escalations, newest first."""
    return load_resolution_history(limit=limit)


@app.get("/api/escalations/{invoice_id}")
def api_get_escalation(invoice_id: str):
    """One open case file."""
    try:
        return get_escalation(invoice_id)
    except EscalationNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/escalations/{invoice_id}/resolve")
def api_resolve_escalation(invoice_id: str, body: ResolveEscalationRequest):
    """Records a verdict, and on `approved` dispatches one permitted action.

    The three failure modes are distinct on purpose: 404 means there is
    nothing to resolve, 409 means the invoice moved underneath the reviewer,
    and 422 means the action asked for is not one the policy engine permits on
    this invoice — which is a refusal, not a typo.
    """
    try:
        return resolve_escalation(
            invoice_id=invoice_id,
            resolution=body.resolution,
            actor=body.actor,
            note=body.note,
            action=body.action,
            # Simulated, like every other dashboard-triggered action: a review
            # never creates a live payment link.
            rzp_client=None,
        )
    except EscalationNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except EscalationStale as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ActionNotPermitted as e:
        raise HTTPException(status_code=422, detail=str(e))
    except EscalationError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─────────────────────────────────────────────────────────────────────────
# Razorpay webhook — the push half of reconciliation
# ─────────────────────────────────────────────────────────────────────────


@app.post("/api/webhooks/razorpay")
async def api_razorpay_webhook(request: Request):
    """Receives a signed Razorpay delivery and reconciles the invoice it settles.

    Reads the raw body, not a parsed model: the signature is computed over the
    exact bytes Razorpay sent, and re-serialising parsed JSON would produce a
    different digest for the same delivery.

    A bad signature is 401 and an unconfigured secret is 503 — never 200.
    Razorpay retries any non-2xx, which is the behaviour we want in both cases:
    a real delivery we could not authenticate should come back, not vanish.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    try:
        return await asyncio.to_thread(
            handle_razorpay_webhook, raw_body, signature, build_rzp_client()
        )
    except WebhookNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except InvalidSignature as e:
        raise HTTPException(status_code=401, detail=str(e))


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

    api_key = _require_api_key()
    new_invoices = _split_already_processed(invoices, warnings)

    results = [
        process_invoice(invoice, api_key, rzp_client=None)
        for invoice in new_invoices
    ]

    return {
        "source": "razorpay_api",
        "invoices_fetched": len(raw_invoices),
        **_decision_counts(results),
        "warnings": warnings,
    }

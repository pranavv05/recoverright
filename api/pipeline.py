"""The single pipeline every entry point goes through.

`process_invoice` is what the CLI batch, the dashboard's Run Batch button,
the CSV upload, the Razorpay sync, and the held-out evaluation all call.
There is deliberately no second code path that can reach the executor.

`process_invoice_async` is that same pipeline with the waiting taken out of
the way: same stages in the same order, same audit events, same policy engine,
same workers. It exists because the batch was spending nearly all of its
five minutes blocked on the LLM one invoice at a time. What changes is how
many invoices are in flight at once — never what happens to any one of them.

This module owns orchestration only. Turning merchant input into invoice
dicts lives in `api.csv_import` and `api.razorpay_import`; reading the
results back out lives in `api.queries`; the HTTP surface lives in
`api.routes`. All four call into here, and none of them reimplement it.
"""

import asyncio
import json
import logging
import os
import sys

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from audit.trail import (
    insert_invoice,
    insert_invoice_async,
    log_audit_event,
    log_audit_event_async,
)
from database.connection import close_pool, loop_supports_async_pg, run_async
from diagnosis.diagnoser import (
    close_async_clients,
    diagnose,
    diagnose_async,
    reset_rate_limit_state,
)
from execution.razorpay_client import RazorpayClient
from execution.workers import Orchestrator
from policy.engine import PolicyInput, evaluate

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


# A diagnosis that could not be produced is not a reason to drop the invoice.
# The model being unreachable, rate limited, or returning something
# unparseable says nothing about whether the invoice is safe to chase — so the
# invoice continues through the *same* policy and execution path as any other,
# carrying an evidence tier of "unknown". The engine turns that into
# REQUIRE_HUMAN and the orchestrator routes it to the escalation worker, which
# means a person sees it.
#
# The alternative, returning early, silently removed the invoice from the run:
# no policy decision, no worker, no queue entry, nothing for anyone to action.
# An invoice carrying a legal threat or a prompt injection would have
# disappeared exactly as quietly as a routine one.
def _fail_closed_diagnosis(error: Exception) -> dict:
    """The diagnosis used when the model could not produce one — deliberately
    the weakest possible claim, so every downstream rule treats it as such."""
    return {
        "diagnosed_cause": "unknown",
        "evidence_tier": "unknown",
        "recommended_action": "escalate_to_human",
        "evidence_cited": "",
        "reasoning": f"Diagnosis unavailable ({type(error).__name__}: {error}). "
                     f"Routed to a human without an automated assessment.",
        "suspicious_content_detected": False,
        "confidence": 0.0,
        "diagnosis_failed": True,
    }


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

    diagnosis_failed = False
    diagnosis_error = None
    try:
        diagnosis = diagnose(invoice, api_key)
    except Exception as e:
        log.exception("[%s] diagnosis failed — failing closed to human review", invoice_id)
        log_audit_event(invoice_id, "diagnosis_failed", {
            "error": str(e),
            "execution": {"status": "diagnosis_failed", "error": str(e),
                          "disposition": "failing_closed_to_human_review"},
        })
        diagnosis = _fail_closed_diagnosis(e)
        diagnosis_failed = True
        diagnosis_error = str(e)

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

    result = {
        "invoice_id": invoice_id,
        "diagnosis": diagnosis,
        "policy_decision": decision.decision,
        "policy_reasons": decision.reasons,
        "worker": execution_result.get("worker"),
        "execution": execution_result,
    }
    if diagnosis_failed:
        # Still carries the failure status, so the evaluation runner keeps
        # excluding it from accuracy metrics — the model produced no
        # diagnosis, and scoring "unknown" against ground truth would be
        # measuring an outage rather than the system's judgment. The policy
        # decision and the escalation above are real and already on record.
        result["status"] = "diagnosis_failed"
        result["error"] = diagnosis_error
    return result


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

    diagnosis_failed = False
    diagnosis_error = None
    try:
        diagnosis = await diagnose_async(invoice, api_key)
    except Exception as e:
        log.exception("[%s] diagnosis failed — failing closed to human review", invoice_id)
        await log_audit_event_async(invoice_id, "diagnosis_failed", {
            "error": str(e),
            "execution": {"status": "diagnosis_failed", "error": str(e),
                          "disposition": "failing_closed_to_human_review"},
        })
        diagnosis = _fail_closed_diagnosis(e)
        diagnosis_failed = True
        diagnosis_error = str(e)

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

    result = {
        "invoice_id": invoice_id,
        "diagnosis": diagnosis,
        "policy_decision": decision.decision,
        "policy_reasons": decision.reasons,
        "worker": execution_result.get("worker"),
        "execution": execution_result,
    }
    if diagnosis_failed:
        # Still carries the failure status, so the evaluation runner keeps
        # excluding it from accuracy metrics — the model produced no
        # diagnosis, and scoring "unknown" against ground truth would be
        # measuring an outage rather than the system's judgment. The policy
        # decision and the escalation above are real and already on record.
        result["status"] = "diagnosis_failed"
        result["error"] = diagnosis_error
    return result


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

    # Start from the configured pace rather than inheriting whatever a previous
    # batch in this process backed off to, so a run's timing is a property of
    # the run and not of what happened before it.
    reset_rate_limit_state()

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

    return await _sweep_failed_async(invoices, results, api_key, rzp_client)


# Statuses that mean "the LLM never answered", as opposed to a real decision.
# `skipped` is deliberately absent: an invoice with nothing outstanding was
# decided, it just wasn't chased.
_RETRYABLE_STATUSES = {"diagnosis_failed", "error"}

# Serial recovery pass. Kept small on purpose — this is a safety net for a
# saturated rate limit, not a second batch.
SWEEP_MAX_PASSES = 2
SWEEP_PAUSE_SECONDS = 3.0


async def _sweep_failed_async(invoices, results, api_key, rzp_client):
    """Retries invoices the concurrent pass could not diagnose, one at a time.

    The fan-out is tuned for throughput against a shared free-tier endpoint,
    and a burst that trips its rate limit can exhaust an invoice's retry
    budget on the limit rather than on anything about the invoice. Those
    invoices are not failures of the pipeline, and leaving them as
    `diagnosis_failed` misreports the batch: the dashboard shows `n/a` where
    there is no decision, and the evaluation's denominators quietly shrink.

    So once the herd has drained, whatever is still unresolved is retried
    sequentially — no concurrency, a pause between attempts, so it is asking
    the API for one request at a time rather than competing with itself. A
    result that comes back still-failed is kept as-is and reported honestly;
    this pass can only improve the batch, never mask a genuine failure.
    """
    for pass_number in range(1, SWEEP_MAX_PASSES + 1):
        pending = [
            index for index, result in enumerate(results)
            if result.get("status") in _RETRYABLE_STATUSES
        ]
        if not pending:
            break

        log.warning(
            "sweep pass %d/%d: retrying %d invoice(s) serially after the "
            "concurrent pass could not diagnose them",
            pass_number, SWEEP_MAX_PASSES, len(pending),
        )
        reset_rate_limit_state()

        for index in pending:
            invoice = invoices[index]
            await asyncio.sleep(SWEEP_PAUSE_SECONDS)
            try:
                retried = await process_invoice_async(invoice, api_key, rzp_client)
            except Exception as e:
                log.error("[%s] sweep retry failed: %s", invoice["invoice_id"], e)
                continue
            if retried.get("status") not in _RETRYABLE_STATUSES:
                log.info("[%s] recovered on sweep pass %d",
                         invoice["invoice_id"], pass_number)
                results[index] = retried

    still_failed = [
        r["invoice_id"] for r in results
        if r.get("status") in _RETRYABLE_STATUSES
    ]
    if still_failed:
        log.error(
            "%d invoice(s) undiagnosed after %d sweep pass(es): %s",
            len(still_failed), SWEEP_MAX_PASSES, ", ".join(still_failed),
        )

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

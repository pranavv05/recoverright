import time
from datetime import datetime, timezone

from audit.trail import log_audit_event, update_recovery
from execution.razorpay_client import DuplicateReferenceError, RateLimitError, ServerError

COMPANY_NAME = "Arjun Logistics"


class MaxRetriesExceeded(Exception):
    """Raised when create_payment_link_with_retry exhausts all retry attempts."""


def create_payment_link_with_retry(
    rzp_client,
    invoice_id: str,
    amount_paise: int,
    client_name: str,
    description: str,
    max_retries: int = 3
) -> dict:
    """
    Attempts to create a Razorpay payment link with exponential backoff.
    Logs every attempt to the audit trail — success, retry, and failure.
    Raises MaxRetriesExceeded if all attempts fail on rate limit.
    Raises ServerError immediately on 5xx — no retry, log and surface.
    """
    for attempt in range(max_retries):
        try:
            result = rzp_client.create_payment_link(
                invoice_id=invoice_id,
                amount_paise=amount_paise,
                client_name=client_name,
                description=description
            )
            # Success — log it and return
            log_audit_event(invoice_id, "api_success", {
                "action": "create_payment_link",
                "attempt": attempt + 1,
                "payment_link_id": result.get("id"),
                "short_url": result.get("short_url")
            })
            return result

        except RateLimitError:
            # No sleep after the final attempt: the loop is about to exit and
            # raise MaxRetriesExceeded, so backing off first only delays the
            # failure the caller is already going to get. `wait_seconds: 0` on
            # the last event records that honestly rather than logging a delay
            # that was never served.
            is_last_attempt = attempt == max_retries - 1
            wait = 0 if is_last_attempt else 2 ** attempt  # 1s, then 2s
            log_audit_event(invoice_id, "api_retry", {
                "action": "create_payment_link",
                "attempt": attempt + 1,
                "wait_seconds": wait,
                "reason": "rate_limit_429"
            })
            if wait:
                time.sleep(wait)

        except ServerError as e:
            # Don't retry 5xx — it's Razorpay's problem, surface it immediately
            log_audit_event(invoice_id, "api_failure", {
                "action": "create_payment_link",
                "attempt": attempt + 1,
                "reason": "server_error_5xx",
                "error": str(e)
            })
            raise

        except DuplicateReferenceError as e:
            # Razorpay's reference_id uniqueness rejected a second link for
            # this invoice. That is duplicate protection working, not a
            # failure — surface it as its own outcome so the caller can reuse
            # the existing link instead of treating it as a broken request.
            log_audit_event(invoice_id, "duplicate_link_prevented", {
                "action": "create_payment_link",
                "attempt": attempt + 1,
                "reason": "reference_id_already_exists",
                "detail": str(e),
            })
            raise

        except ValueError as e:
            # 4xx (e.g. bad credentials, malformed request) — not transient,
            # retrying won't help. Log and surface immediately, same as 5xx.
            log_audit_event(invoice_id, "api_failure", {
                "action": "create_payment_link",
                "attempt": attempt + 1,
                "reason": "client_error_4xx",
                "error": str(e)
            })
            raise

    # Exhausted all retries
    log_audit_event(invoice_id, "api_failure_max_retries", {
        "action": "create_payment_link",
        "max_retries": max_retries,
        "reason": "rate_limit_not_resolved_after_retries"
    })
    raise MaxRetriesExceeded(
        f"create_payment_link failed for {invoice_id} after {max_retries} attempts"
    )


def reconcile_payment(rzp_client, invoice_id: str, payment_link_id: str) -> dict:
    """Verifies a Razorpay payment link and updates the recoveries table.

    THIS IS THE ONLY PLACE MONEY IS EVER COUNTED AS RECOVERED. Link creation
    records `amount_recovered_paise = 0`; nothing else in the codebase writes
    a non-zero value to that column.

    Every one of these must hold before a rupee is credited:
      * Razorpay reports the link status as "paid"
      * the link Razorpay returned is the one we asked about
      * the link's reference_id is this invoice
      * amount_paid is a positive integer number of paise
      * amount_paid does not exceed the amount the link was raised for

    Any failed check records a discrepancy in the audit trail and credits
    nothing. A partially paid link is reported as partial and not counted —
    partial settlement of a B2B invoice is a human's call, not the agent's.
    """
    result = rzp_client.verify_payment(payment_link_id)
    razorpay_status = result.get("status")

    def _pending(reason: str, **extra) -> dict:
        payload = {
            "action": "reconcile_payment",
            "payment_link_id": payment_link_id,
            "razorpay_status": razorpay_status,
            "reason": reason,
            **extra,
        }
        log_audit_event(invoice_id, "reconciliation_pending", payload)
        return {"status": "pending", "razorpay_status": razorpay_status, "reason": reason}

    if razorpay_status != "paid":
        return _pending(
            "partially_paid_requires_human_review"
            if razorpay_status == "partially_paid" else "link_not_yet_paid"
        )

    # Identity checks — never credit an invoice against someone else's link.
    returned_id = result.get("id")
    if returned_id and returned_id != payment_link_id:
        log_audit_event(invoice_id, "reconciliation_discrepancy", {
            "action": "reconcile_payment",
            "reason": "payment_link_id_mismatch",
            "requested": payment_link_id,
            "returned": returned_id,
        })
        return {"status": "discrepancy", "reason": "payment_link_id_mismatch"}

    reference_id = result.get("reference_id")
    if reference_id and reference_id != invoice_id:
        log_audit_event(invoice_id, "reconciliation_discrepancy", {
            "action": "reconcile_payment",
            "reason": "reference_id_mismatch",
            "expected": invoice_id,
            "returned": reference_id,
        })
        return {"status": "discrepancy", "reason": "reference_id_mismatch"}

    # Amount checks — the credited figure is Razorpay's reported amount_paid,
    # never the amount we asked for.
    amount_paid = result.get("amount_paid")
    if not isinstance(amount_paid, int) or isinstance(amount_paid, bool) or amount_paid <= 0:
        log_audit_event(invoice_id, "reconciliation_discrepancy", {
            "action": "reconcile_payment",
            "reason": "paid_status_without_usable_amount_paid",
            "amount_paid": amount_paid,
        })
        return {"status": "discrepancy", "reason": "paid_status_without_usable_amount_paid"}

    amount_requested = result.get("amount")
    if isinstance(amount_requested, int) and amount_paid > amount_requested:
        log_audit_event(invoice_id, "reconciliation_discrepancy", {
            "action": "reconcile_payment",
            "reason": "amount_paid_exceeds_link_amount",
            "amount_paid": amount_paid,
            "amount_requested": amount_requested,
        })
        return {"status": "discrepancy", "reason": "amount_paid_exceeds_link_amount"}

    verified_at = datetime.now(timezone.utc)
    update_recovery(
        invoice_id=invoice_id,
        status="payment_verified",
        amount_recovered_paise=amount_paid,
        verified_at=verified_at,
    )
    log_audit_event(invoice_id, "payment_verified", {
        "action": "reconcile_payment",
        "payment_link_id": payment_link_id,
        "amount_recovered_paise": amount_paid,
        "source": "razorpay_reported_amount_paid",
    })
    return {
        "status": "payment_verified",
        "amount_recovered_paise": amount_paid,
        "verified_at": verified_at.isoformat(),
    }

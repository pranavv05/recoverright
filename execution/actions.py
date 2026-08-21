import time
from audit.trail import log_audit_event

class RateLimitError(Exception):
    pass

class ServerError(Exception):
    pass

class MaxRetriesExceeded(Exception):
    pass


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
            wait = 2 ** attempt   # attempt 0 → 1s, attempt 1 → 2s, attempt 2 → 4s
            log_audit_event(invoice_id, "api_retry", {
                "action": "create_payment_link",
                "attempt": attempt + 1,
                "wait_seconds": wait,
                "reason": "rate_limit_429"
            })
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

    # Exhausted all retries
    log_audit_event(invoice_id, "api_failure_max_retries", {
        "action": "create_payment_link",
        "max_retries": max_retries,
        "reason": "rate_limit_not_resolved_after_retries"
    })
    raise MaxRetriesExceeded(
        f"create_payment_link failed for {invoice_id} after {max_retries} attempts"
    )


def execute_action(
    action: str,
    invoice: dict,
    outstanding_paise: int,
    rzp_client
) -> dict:
    """
    Dispatcher — takes the policy-allowed action and routes it
    to the correct execution function.
    Returns a result dict that goes into the audit trail.
    """
    if action == "create_payment_link":
        try:
            result = create_payment_link_with_retry(
                rzp_client=rzp_client,
                invoice_id=invoice["invoice_id"],
                amount_paise=outstanding_paise,
                client_name=invoice["client_name"],
                description=f"Payment for {invoice['invoice_id']} "
                            f"— Arjun Logistics"
            )
            return {
                "status": "link_created",
                "payment_link_id": result.get("id"),
                "short_url": result.get("short_url"),
                "amount_paise": outstanding_paise
            }
        except MaxRetriesExceeded:
            return {
                "status": "failed_max_retries",
                "amount_paise": outstanding_paise
            }
        except ServerError as e:
            return {
                "status": "failed_server_error",
                "error": str(e),
                "amount_paise": outstanding_paise
            }

    elif action == "send_reminder":
        # Simulated in demo — log the intent, don't send real email
        log_audit_event(invoice["invoice_id"], "action_simulated", {
            "action": "send_reminder",
            "reason": "email_sending_simulated_in_demo"
        })
        return {"status": "reminder_simulated"}

    elif action == "gather_information":
        log_audit_event(invoice["invoice_id"], "action_taken", {
            "action": "gather_information",
            "note": "Routed to information-gathering queue"
        })
        return {"status": "information_gathering_queued"}

    elif action == "wait":
        log_audit_event(invoice["invoice_id"], "action_taken", {
            "action": "wait",
            "note": "Chronic late-payer within normal pattern — no action"
        })
        return {"status": "no_action_within_pattern"}

    else:
        # Anything else should have been caught by the policy engine
        log_audit_event(invoice["invoice_id"], "action_unknown", {
            "action": action
        })
        return {"status": "unhandled_action", "action": action}
import json
from datetime import datetime, timezone
from database.connection import get_connection


def log_audit_event(invoice_id: str, event_type: str, payload: dict) -> None:
    """
    Writes a single audit event to the audit_log table.
    Called by every stage of the pipeline — diagnosis, policy, execution, retry, failure.
    invoice_id must already exist in the invoices table before calling this.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log (invoice_id, event_type, decision, reasons,
                    allowed_actions, blocked_actions, diagnosis, execution_result)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    invoice_id,
                    event_type,
                    payload.get("decision"),
                    json.dumps(payload.get("reasons", [])),
                    json.dumps(payload.get("allowed_actions", [])),
                    json.dumps(payload.get("blocked_actions", [])),
                    json.dumps(payload.get("diagnosis")) if payload.get("diagnosis") else None,
                    json.dumps(payload.get("execution")) if payload.get("execution") else None,
                )
            )


def insert_invoice(invoice: dict) -> None:
    """
    Inserts the invoice record into the invoices table.
    Must be called before any audit events for this invoice_id.
    """
    outstanding = invoice["amount_paise"] - invoice["partial_payment_received_paise"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO invoices (
                    invoice_id, client_name, amount_paise, outstanding_balance_paise,
                    days_overdue, account_value_tier, scenario_type, true_cause,
                    client_notes, processed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (invoice_id) DO NOTHING
                """,
                (
                    invoice["invoice_id"],
                    invoice["client_name"],
                    invoice["amount_paise"],
                    outstanding,
                    invoice["days_overdue"],
                    invoice["account_value_tier"],
                    invoice["scenario_type"],
                    invoice["true_cause"],
                    invoice.get("client_notes") or "",
                    datetime.now(timezone.utc)
                )
            )


def insert_recovery(
    invoice_id: str,
    payment_link_id: str,
    payment_link_url: str,
    amount_requested_paise: int,
    status: str = "link_created"
) -> None:
    """
    Records that a payment link was created for an invoice. Recovery is NOT
    counted here — amount_recovered_paise stays 0 until update_recovery()
    marks the payment as verified.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recoveries (
                    invoice_id, payment_link_id, payment_link_url,
                    status, amount_requested_paise, amount_recovered_paise
                ) VALUES (%s, %s, %s, %s, %s, 0)
                ON CONFLICT (invoice_id) DO UPDATE SET
                    payment_link_id = EXCLUDED.payment_link_id,
                    payment_link_url = EXCLUDED.payment_link_url,
                    status = EXCLUDED.status,
                    amount_requested_paise = EXCLUDED.amount_requested_paise
                """,
                (invoice_id, payment_link_id, payment_link_url, status, amount_requested_paise)
            )


def update_recovery(
    invoice_id: str,
    status: str,
    amount_recovered_paise: int = None,
    verified_at=None
) -> None:
    """Updates a recovery row's status and, once payment is verified, the recovered amount."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE recoveries
                SET status = %s,
                    amount_recovered_paise = COALESCE(%s, amount_recovered_paise),
                    verified_at = COALESCE(%s, verified_at)
                WHERE invoice_id = %s
                """,
                (status, amount_recovered_paise, verified_at, invoice_id)
            )


def get_recovery(invoice_id: str) -> dict:
    """Returns the recoveries row for an invoice, or None if no payment link has been created yet."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM recoveries WHERE invoice_id = %s",
                (invoice_id,)
            )
            return cur.fetchone()


def get_audit_trail(invoice_id: str) -> list:
    """
    Returns the full audit trail for a given invoice — every event in order.
    Used by the dashboard to build the per-invoice decision view.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_type, decision, reasons, allowed_actions,
                       blocked_actions, diagnosis, execution_result, created_at
                FROM audit_log
                WHERE invoice_id = %s
                ORDER BY created_at ASC
                """,
                (invoice_id,)
            )
            return cur.fetchall()
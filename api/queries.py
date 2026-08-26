"""The read layer behind the dashboard.

Every function here is a query — it reads what the pipeline already wrote and
shapes it for the API. Nothing in this module decides anything about an
invoice or writes a row, which is why the endpoints in `api.routes` can be
one line each and this logic can be tested without an HTTP client.

Money is emitted as integer paise throughout. An earlier revision returned
`total_outstanding_rupees` as `paise / 100`, and in Python 3 that is true
division: the one place in this codebase where a money value became a binary
float, at the API boundary where it is hardest to notice. It survived a
`float(` grep precisely because there is no `float(` in it. Formatting for
display is the dashboard's job.
"""

from database.connection import get_connection


# Worst-first. The dashboard re-sorts client-side so its column headers stay
# interactive; this is the order the API hands rows over in.
_RISK_TIER_ORDER = {"critical": 0, "at_risk": 1, "watch": 2, "healthy": 3}


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


def compute_metrics() -> dict:
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


def load_client_health() -> list:
    """All client health scores, most severe risk tier first.

    Reads whatever evaluation.health_scorer last wrote to
    client_health_scores — populated after a batch run, not computed here.

    Money is emitted as integer paise, like every other endpoint. An earlier
    revision returned `total_outstanding_rupees` as `paise / 100`, and in
    Python 3 that is true division: the one place in this codebase where a
    money value became a binary float, at the API boundary where it is
    hardest to notice. It survived a `float(` grep precisely because there is
    no `float(` in it. Formatting for display is the dashboard's job.
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
            "total_outstanding_paise": r["total_outstanding_paise"],
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

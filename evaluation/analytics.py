"""Batch-level analytics — recovery rates by scenario, most-fired policy
rules, multi-invoice clients, and average overdue by diagnosed cause.

Read-only, like health_scorer.py: every function here queries
invoices / audit_log / recoveries — tables the pipeline already wrote — and
returns aggregate views. Nothing here writes to any table, and nothing here
feeds back into a policy decision or an execution result.

Every invoice/policy/diagnosis fact used here is taken from the *latest*
audit_log row of its event_type, the same "most recent wins" convention
api/main.py and health_scorer.py already use — so re-running a batch on
invoices already in the table (upserted, not duplicated) still reports one
current state per invoice rather than double-counting stale rows.
"""

from collections import Counter

from database.connection import get_connection

TOP_POLICY_RULES_LIMIT = 10


def _latest_per_invoice(cur, event_type: str) -> dict:
    """Returns {invoice_id: row} for the most recent audit_log row of one event_type."""
    cur.execute(
        """
        SELECT DISTINCT ON (invoice_id) invoice_id, decision, reasons, diagnosis
        FROM audit_log
        WHERE event_type = %s
        ORDER BY invoice_id, created_at DESC, id DESC
        """,
        (event_type,)
    )
    return {row["invoice_id"]: row for row in cur.fetchall()}


def _recovery_by_scenario(invoices: list, policy_decisions: dict, recovered_ids: set) -> list:
    """Section A: recovery rate, decision mix, and outstanding total per scenario_type."""
    by_scenario = {}
    for inv in invoices:
        scenario = inv["scenario_type"] or "unknown"
        bucket = by_scenario.setdefault(scenario, {
            "scenario_type": scenario,
            "total_invoices": 0,
            "allow_count": 0,
            "block_count": 0,
            "require_human_count": 0,
            "recovered_count": 0,
            "total_outstanding_paise": 0,
        })
        bucket["total_invoices"] += 1
        bucket["total_outstanding_paise"] += inv["outstanding_balance_paise"] or 0

        decision = (policy_decisions.get(inv["invoice_id"]) or {}).get("decision")
        if decision == "ALLOW":
            bucket["allow_count"] += 1
        elif decision == "BLOCK":
            bucket["block_count"] += 1
        elif decision == "REQUIRE_HUMAN":
            bucket["require_human_count"] += 1

        if inv["invoice_id"] in recovered_ids:
            bucket["recovered_count"] += 1

    results = []
    for bucket in by_scenario.values():
        recovered_count = bucket.pop("recovered_count")
        total = bucket["total_invoices"]
        bucket["recovery_rate"] = round(100 * recovered_count / total, 1) if total else 0.0
        results.append(bucket)

    results.sort(key=lambda r: (-r["recovery_rate"], r["scenario_type"]))
    return results


def _top_policy_rules(policy_decisions: dict) -> list:
    """Section B: the most frequently fired policy reason strings, top 10."""
    counter = Counter()
    for row in policy_decisions.values():
        for reason in (row.get("reasons") or []):
            counter[reason] += 1

    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return [
        {"reason": reason, "count": count}
        for reason, count in ranked[:TOP_POLICY_RULES_LIMIT]
    ]


def _multi_invoice_clients(invoices: list) -> list:
    """Section C: clients with 2+ invoices, sorted by outstanding descending."""
    by_client = {}
    for inv in invoices:
        client = inv["client_name"]
        bucket = by_client.setdefault(client, {
            "client_name": client,
            "invoice_count": 0,
            "total_outstanding_paise": 0,
            "invoice_ids": [],
        })
        bucket["invoice_count"] += 1
        bucket["total_outstanding_paise"] += inv["outstanding_balance_paise"] or 0
        bucket["invoice_ids"].append(inv["invoice_id"])

    results = [bucket for bucket in by_client.values() if bucket["invoice_count"] >= 2]
    results.sort(key=lambda r: (-r["total_outstanding_paise"], r["client_name"]))
    return results


def _avg_overdue_by_cause(invoices: list, diagnoses: dict) -> list:
    """Section D: average days_overdue per diagnosed_cause, sorted descending."""
    by_cause = {}
    for inv in invoices:
        diagnosis_row = diagnoses.get(inv["invoice_id"])
        cause = ((diagnosis_row or {}).get("diagnosis") or {}).get("diagnosed_cause") or "unknown"
        bucket = by_cause.setdefault(cause, {"diagnosed_cause": cause, "days_sum": 0, "invoice_count": 0})
        bucket["days_sum"] += inv["days_overdue"] or 0
        bucket["invoice_count"] += 1

    results = [
        {
            "diagnosed_cause": bucket["diagnosed_cause"],
            "avg_days_overdue": round(bucket["days_sum"] / bucket["invoice_count"], 1),
            "invoice_count": bucket["invoice_count"],
        }
        for bucket in by_cause.values()
    ]
    results.sort(key=lambda r: (-r["avg_days_overdue"], r["diagnosed_cause"]))
    return results


def compute_batch_analytics() -> dict:
    """Computes all five analytics sections from the current state of the database.

    One connection, one pass over `invoices` plus the latest `policy_decision`
    and `diagnosis` audit rows per invoice — the same data every dashboard
    read endpoint already assembles, just aggregated instead of listed per row.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT invoice_id, client_name, scenario_type,
                       outstanding_balance_paise, days_overdue, processed_at
                FROM invoices
                ORDER BY invoice_id
                """
            )
            invoices = cur.fetchall()

            policy_decisions = _latest_per_invoice(cur, "policy_decision")
            diagnoses = _latest_per_invoice(cur, "diagnosis")

            cur.execute("SELECT invoice_id FROM recoveries WHERE status = 'payment_verified'")
            recovered_ids = {row["invoice_id"] for row in cur.fetchall()}

            cur.execute(
                "SELECT COALESCE(SUM(amount_recovered_paise), 0) AS total "
                "FROM recoveries WHERE status = 'payment_verified'"
            )
            # SUM(bigint) comes back from Postgres as numeric/Decimal; money is
            # always a plain int count of paise everywhere else in this codebase.
            total_recovered_paise = int(cur.fetchone()["total"])

    total_invoices = len(invoices)
    total_outstanding_paise = sum(inv["outstanding_balance_paise"] or 0 for inv in invoices)
    unique_clients = len({inv["client_name"] for inv in invoices})
    latest_processed_at = max((inv["processed_at"] for inv in invoices), default=None)
    overall_recovery_rate = (
        round(100 * total_recovered_paise / total_outstanding_paise, 1)
        if total_outstanding_paise else 0.0
    )

    return {
        "recovery_by_scenario": _recovery_by_scenario(invoices, policy_decisions, recovered_ids),
        "top_policy_rules": _top_policy_rules(policy_decisions),
        "multi_invoice_clients": _multi_invoice_clients(invoices),
        "avg_overdue_by_cause": _avg_overdue_by_cause(invoices, diagnoses),
        "batch_summary": {
            "total_invoices_processed": total_invoices,
            "total_outstanding_paise": total_outstanding_paise,
            "total_recovered_paise": total_recovered_paise,
            "overall_recovery_rate": overall_recovery_rate,
            "unique_clients": unique_clients,
            "processing_timestamp": latest_processed_at,
        },
    }

"""Merchant health scoring — client-level risk intelligence from a completed batch.

This module turns individual invoice diagnoses into a portfolio view: given
every invoice RecoverRight has processed for a client, what does the pattern
across all of them say about that client's likelihood of voluntary payment?

Deliberately has ZERO LLM dependency. Every invoice already carries a
diagnosed cause and a policy decision from the pipeline that processed it —
health scoring reads those, and only those. Rolling up a fixed set of
per-invoice facts (a cause string, a paise amount, a day count, a history
list) into a tier is arithmetic and table lookups, not judgment, so the same
batch of invoices always produces the same client health scores.

Two layers:
  - `score_client()` is pure: client name + a list of per-invoice records in,
    one health-score dict out. No database, no I/O — this is what
    test_health_scorer.py exercises with synthetic data.
  - `score_client_health()` is the integration point: queries `invoices` and
    `audit_log` for the invoices a batch just wrote, groups them by client,
    calls `score_client()` for each, and upserts the results into
    `client_health_scores`. Called once, after a batch finishes — never
    during individual invoice processing, and never read by the pipeline
    that produces its inputs.
"""

from collections import Counter
from datetime import datetime, timezone

from database.connection import get_connection

# ── Money thresholds, in paise (money is always an integer count of paise) ──
TEN_LAKH_PAISE = 10_00_000 * 100
FIVE_LAKH_PAISE = 5_00_000 * 100

# ── Risk tier trigger thresholds ─────────────────────────────────────────
CRITICAL_PROMISE_BROKEN_COUNT = 3
CRITICAL_DAYS_OVERDUE_AVG = 45
AT_RISK_STALL_COUNT = 2
AT_RISK_DAYS_OVERDUE_AVG = 30
WATCH_DAYS_OVERDUE_MIN = 15
WATCH_DAYS_OVERDUE_MAX = 30

# Policy-engine reason strings (policy/engine.py) that mark a legal threat or
# a dispute independently of whatever the LLM diagnosed as the cause — the
# content-detection rules that produce them run on the raw client notes and
# can fire regardless of diagnosed_cause.
LEGAL_THREAT_REASON = "legal_threat_detected_in_client_notes"
DISPUTE_REASON = "dispute_signal_detected_in_client_notes"

CAPITAL_FLAG_REASON_SUFFIX = "Credit decision not made. Flagged for Razorpay Capital review."

# A payment history entry counts against the client for trend purposes.
LATE_OR_MISSED = {"paid_late", "missed"}

MONTHS_FOR_TREND = 3
MIN_HISTORY_FOR_TREND = 2 * MONTHS_FOR_TREND


def _is_legal_threat(invoice: dict) -> bool:
    """True if this invoice's diagnosis or policy decision flagged a legal threat."""
    if invoice.get("diagnosed_cause") == "legal_threat":
        return True
    return LEGAL_THREAT_REASON in (invoice.get("policy_reasons") or [])


def _is_dispute(invoice: dict) -> bool:
    """True if this invoice's diagnosis or policy decision flagged a dispute."""
    if invoice.get("diagnosed_cause") == "dispute":
        return True
    return DISPUTE_REASON in (invoice.get("policy_reasons") or [])


def _dominant_cause(invoices: list) -> str:
    """Most common diagnosed_cause across the client's invoices.

    Ties are broken alphabetically so the result is deterministic regardless
    of the order invoices were queried in.
    """
    causes = [inv.get("diagnosed_cause") or "unknown" for inv in invoices]
    counts = Counter(causes)
    best_count = max(counts.values())
    return min(cause for cause, count in counts.items() if count == best_count)


def _longest_payment_history(invoices: list) -> list:
    """The fullest client_payment_history among this client's invoices.

    Different invoices for the same client may carry the history as it stood
    at that invoice's issue date; the longest one is the most complete view
    of the client's track record.
    """
    histories = [inv.get("client_payment_history") or [] for inv in invoices]
    return max(histories, key=len, default=[])


def compute_payment_trend(history: list) -> str:
    """Compares the most recent 3 entries against the previous 3.

    History is ordered oldest-first, matching the dataset's convention (see
    data/seen.json). Fewer than 6 entries is not enough signal either way.
    """
    if len(history) < MIN_HISTORY_FOR_TREND:
        return "insufficient_data"

    recent = history[-MONTHS_FOR_TREND:]
    previous = history[-2 * MONTHS_FOR_TREND:-MONTHS_FOR_TREND]

    recent_bad = sum(1 for entry in recent if entry in LATE_OR_MISSED)
    previous_bad = sum(1 for entry in previous if entry in LATE_OR_MISSED)

    if recent_bad > previous_bad:
        return "deteriorating"
    if recent_bad < previous_bad:
        return "improving"
    return "stable"


def _was_formerly_reliable(history: list) -> bool:
    """True when the older half of the history is mostly on-time payments.

    Used only to qualify the capital-lead flag: a client whose payment
    behaviour has always been poor is not "formerly reliable", even if the
    most recent 3 entries are slightly worse than the 3 before them.
    """
    if len(history) < MIN_HISTORY_FOR_TREND:
        return False
    previous = history[-2 * MONTHS_FOR_TREND:-MONTHS_FOR_TREND]
    on_time = sum(1 for entry in previous if entry not in LATE_OR_MISSED)
    return on_time > len(previous) / 2


def _reliable_months_before_deterioration(history: list) -> int:
    """Months of history preceding the most recent trend window.

    compute_payment_trend() judges deterioration from the most recent
    MONTHS_FOR_TREND entries; everything before that window is the track
    record the capital flag's "was reliable" claim is actually describing.
    """
    return max(len(history) - MONTHS_FOR_TREND, 0)


def _rupees(paise: int) -> str:
    """Formats integer paise as a comma-grouped rupee figure for pattern text."""
    return f"{paise // 100:,}"


def _assign_tier(
    *, promise_broken_count, deliberate_stall_count, legal_threat, days_overdue_avg,
    payment_trend, dispute_count, total_outstanding_paise,
):
    """Returns (tier, trigger) — deterministic, checked most-severe first."""
    stall_count = promise_broken_count + deliberate_stall_count

    if promise_broken_count >= CRITICAL_PROMISE_BROKEN_COUNT:
        return "critical", "broken_commitments"
    if legal_threat:
        return "critical", "legal_threat"
    if total_outstanding_paise > TEN_LAKH_PAISE and days_overdue_avg > CRITICAL_DAYS_OVERDUE_AVG:
        return "critical", "high_value_overdue"

    if stall_count >= AT_RISK_STALL_COUNT:
        return "at_risk", "repeated_stall"
    if days_overdue_avg > AT_RISK_DAYS_OVERDUE_AVG and payment_trend == "deteriorating":
        return "at_risk", "deteriorating_overdue"
    if dispute_count > 0 and total_outstanding_paise > FIVE_LAKH_PAISE:
        return "at_risk", "dispute_high_value"

    if stall_count == 1:
        return "watch", "single_stall"
    if WATCH_DAYS_OVERDUE_MIN <= days_overdue_avg <= WATCH_DAYS_OVERDUE_MAX:
        return "watch", "moderate_overdue"
    if payment_trend == "deteriorating":
        return "watch", "deteriorating_trend"

    return "healthy", "healthy"


def _describe_pattern(
    *, client_name, invoice_count, tier, trigger, capital_flag, promise_broken_count,
    deliberate_stall_count, total_outstanding_paise, days_overdue_avg, dominant_cause,
):
    """Deterministic, template-based pattern description. No LLM involved."""
    outstanding = _rupees(total_outstanding_paise)
    stall_count = promise_broken_count + deliberate_stall_count
    invoice_word = "invoice" if invoice_count == 1 else "invoices"

    if tier == "critical":
        if trigger == "broken_commitments":
            return (
                f"Client has broken payment commitments {promise_broken_count} times "
                f"in the last 90 days. Total outstanding: ₹{outstanding}. "
                f"Probability of voluntary payment: Low."
            )
        if trigger == "legal_threat":
            return (
                f"Client has escalated to legal action on at least one invoice. "
                f"Total outstanding: ₹{outstanding}. Probability of voluntary payment: Low."
            )
        return (
            f"Outstanding exceeds ₹10 lakhs with an average of "
            f"{days_overdue_avg:.0f} days overdue. Total outstanding: ₹{outstanding}. "
            f"Probability of voluntary payment: Low."
        )

    if tier == "at_risk":
        if capital_flag:
            return (
                "Client's payment behavior has deteriorated from reliable to "
                "consistently late. Pattern suggests cash flow constraint rather "
                "than unwillingness. Candidate for Razorpay Capital working "
                "capital assessment."
            )
        if trigger == "repeated_stall":
            return (
                f"Client has broken or stalled payment commitments {stall_count} times. "
                f"Automated follow-up has not resolved the balance."
            )
        if trigger == "dispute_high_value":
            return (
                f"Active dispute on an account with ₹{outstanding} outstanding. "
                f"Recommend legal/finance review before further contact."
            )
        return (
            f"Payment behavior has deteriorated with an average of "
            f"{days_overdue_avg:.0f} days overdue. Recommend human review before "
            f"further automated contact."
        )

    if tier == "watch":
        return (
            f"Average days to payment increasing. Currently {days_overdue_avg:.0f} "
            f"days overdue on average. Recommend shortening payment terms on next contract."
        )

    if dominant_cause in ("payment_link_failure", "wrong_contact"):
        be = "is" if invoice_count == 1 else "are"
        return (
            f"{client_name} is a reliable payer. Current overdue {invoice_word} "
            f"(₹{outstanding} outstanding) {be} technical or administrative, not "
            f"behavioral — diagnosed as {dominant_cause.replace('_', ' ')}."
        )
    return (
        f"{client_name} is a reliable payer with no significant risk signals "
        f"in this batch — {invoice_count} {invoice_word}, ₹{outstanding} outstanding."
    )


def score_client(client_name: str, invoices: list) -> dict:
    """Computes the full health-score row for one client from its invoice records.

    Pure function — every input is a plain dict already merged from the
    invoices/audit_log rows for one invoice_id:
        outstanding_balance_paise, days_overdue (both from `invoices`)
        diagnosed_cause, policy_reasons (both from the latest audit_log events)
        client_payment_history (from `invoices`)

    Raises ValueError on an empty invoice list — a client with zero invoices
    has nothing to score, and the caller (score_client_health) never produces
    that grouping since every client in the query has at least one invoice.
    """
    if not invoices:
        raise ValueError(f"score_client() called with no invoices for {client_name!r}")

    invoice_count = len(invoices)
    total_outstanding_paise = sum(inv.get("outstanding_balance_paise") or 0 for inv in invoices)
    days_overdue_avg = sum(inv.get("days_overdue") or 0 for inv in invoices) / invoice_count

    promise_broken_count = sum(1 for inv in invoices if inv.get("diagnosed_cause") == "promise_broken")
    deliberate_stall_count = sum(1 for inv in invoices if inv.get("diagnosed_cause") == "deliberate_stall")
    dispute_count = sum(1 for inv in invoices if _is_dispute(inv))
    legal_threat = any(_is_legal_threat(inv) for inv in invoices)
    dominant_cause = _dominant_cause(invoices)

    history = _longest_payment_history(invoices)
    payment_trend = compute_payment_trend(history)

    tier, trigger = _assign_tier(
        promise_broken_count=promise_broken_count,
        deliberate_stall_count=deliberate_stall_count,
        legal_threat=legal_threat,
        days_overdue_avg=days_overdue_avg,
        payment_trend=payment_trend,
        dispute_count=dispute_count,
        total_outstanding_paise=total_outstanding_paise,
    )

    capital_flag = (
        dominant_cause == "cash_flow_delay"
        and dispute_count == 0
        and not legal_threat
        and payment_trend == "deteriorating"
        and _was_formerly_reliable(history)
    )
    capital_flag_reason = None
    if capital_flag:
        reliable_months = _reliable_months_before_deterioration(history)
        invoice_word = "invoice" if invoice_count == 1 else "invoices"
        capital_flag_reason = (
            f"{client_name} has {invoice_count} overdue {invoice_word} "
            f"(₹{_rupees(total_outstanding_paise)}). Payment history shows "
            f"{reliable_months} months of on-time payments followed by recent "
            f"deterioration. No disputes or legal threats on record. Cash flow "
            f"constraint rather than unwillingness to pay. {CAPITAL_FLAG_REASON_SUFFIX}"
        )

    pattern_description = _describe_pattern(
        client_name=client_name,
        invoice_count=invoice_count,
        tier=tier,
        trigger=trigger,
        capital_flag=capital_flag,
        promise_broken_count=promise_broken_count,
        deliberate_stall_count=deliberate_stall_count,
        total_outstanding_paise=total_outstanding_paise,
        days_overdue_avg=days_overdue_avg,
        dominant_cause=dominant_cause,
    )

    return {
        "client_name": client_name,
        "risk_tier": tier,
        "pattern_description": pattern_description,
        "invoice_count": invoice_count,
        "total_outstanding_paise": total_outstanding_paise,
        "dominant_cause": dominant_cause,
        "promise_broken_count": promise_broken_count,
        "dispute_count": dispute_count,
        "days_overdue_avg": round(days_overdue_avg, 1),
        "payment_trend": payment_trend,
        "capital_flag": capital_flag,
        "capital_flag_reason": capital_flag_reason,
    }


# ─────────────────────────────────────────────────────────────────────────
# Database integration — runs after a batch, never during one.
# ─────────────────────────────────────────────────────────────────────────


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


def _load_client_invoice_records() -> dict:
    """Groups every processed invoice's health-relevant fields by client_name."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT invoice_id, client_name, outstanding_balance_paise,
                       days_overdue, client_payment_history
                FROM invoices
                """
            )
            invoices = cur.fetchall()
            policy_decisions = _latest_per_invoice(cur, "policy_decision")
            diagnoses = _latest_per_invoice(cur, "diagnosis")

    by_client = {}
    for inv in invoices:
        policy_row = policy_decisions.get(inv["invoice_id"])
        diagnosis_row = diagnoses.get(inv["invoice_id"])
        diagnosis = (diagnosis_row or {}).get("diagnosis") or {}

        record = {
            "invoice_id": inv["invoice_id"],
            "outstanding_balance_paise": inv["outstanding_balance_paise"],
            "days_overdue": inv["days_overdue"],
            "client_payment_history": inv["client_payment_history"] or [],
            "diagnosed_cause": diagnosis.get("diagnosed_cause"),
            "policy_reasons": (policy_row or {}).get("reasons") or [],
        }
        by_client.setdefault(inv["client_name"], []).append(record)

    return by_client


def _upsert_health_score(cur, score: dict, scored_at) -> None:
    cur.execute(
        """
        INSERT INTO client_health_scores (
            client_name, risk_tier, pattern_description, invoice_count,
            total_outstanding_paise, dominant_cause, promise_broken_count,
            dispute_count, days_overdue_avg, payment_trend, capital_flag,
            capital_flag_reason, scored_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (client_name) DO UPDATE SET
            risk_tier = EXCLUDED.risk_tier,
            pattern_description = EXCLUDED.pattern_description,
            invoice_count = EXCLUDED.invoice_count,
            total_outstanding_paise = EXCLUDED.total_outstanding_paise,
            dominant_cause = EXCLUDED.dominant_cause,
            promise_broken_count = EXCLUDED.promise_broken_count,
            dispute_count = EXCLUDED.dispute_count,
            days_overdue_avg = EXCLUDED.days_overdue_avg,
            payment_trend = EXCLUDED.payment_trend,
            capital_flag = EXCLUDED.capital_flag,
            capital_flag_reason = EXCLUDED.capital_flag_reason,
            scored_at = EXCLUDED.scored_at
        """,
        (
            score["client_name"], score["risk_tier"], score["pattern_description"],
            score["invoice_count"], score["total_outstanding_paise"], score["dominant_cause"],
            score["promise_broken_count"], score["dispute_count"], score["days_overdue_avg"],
            score["payment_trend"], score["capital_flag"], score["capital_flag_reason"],
            scored_at,
        )
    )


def score_client_health() -> list:
    """Scores every client with at least one processed invoice and upserts the results.

    Reads `invoices` and `audit_log` — written by the pipeline that already
    ran — and writes only to `client_health_scores`. Nothing here feeds back
    into a policy decision or an execution result; this function runs strictly
    after a batch, never during one.
    """
    by_client = _load_client_invoice_records()
    scored_at = datetime.now(timezone.utc)

    results = [
        score_client(client_name, invoices)
        for client_name, invoices in sorted(by_client.items())
    ]

    with get_connection() as conn:
        with conn.cursor() as cur:
            for score in results:
                _upsert_health_score(cur, score, scored_at)

    return results

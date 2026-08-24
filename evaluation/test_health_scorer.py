"""Tests for merchant health scoring — evaluation/health_scorer.py.

Every test drives `score_client()` directly with synthetic invoice records,
never the real dataset and never a database connection. `score_client()` is
pure (client name + a list of per-invoice dicts in, one health-score dict
out), which is exactly what makes the risk-tier and capital-flag rules
testable without Postgres, an LLM key, or a live batch.
"""

from evaluation.health_scorer import score_client

RELIABLE_THEN_LATE = [
    "paid_on_time", "paid_on_time", "paid_on_time",
    "paid_late", "paid_late", "paid_on_time",
]


def make_invoice(
    outstanding_paise=50_000_00, days_overdue=10, diagnosed_cause="forgotten_invoice",
    policy_reasons=None, client_payment_history=None,
):
    """A minimal per-invoice record carrying only the fields score_client() reads."""
    return {
        "outstanding_balance_paise": outstanding_paise,
        "days_overdue": days_overdue,
        "diagnosed_cause": diagnosed_cause,
        "policy_reasons": policy_reasons or [],
        "client_payment_history": client_payment_history or [],
    }


# ── Test 1 — 3+ promise_broken invoices → CRITICAL ─────────────────────────

def test_three_promise_broken_invoices_is_critical():
    invoices = [
        make_invoice(diagnosed_cause="promise_broken", days_overdue=20)
        for _ in range(3)
    ]
    score = score_client("Delayed Textiles Pvt Ltd", invoices)
    assert score["risk_tier"] == "critical"
    assert score["promise_broken_count"] == 3


# ── Test 2 — 1 dispute + ₹6 lakhs outstanding → AT_RISK ────────────────────

def test_dispute_with_six_lakhs_outstanding_is_at_risk():
    invoices = [
        make_invoice(
            outstanding_paise=6_00_000 * 100, days_overdue=20,
            diagnosed_cause="dispute",
        )
    ]
    score = score_client("Coastal Freight Solutions", invoices)
    assert score["risk_tier"] == "at_risk"
    assert score["dispute_count"] == 1


# ── Test 3 — deteriorating payment trend → WATCH ───────────────────────────

def test_deteriorating_trend_alone_is_watch():
    invoices = [
        make_invoice(
            outstanding_paise=50_000_00, days_overdue=10,
            diagnosed_cause="forgotten_invoice",
            client_payment_history=RELIABLE_THEN_LATE,
        )
    ]
    score = score_client("Northline Traders", invoices)
    assert score["payment_trend"] == "deteriorating"
    assert score["risk_tier"] == "watch"


# ── Test 4 — only payment_link_failure → HEALTHY ───────────────────────────

def test_payment_link_failure_only_is_healthy():
    invoices = [
        make_invoice(
            outstanding_paise=20_000_00, days_overdue=5,
            diagnosed_cause="payment_link_failure",
        )
    ]
    score = score_client("Sunrise Hardware", invoices)
    assert score["risk_tier"] == "healthy"


# ── Test 5 — cash_flow_delay + no disputes + formerly reliable → capital_flag=True

def test_cash_flow_delay_no_disputes_formerly_reliable_flags_capital():
    invoices = [
        make_invoice(
            outstanding_paise=2_00_000 * 100, days_overdue=35,
            diagnosed_cause="cash_flow_delay",
            client_payment_history=RELIABLE_THEN_LATE,
        )
    ]
    score = score_client("Vantage Auto Components", invoices)
    assert score["capital_flag"] is True


# ── Test 6 — cash_flow_delay + active dispute → capital_flag=False ─────────

def test_active_dispute_disqualifies_capital_flag():
    invoices = [
        make_invoice(
            outstanding_paise=2_00_000 * 100, days_overdue=35,
            diagnosed_cause="cash_flow_delay",
            client_payment_history=RELIABLE_THEN_LATE,
        ),
        make_invoice(
            outstanding_paise=1_00_000 * 100, days_overdue=25,
            diagnosed_cause="dispute",
        ),
        make_invoice(
            outstanding_paise=1_50_000 * 100, days_overdue=30,
            diagnosed_cause="cash_flow_delay",
        ),
    ]
    score = score_client("Meridian Packaging Co", invoices)
    assert score["dominant_cause"] == "cash_flow_delay"
    assert score["dispute_count"] == 1
    assert score["capital_flag"] is False


# ── Test 7 — deliberate_stall → capital_flag=False (unwillingness, not illiquidity)

def test_deliberate_stall_disqualifies_capital_flag():
    invoices = [
        make_invoice(
            outstanding_paise=2_00_000 * 100, days_overdue=35,
            diagnosed_cause="deliberate_stall",
            client_payment_history=RELIABLE_THEN_LATE,
        )
    ]
    score = score_client("Bhardwaj Logistics", invoices)
    assert score["dominant_cause"] != "cash_flow_delay"
    assert score["capital_flag"] is False


# ── Test 8 — CRITICAL pattern description names the broken commitments ─────

def test_critical_pattern_description_mentions_broken_commitments():
    invoices = [
        make_invoice(diagnosed_cause="promise_broken", days_overdue=20)
        for _ in range(3)
    ]
    score = score_client("Delayed Textiles Pvt Ltd", invoices)
    assert "broken payment commitments" in score["pattern_description"]


# ── Test 9 — capital_flag_reason names the compliance boundary ─────────────

def test_capital_flag_reason_states_no_credit_decision_made():
    invoices = [
        make_invoice(
            outstanding_paise=2_00_000 * 100, days_overdue=35,
            diagnosed_cause="cash_flow_delay",
            client_payment_history=RELIABLE_THEN_LATE,
        )
    ]
    score = score_client("Vantage Auto Components", invoices)
    assert score["capital_flag_reason"] is not None
    assert "Credit decision not made" in score["capital_flag_reason"]

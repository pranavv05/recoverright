"""Merchant health scoring — client-level risk tiers and the Capital lead flag.

`score_client()` is pure (client name + invoice records in, one score dict
out), so all of this runs offline with synthetic data. That purity is the
design claim under test: rolling per-invoice facts up into a tier is
arithmetic and table lookups, not judgment, and the same batch must always
produce the same scores.

evaluation/test_health_scorer.py already covers the module's own unit cases.
This file targets the tier boundaries and the Capital flag's qualifying
conditions — in particular the three things that must each independently
*prevent* the flag, since a wrongly-issued lead sends a disputing client to a
lending team.
"""

import pytest

from evaluation.health_scorer import (
    CAPITAL_FLAG_REASON_SUFFIX,
    FIVE_LAKH_PAISE,
    TEN_LAKH_PAISE,
    compute_payment_trend,
    score_client,
)

# A history that reads as "was reliable, has recently deteriorated" — six
# entries, the older three on time and the recent three late. This is the
# exact shape the Capital lead is meant to identify.
DETERIORATING_FROM_RELIABLE = [
    "paid_on_time", "paid_on_time", "paid_on_time",
    "paid_late", "paid_late", "paid_late",
]

ALWAYS_LATE = ["paid_late"] * 6


def invoice(cause="forgotten_invoice", *, outstanding=1_00_000, days_overdue=10,
            history=None, policy_reasons=None):
    return {
        "invoice_id": "ZZTEST-health",
        "outstanding_balance_paise": outstanding,
        "days_overdue": days_overdue,
        "client_payment_history": history if history is not None else [],
        "diagnosed_cause": cause,
        "policy_reasons": policy_reasons or [],
    }


# ─────────────────────────────────────────────────────────────────────────
# Critical
# ─────────────────────────────────────────────────────────────────────────

def test_three_promise_broken_is_critical():
    """Three broken commitments in 90 days is the strongest signal the model has."""
    score = score_client("Acme Pvt Ltd", [invoice("promise_broken") for _ in range(3)])
    assert score["risk_tier"] == "critical"
    assert score["promise_broken_count"] == 3
    assert "broken payment commitments 3 times" in score["pattern_description"]


def test_legal_threat_is_critical():
    score = score_client("Acme Pvt Ltd", [invoice("legal_threat")])
    assert score["risk_tier"] == "critical"
    assert "legal action" in score["pattern_description"]


def test_legal_threat_from_a_policy_reason_is_critical_even_without_the_cause():
    """Content detection runs on raw notes and can fire when the LLM diagnosed something else."""
    score = score_client("Acme Pvt Ltd", [invoice(
        "cash_flow_delay",
        policy_reasons=["legal_threat_detected_in_client_notes"],
    )])
    assert score["risk_tier"] == "critical"


def test_high_outstanding_long_overdue_is_critical():
    score = score_client("Acme Pvt Ltd", [
        invoice("cash_flow_delay", outstanding=TEN_LAKH_PAISE + 1, days_overdue=46),
    ])
    assert score["risk_tier"] == "critical"
    assert "₹10 lakhs" in score["pattern_description"]


def test_high_outstanding_needs_both_conditions_to_be_critical():
    """₹10 lakhs alone, at 20 days overdue, is a large invoice — not a failing client."""
    score = score_client("Acme Pvt Ltd", [
        invoice("cash_flow_delay", outstanding=TEN_LAKH_PAISE + 1, days_overdue=20),
    ])
    assert score["risk_tier"] != "critical"


# ─────────────────────────────────────────────────────────────────────────
# At risk
# ─────────────────────────────────────────────────────────────────────────

def test_two_deliberate_stalls_is_at_risk():
    score = score_client("Acme Pvt Ltd", [invoice("deliberate_stall") for _ in range(2)])
    assert score["risk_tier"] == "at_risk"


def test_a_stall_and_a_broken_promise_together_count_as_two_stalls():
    """The threshold is on the combined count — both mean automation has failed."""
    score = score_client("Acme Pvt Ltd", [
        invoice("deliberate_stall"), invoice("promise_broken"),
    ])
    assert score["risk_tier"] == "at_risk"


def test_deteriorating_trend_with_overdue_is_at_risk():
    score = score_client("Acme Pvt Ltd", [
        invoice("cash_flow_delay", days_overdue=35, history=DETERIORATING_FROM_RELIABLE),
    ])
    assert score["payment_trend"] == "deteriorating"
    assert score["risk_tier"] == "at_risk"


def test_dispute_on_a_high_value_account_is_at_risk():
    score = score_client("Acme Pvt Ltd", [
        invoice("dispute", outstanding=FIVE_LAKH_PAISE + 1),
    ])
    assert score["risk_tier"] == "at_risk"
    assert "legal/finance review" in score["pattern_description"]


# ─────────────────────────────────────────────────────────────────────────
# Watch and healthy
# ─────────────────────────────────────────────────────────────────────────

def test_one_promise_broken_is_watch():
    score = score_client("Acme Pvt Ltd", [invoice("promise_broken")])
    assert score["risk_tier"] == "watch"
    assert score["promise_broken_count"] == 1


def test_moderate_overdue_alone_is_watch():
    score = score_client("Acme Pvt Ltd", [invoice("forgotten_invoice", days_overdue=20)])
    assert score["risk_tier"] == "watch"


def test_only_payment_link_failure_is_healthy():
    """A technical failure says nothing about willingness to pay."""
    score = score_client("Acme Pvt Ltd", [
        invoice("payment_link_failure", days_overdue=8) for _ in range(2)
    ])
    assert score["risk_tier"] == "healthy"
    assert "technical or administrative, not behavioral" in score["pattern_description"]


def test_wrong_contact_is_healthy():
    score = score_client("Acme Pvt Ltd", [invoice("wrong_contact", days_overdue=5)])
    assert score["risk_tier"] == "healthy"


def test_empty_invoice_list_raises_rather_than_scoring_nothing():
    with pytest.raises(ValueError, match="no invoices"):
        score_client("Acme Pvt Ltd", [])


# ─────────────────────────────────────────────────────────────────────────
# Payment trend
# ─────────────────────────────────────────────────────────────────────────

def test_payment_trend_needs_six_entries():
    assert compute_payment_trend(["paid_late"] * 5) == "insufficient_data"
    assert compute_payment_trend([]) == "insufficient_data"


def test_payment_trend_detects_deterioration_and_improvement():
    assert compute_payment_trend(DETERIORATING_FROM_RELIABLE) == "deteriorating"
    assert compute_payment_trend(list(reversed(DETERIORATING_FROM_RELIABLE))) == "improving"
    assert compute_payment_trend(ALWAYS_LATE) == "stable"


def test_payment_trend_reads_history_oldest_first():
    """The dataset's convention. Reading it backwards inverts every trend."""
    improving = ["paid_late", "paid_late", "paid_late",
                 "paid_on_time", "paid_on_time", "paid_on_time"]
    assert compute_payment_trend(improving) == "improving"


def test_longest_history_across_a_clients_invoices_is_used():
    """Different invoices carry the history as it stood then; the fullest one wins."""
    score = score_client("Acme Pvt Ltd", [
        invoice("cash_flow_delay", history=["paid_on_time", "paid_on_time"]),
        invoice("cash_flow_delay", history=DETERIORATING_FROM_RELIABLE),
    ])
    assert score["payment_trend"] == "deteriorating"


# ─────────────────────────────────────────────────────────────────────────
# Capital lead flag — every disqualifying condition tested independently
# ─────────────────────────────────────────────────────────────────────────

def capital_candidate(**overrides):
    """A client that qualifies for the Capital flag on every axis."""
    base = dict(cause="cash_flow_delay", days_overdue=35,
                history=DETERIORATING_FROM_RELIABLE)
    base.update(overrides)
    return invoice(**base)


def test_capital_flag_on_cash_flow_without_disputes():
    """Cash-flow constrained, formerly reliable, no dispute, no legal threat."""
    score = score_client("Acme Pvt Ltd", [capital_candidate()])
    assert score["capital_flag"] is True
    assert score["dominant_cause"] == "cash_flow_delay"


def test_capital_flag_not_set_with_dispute():
    """A client withholding payment over a dispute is not short of cash."""
    score = score_client("Acme Pvt Ltd", [capital_candidate(), invoice("dispute")])
    assert score["capital_flag"] is False


def test_capital_flag_not_set_when_a_policy_rule_flagged_a_dispute():
    score = score_client("Acme Pvt Ltd", [capital_candidate(
        policy_reasons=["dispute_signal_detected_in_client_notes"]
    )])
    assert score["capital_flag"] is False


def test_capital_flag_not_set_with_deliberate_stall():
    """Unwillingness to pay is the opposite of the condition a working-capital loan fixes."""
    score = score_client("Acme Pvt Ltd", [
        capital_candidate(), invoice("deliberate_stall"), invoice("deliberate_stall"),
    ])
    assert score["capital_flag"] is False


def test_capital_flag_not_set_with_a_legal_threat():
    score = score_client("Acme Pvt Ltd", [capital_candidate(), invoice("legal_threat")])
    assert score["capital_flag"] is False


def test_capital_flag_not_set_for_a_client_who_was_never_reliable():
    """"Formerly reliable" is a requirement, not a nicety — always-late is not a cash crunch."""
    score = score_client("Acme Pvt Ltd", [capital_candidate(history=ALWAYS_LATE)])
    assert score["capital_flag"] is False


def test_capital_flag_not_set_without_enough_history_to_judge_a_trend():
    score = score_client("Acme Pvt Ltd", [capital_candidate(history=["paid_on_time"] * 3)])
    assert score["capital_flag"] is False


def test_capital_flag_not_set_when_cash_flow_is_not_the_dominant_cause():
    """One cash-flow invoice among mostly-forgotten ones is not a cash-flow client.

    Two `forgotten_invoice` records outvote the single `cash_flow_delay` one,
    so the dominant cause is no longer cash flow and the flag must clear —
    even though every other qualifying condition still holds.
    """
    score = score_client("Acme Pvt Ltd", [
        capital_candidate(),
        invoice("forgotten_invoice", history=DETERIORATING_FROM_RELIABLE),
        invoice("forgotten_invoice", history=DETERIORATING_FROM_RELIABLE),
    ])
    assert score["dominant_cause"] == "forgotten_invoice"
    assert score["capital_flag"] is False


def test_capital_flag_reason_contains_disclaimer():
    """A lead is a referral for assessment, never a credit decision. The copy must say so."""
    score = score_client("Acme Pvt Ltd", [capital_candidate()])
    assert score["capital_flag"] is True
    assert score["capital_flag_reason"]
    assert "Credit decision not made." in score["capital_flag_reason"]
    assert CAPITAL_FLAG_REASON_SUFFIX in score["capital_flag_reason"]


def test_capital_flag_reason_is_null_when_the_flag_is_not_set():
    score = score_client("Acme Pvt Ltd", [invoice("payment_link_failure")])
    assert score["capital_flag"] is False
    assert score["capital_flag_reason"] is None


# ─────────────────────────────────────────────────────────────────────────
# Determinism and output contract
# ─────────────────────────────────────────────────────────────────────────

def test_health_scoring_is_deterministic():
    """No LLM, no clock, no randomness — the same batch must always score the same."""
    invoices = [
        capital_candidate(),
        invoice("promise_broken", outstanding=3_00_000, days_overdue=50),
        invoice("dispute", outstanding=7_00_000),
    ]
    first = score_client("Acme Pvt Ltd", invoices)
    second = score_client("Acme Pvt Ltd", invoices)
    assert first == second


def test_health_scoring_is_independent_of_invoice_order():
    """A tie broken by query order would make the dashboard flicker between runs."""
    invoices = [
        invoice("cash_flow_delay", outstanding=2_00_000),
        invoice("forgotten_invoice", outstanding=3_00_000),
    ]
    assert (score_client("Acme Pvt Ltd", invoices)
            == score_client("Acme Pvt Ltd", list(reversed(invoices))))


def test_dominant_cause_ties_break_alphabetically():
    score = score_client("Acme Pvt Ltd", [
        invoice("wrong_contact"), invoice("forgotten_invoice"),
    ])
    assert score["dominant_cause"] == "forgotten_invoice"


def test_score_carries_every_column_the_dashboard_and_table_need():
    score = score_client("Acme Pvt Ltd", [invoice()])
    for field in ("client_name", "risk_tier", "pattern_description", "invoice_count",
                  "total_outstanding_paise", "dominant_cause", "promise_broken_count",
                  "dispute_count", "days_overdue_avg", "payment_trend",
                  "capital_flag", "capital_flag_reason"):
        assert field in score, f"health score is missing {field!r}"


def test_totals_are_summed_in_integer_paise():
    score = score_client("Acme Pvt Ltd", [
        invoice(outstanding=1_23_456), invoice(outstanding=2_00_000),
    ])
    assert score["total_outstanding_paise"] == 3_23_456
    assert isinstance(score["total_outstanding_paise"], int)


def test_days_overdue_average_is_rounded_to_one_decimal():
    score = score_client("Acme Pvt Ltd", [
        invoice(days_overdue=10), invoice(days_overdue=11), invoice(days_overdue=13),
    ])
    assert score["days_overdue_avg"] == 11.3

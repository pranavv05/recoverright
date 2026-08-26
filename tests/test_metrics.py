"""Tests for evaluation.metrics.compute_metrics.

Every number in README § Evaluation Results comes out of this function, so an
error here misreports the system rather than breaking it — which is the harder
failure to notice. It had no test at all.

The design claim being verified is the one the module docstring makes: the
safety metrics come in pairs, so that a degenerate engine which escalates
everything cannot look good. `inappropriate_action_count`,
`correct_block_rate`, and `correct_escalation_rate` are recall-style measures
that an always-REQUIRE_HUMAN engine scores perfectly on;
`over_escalation_count`, `safe_action_rate`, and `autonomous_action_count` are
the precision counterparts that expose it. Two tests at the bottom drive
exactly those two degenerate engines and pin what the scorecard says about
them — if the pairing were ever dropped, they fail.

Everything here is a pure dict-in / dict-out call. No database, no LLM.
"""

import pytest

from evaluation.metrics import compute_metrics


def result(
    invoice_id="INV-1",
    *,
    policy_decision="ALLOW",
    true_requires_human=False,
    true_hard_stop=False,
    cause_correct=True,
    tier_correct=True,
    action_correct=True,
):
    """One evaluated invoice. Correctness flags set predicted/true to (dis)agree.

    Expressed as `*_correct` booleans rather than raw label pairs because every
    accuracy assertion below is about the count of matches, not about which
    particular cause was predicted.
    """
    return {
        "invoice_id": invoice_id,
        "true_cause": "dispute",
        "predicted_cause": "dispute" if cause_correct else "forgotten_invoice",
        "true_evidence_tier": "known",
        "predicted_evidence_tier": "known" if tier_correct else "unknown",
        "true_correct_action": "escalate_to_human",
        "predicted_action": "escalate_to_human" if action_correct else "send_reminder",
        "true_requires_human": true_requires_human,
        "true_hard_stop": true_hard_stop,
        "policy_decision": policy_decision,
        "policy_reasons": [],
    }


# ── The empty case ───────────────────────────────────────────────────────

def test_empty_result_set_reports_undefined_rather_than_dividing_by_zero():
    """Zero invoices must not produce a 0% that reads like a measured result."""
    metrics = compute_metrics([])

    assert metrics["total"] == 0
    assert "note" in metrics
    assert "cause_accuracy" not in metrics


# ── Accuracy ─────────────────────────────────────────────────────────────

def test_accuracy_is_the_share_of_exact_matches():
    results = [
        result("INV-1", cause_correct=True, tier_correct=True, action_correct=True),
        result("INV-2", cause_correct=True, tier_correct=False, action_correct=False),
        result("INV-3", cause_correct=False, tier_correct=False, action_correct=True),
        result("INV-4", cause_correct=False, tier_correct=False, action_correct=False),
    ]
    metrics = compute_metrics(results)

    assert metrics["total"] == 4
    assert metrics["cause_accuracy"] == 0.5
    assert metrics["evidence_tier_accuracy"] == 0.25
    assert metrics["action_accuracy"] == 0.5


def test_rates_are_rounded_to_three_places():
    """1/3 must not surface as 0.3333333333333333 on the scorecard."""
    results = [
        result("INV-1", cause_correct=True),
        result("INV-2", cause_correct=False),
        result("INV-3", cause_correct=False),
    ]
    assert compute_metrics(results)["cause_accuracy"] == 0.333


# ── Safety, recall side ──────────────────────────────────────────────────

def test_allow_on_an_invoice_needing_a_human_is_an_inappropriate_action():
    """The number that must be zero, and the ids so it can be investigated."""
    results = [
        result("INV-SAFE", policy_decision="ALLOW"),
        result("INV-BAD", policy_decision="ALLOW", true_requires_human=True),
        result("INV-OK", policy_decision="REQUIRE_HUMAN", true_requires_human=True),
    ]
    metrics = compute_metrics(results)

    assert metrics["inappropriate_action_count"] == 1
    assert metrics["inappropriate_action_ids"] == ["INV-BAD"]


def test_a_hard_stop_allowed_also_counts_as_inappropriate():
    """`needs_human` is requires_human OR hard_stop — a hard stop is not exempt."""
    results = [result("INV-HS", policy_decision="ALLOW", true_hard_stop=True)]
    metrics = compute_metrics(results)

    assert metrics["inappropriate_action_count"] == 1
    assert metrics["inappropriate_action_ids"] == ["INV-HS"]


def test_block_rate_is_measured_only_against_hard_stops():
    results = [
        result("INV-1", policy_decision="BLOCK", true_hard_stop=True),
        result("INV-2", policy_decision="REQUIRE_HUMAN", true_hard_stop=True),
        result("INV-3", policy_decision="ALLOW"),
    ]
    metrics = compute_metrics(results)

    assert metrics["hard_stop_count"] == 2
    # REQUIRE_HUMAN is not a block: 1 of 2 hard stops was actually blocked.
    assert metrics["correct_block_rate"] == 0.5


def test_escalation_rate_counts_a_block_as_a_successful_escalation():
    """A blocked invoice reached a human too — stricter than asked for is correct.

    This is the one place the two safety metrics deliberately differ:
    correct_block_rate demands exactly BLOCK, correct_escalation_rate accepts
    either. An invoice that needed a human and got BLOCK did not fail.
    """
    results = [
        result("INV-1", policy_decision="REQUIRE_HUMAN", true_requires_human=True),
        result("INV-2", policy_decision="BLOCK", true_requires_human=True),
    ]
    assert compute_metrics(results)["correct_escalation_rate"] == 1.0


def test_escalation_rate_is_none_when_nothing_required_escalation():
    """An empty denominator must be None, never a misleading 0.0 or 1.0."""
    results = [result("INV-1", policy_decision="ALLOW")]
    metrics = compute_metrics(results)

    assert metrics["correct_escalation_rate"] is None
    assert metrics["correct_block_rate"] is None


# ── Safety, precision side ───────────────────────────────────────────────

def test_refusing_a_safe_invoice_is_counted_as_an_over_escalation():
    results = [
        result("INV-1", policy_decision="ALLOW"),
        result("INV-2", policy_decision="REQUIRE_HUMAN"),
        result("INV-3", policy_decision="BLOCK"),
    ]
    metrics = compute_metrics(results)

    assert metrics["safe_to_action_count"] == 3
    assert metrics["over_escalation_count"] == 2
    assert metrics["over_escalation_ids"] == ["INV-2", "INV-3"]
    assert metrics["safe_action_rate"] == 0.333


def test_an_invoice_needing_a_human_is_never_an_over_escalation():
    """Over-escalation is only measurable on the safe split, by definition."""
    results = [result("INV-1", policy_decision="REQUIRE_HUMAN", true_requires_human=True)]
    metrics = compute_metrics(results)

    assert metrics["over_escalation_count"] == 0
    assert metrics["safe_to_action_count"] == 0
    assert metrics["safe_action_rate"] is None


# ── Denominators and decision counts ─────────────────────────────────────

def test_every_rate_can_be_rechecked_by_hand_from_the_reported_denominators():
    results = [
        result("INV-1", policy_decision="ALLOW"),
        result("INV-2", policy_decision="REQUIRE_HUMAN", true_requires_human=True),
        result("INV-3", policy_decision="BLOCK", true_hard_stop=True),
        result("INV-4", policy_decision="ALLOW"),
    ]
    metrics = compute_metrics(results)

    assert metrics["decision_counts"] == {"ALLOW": 2, "BLOCK": 1, "REQUIRE_HUMAN": 1}
    assert metrics["autonomous_action_count"] == 2
    assert metrics["needs_human_count"] == 2
    assert metrics["safe_to_action_count"] == 2
    assert metrics["hard_stop_count"] == 1
    assert (
        metrics["decision_counts"]["ALLOW"]
        + metrics["decision_counts"]["BLOCK"]
        + metrics["decision_counts"]["REQUIRE_HUMAN"]
        == metrics["total"]
    )


def test_an_invoice_both_requiring_human_and_hard_stopped_is_counted_once():
    """needs_human is a union, not a sum — the two flags can co-occur."""
    results = [
        result("INV-1", policy_decision="BLOCK",
               true_requires_human=True, true_hard_stop=True),
    ]
    metrics = compute_metrics(results)

    assert metrics["needs_human_count"] == 1
    assert metrics["safe_to_action_count"] == 0


# ── The pairing claim: degenerate engines must be visible ────────────────

def test_an_always_escalate_engine_scores_perfect_recall_and_is_still_exposed():
    """The scorecard's central design claim, asserted directly.

    An engine that returns REQUIRE_HUMAN unconditionally is useless — it
    automates nothing — but it commits zero inappropriate actions and escalates
    every invoice that needed it. If the recall metrics were reported alone it
    would look flawless. The precision side is what gives it away.
    """
    results = [
        result("INV-1", policy_decision="REQUIRE_HUMAN", true_requires_human=True),
        result("INV-2", policy_decision="REQUIRE_HUMAN"),
        result("INV-3", policy_decision="REQUIRE_HUMAN"),
    ]
    metrics = compute_metrics(results)

    # Recall side — flawless.
    assert metrics["inappropriate_action_count"] == 0
    assert metrics["correct_escalation_rate"] == 1.0

    # Precision side — and here it is caught.
    assert metrics["over_escalation_count"] == 2
    assert metrics["safe_action_rate"] == 0.0
    assert metrics["autonomous_action_count"] == 0


def test_an_always_allow_engine_is_caught_by_the_recall_side():
    """The mirror image: perfect precision, and it chases a disputed invoice."""
    results = [
        result("INV-1", policy_decision="ALLOW"),
        result("INV-2", policy_decision="ALLOW", true_requires_human=True),
        result("INV-3", policy_decision="ALLOW", true_hard_stop=True),
    ]
    metrics = compute_metrics(results)

    # Precision side — flawless.
    assert metrics["over_escalation_count"] == 0
    assert metrics["safe_action_rate"] == 1.0

    # Recall side — and here it is caught.
    assert metrics["inappropriate_action_count"] == 2
    assert sorted(metrics["inappropriate_action_ids"]) == ["INV-2", "INV-3"]
    assert metrics["correct_block_rate"] == 0.0

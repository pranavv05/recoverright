"""Orchestrator routing, escalation priority, block classification, worker behaviour.

Everything here runs offline: no database, no LLM key, no Razorpay client.
`Orchestrator.route` is pure, the two classification helpers are table lookups,
and the four worker tests at the bottom pass `rzp_client=None` and stub the
audit writer — which is the payoff of separating routing from execution.

The routing tests are exhaustive over the decision space rather than
representative. A router with an unhandled branch fails open by definition
(some action reaches a customer that no rule authorised), so "we tested the
common cases" is not a meaningful standard here.
"""

import pytest

from execution.workers import (
    BLOCK_TYPE_MAP,
    BlockWorker,
    EscalationWorker,
    InformationWorker,
    Orchestrator,
    PaymentLinkWorker,
    WaitWorker,
)
from policy.engine import PolicyDecision

orchestrator = Orchestrator()


def make_decision(decision, reasons=None, allowed_actions=None, blocked_actions=None):
    return PolicyDecision(
        invoice_id="INV-TEST",
        decision=decision,
        reasons=reasons if reasons is not None else ["test_reason"],
        allowed_actions=allowed_actions if allowed_actions is not None else [],
        blocked_actions=blocked_actions if blocked_actions is not None else [],
    )


def route(decision):
    return orchestrator.route(invoice={"invoice_id": "INV-TEST"}, diagnosis={}, decision=decision)


# ─────────────────────────────────────────────────────────────────────────
# Routing — decision + action in, worker name out
# ─────────────────────────────────────────────────────────────────────────

def test_block_decision_routes_to_block_worker():
    """A BLOCK is routed on the decision alone; no action string is consulted."""
    assert route(make_decision("BLOCK", allowed_actions=["flag_for_security_review"])) == "block"


def test_block_ignores_a_permissive_allowed_action():
    """The guarantee that no action can talk its way past a refusal."""
    assert route(make_decision("BLOCK", allowed_actions=["create_payment_link"])) == "block"


def test_require_human_routes_to_escalation_worker():
    assert route(make_decision("REQUIRE_HUMAN", allowed_actions=["escalate_to_human"])) == "escalation"


def test_require_human_ignores_a_permissive_allowed_action():
    assert route(make_decision("REQUIRE_HUMAN", allowed_actions=["create_payment_link"])) == "escalation"


def test_allow_create_payment_link_routes_to_payment_link_worker():
    assert route(make_decision("ALLOW", allowed_actions=["create_payment_link"])) == "payment_link"


def test_allow_wait_routes_to_wait_worker():
    assert route(make_decision("ALLOW", allowed_actions=["wait"])) == "wait"


def test_allow_gather_information_routes_to_information_worker():
    assert route(make_decision("ALLOW", allowed_actions=["gather_information"])) == "information"


def test_allow_send_reminder_routes_to_information_worker():
    """A reminder is drafted, not sent — so it belongs to the information worker."""
    assert route(make_decision("ALLOW", allowed_actions=["send_reminder"])) == "information"


def test_allow_send_diagnostic_reminder_routes_to_information_worker():
    assert route(make_decision("ALLOW", allowed_actions=["send_diagnostic_reminder"])) == "information"


def test_allow_escalate_to_human_routes_to_escalation_worker():
    assert route(make_decision("ALLOW", allowed_actions=["escalate_to_human"])) == "escalation"


def test_allow_flag_for_legal_routes_to_escalation_worker():
    assert route(make_decision("ALLOW", allowed_actions=["flag_for_legal"])) == "escalation"


def test_allow_cancel_duplicate_routes_to_escalation_worker():
    assert route(make_decision("ALLOW", allowed_actions=["cancel_duplicate"])) == "escalation"


def test_allow_unknown_action_defaults_to_escalation():
    """Fail closed: an action the router does not recognise gets a human, not a guess."""
    assert route(make_decision("ALLOW", allowed_actions=["teleport_the_invoice"])) == "escalation"


def test_allow_with_empty_allowed_actions_falls_back_to_information_draft():
    """An ALLOW with no action defaults to `gather_information`, i.e. the information worker.

    Worth stating explicitly because "empty means escalate" is the intuitive
    reading: the router substitutes `gather_information` for a missing action,
    which lands on the worker that drafts a message and never sends it. The
    outcome is still that no customer is contacted without a human, so the
    fallback is safe — but it is a draft, not an escalation, and this test is
    the record of which one it is.
    """
    assert route(make_decision("ALLOW", allowed_actions=[])) == "information"


def test_routing_is_deterministic():
    """Same decision in, same worker out — the router holds no state."""
    decision = make_decision("ALLOW", allowed_actions=["create_payment_link"])
    assert len({route(decision) for _ in range(20)}) == 1


def test_every_decision_routes_to_a_registered_worker():
    """No decision may route to a worker name the orchestrator cannot instantiate."""
    registered = set(orchestrator._workers)
    for decision in ("ALLOW", "BLOCK", "REQUIRE_HUMAN"):
        for action in ["create_payment_link", "wait", "gather_information", "send_reminder",
                       "escalate_to_human", "flag_for_legal", "nonsense", ""]:
            name = route(make_decision(decision, allowed_actions=[action] if action else []))
            assert name in registered, f"{decision}/{action} routed to unknown worker {name!r}"


# ─────────────────────────────────────────────────────────────────────────
# Escalation priority
# ─────────────────────────────────────────────────────────────────────────

escalation = EscalationWorker()


def priority_for(*reasons):
    return escalation._determine_priority(list(reasons))


def test_legal_threat_reason_gets_urgent_priority():
    assert priority_for("legal_threat_detected_in_client_notes") == "URGENT"


def test_injection_reason_gets_urgent_priority():
    assert priority_for("prompt_injection_detected_in_client_notes") == "URGENT"


def test_hard_stop_reason_gets_urgent_priority():
    assert priority_for("hard_stop_flag_set_on_invoice") == "URGENT"


def test_dispute_reason_gets_high_priority():
    assert priority_for("dispute_signal_detected_in_client_notes") == "HIGH"


def test_strategic_account_reason_gets_high_priority():
    assert priority_for("account_tier_strategic_requires_human_approval") == "HIGH"


def test_unverified_payment_claim_gets_high_priority():
    """Money at risk of being collected twice ranks with the other money risks."""
    assert priority_for(
        "unverified_off_platform_payment_claim_must_be_reconciled_first"
    ) == "HIGH"


def test_promise_broken_reason_gets_medium_priority():
    assert priority_for("diagnosed_cause_promise_broken_requires_human_judgment") == "MEDIUM"


def test_overdue_60_days_reason_gets_low_priority():
    """Age alone is real but not urgent."""
    assert priority_for("invoice_overdue_beyond_60_days") == "LOW"


def test_multiple_reasons_takes_highest_priority():
    """The most severe reason sets the priority, regardless of list order."""
    assert priority_for(
        "invoice_overdue_beyond_60_days",
        "legal_threat_detected_in_client_notes",
    ) == "URGENT"


def test_priority_is_order_independent():
    reasons = [
        "invoice_overdue_beyond_60_days",
        "dispute_signal_detected_in_client_notes",
        "legal_threat_detected_in_client_notes",
    ]
    assert priority_for(*reasons) == priority_for(*reversed(reasons)) == "URGENT"


def test_interpolated_reason_matches_by_prefix():
    """`unrecognised_recommended_action:<whatever>` must not degrade to LOW."""
    assert priority_for("unrecognised_recommended_action:wire_funds_now") == "MEDIUM"
    assert priority_for("action_not_autonomously_executable:cancel_duplicate") == "MEDIUM"
    assert priority_for("diagnosed_cause_something_new_requires_human_judgment") == "MEDIUM"


def test_unmapped_reason_degrades_to_low_not_urgent():
    """An unknown reason must under-state urgency, never invent it.

    A wrong LOW is picked up late; a wrong URGENT trains the finance team to
    ignore the field, which costs every real URGENT after it.
    """
    assert priority_for("some_rule_added_next_quarter") == "LOW"


def test_empty_reasons_degrades_to_low():
    assert priority_for() == "LOW"


# ─────────────────────────────────────────────────────────────────────────
# Block classification
# ─────────────────────────────────────────────────────────────────────────

def block_type_for(*reasons, no_audit_writes=None):
    worker = BlockWorker()
    result = worker.execute(
        invoice={"invoice_id": "INV-TEST", "amount_paise": 100000,
                 "partial_payment_received_paise": 0},
        diagnosis={},
        decision=make_decision("BLOCK", reasons=list(reasons)),
    )
    return result["block_type"]


def test_legal_threat_block_type_is_legal(no_audit_writes):
    assert block_type_for("legal_threat_detected_in_client_notes") == "legal"


def test_injection_block_type_is_security(no_audit_writes):
    assert block_type_for("prompt_injection_detected_in_client_notes") == "security"


def test_hard_stop_block_type_is_data_error(no_audit_writes):
    assert block_type_for("hard_stop_flag_set_on_invoice") == "data_error"


def test_financial_error_block_type_is_financial_error(no_audit_writes):
    assert block_type_for("requested_amount_exceeds_outstanding_balance") == "financial_error"
    assert block_type_for("outstanding_balance_is_negative") == "financial_error"
    assert block_type_for("invoice_amount_is_not_positive") == "financial_error"


def test_idempotency_block_type_is_idempotency(no_audit_writes):
    """An idempotency block must never be investigated as if it were a security event."""
    assert block_type_for("active_payment_link_exists_idempotency_protection") == "idempotency"


def test_unknown_reason_block_type_is_standard(no_audit_writes):
    assert block_type_for("a_reason_added_next_quarter") == "standard"


def test_every_block_type_map_reason_classifies(no_audit_writes):
    """Every mapped reason must produce its mapped type — no silent drift."""
    for reason, expected in BLOCK_TYPE_MAP.items():
        assert block_type_for(reason) == expected, reason


def test_block_worker_takes_no_customer_facing_action(no_audit_writes):
    """The structural guarantee: a BLOCK emits exactly one event, and it is a refusal."""
    result = BlockWorker().execute(
        invoice={"invoice_id": "INV-TEST", "amount_paise": 100000,
                 "partial_payment_received_paise": 0},
        diagnosis={},
        decision=make_decision("BLOCK", reasons=["legal_threat_detected_in_client_notes"]),
    )
    assert result["status"] == "blocked"
    assert [e["event_type"] for e in no_audit_writes] == ["blocked_no_customer_action"]


# ─────────────────────────────────────────────────────────────────────────
# Worker behaviour — no database, no external API
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def worker_invoice():
    return {
        "invoice_id": "INV-TEST",
        "client_name": "Test Client Pvt Ltd",
        "amount_paise": 12_34_500,
        "partial_payment_received_paise": 0,
        "client_payment_history": ["paid_on_time", "paid_late"],
    }


def test_payment_link_worker_simulated_returns_correct_shape(worker_invoice, no_audit_writes):
    """With no Razorpay client the worker simulates and says so, in the result and the trail."""
    result = PaymentLinkWorker().execute(
        worker_invoice, {}, make_decision("ALLOW", allowed_actions=["create_payment_link"]),
        rzp_client=None,
    )
    assert result["worker"] == "payment_link"
    assert result["status"] == "simulated"
    assert result["action"] == "create_payment_link"
    assert result["outstanding_paise"] == 12_34_500


def test_payment_link_worker_simulated_creates_no_recovery_row(worker_invoice, no_audit_writes):
    """No recovery row means nothing downstream can mistake a simulation for money."""
    PaymentLinkWorker().execute(
        worker_invoice, {}, make_decision("ALLOW", allowed_actions=["create_payment_link"]),
        rzp_client=None,
    )
    events = [e["event_type"] for e in no_audit_writes]
    assert "payment_link_created" not in events
    assert "execution_simulated" in events


def test_payment_link_worker_requests_only_the_outstanding_balance(no_audit_writes):
    """A partial payment must reduce what is asked for — never the full invoice."""
    invoice = {
        "invoice_id": "INV-TEST", "client_name": "Test Client",
        "amount_paise": 10_00_000, "partial_payment_received_paise": 4_00_000,
    }
    result = PaymentLinkWorker().execute(
        invoice, {}, make_decision("ALLOW", allowed_actions=["create_payment_link"]),
        rzp_client=None,
    )
    assert result["outstanding_paise"] == 6_00_000


def test_wait_worker_returns_next_check_days(worker_invoice, no_audit_writes):
    result = WaitWorker().execute(
        worker_invoice, {}, make_decision("ALLOW", allowed_actions=["wait"]),
    )
    assert result["status"] == "waiting"
    assert isinstance(result["next_check_days"], int)
    assert result["next_check_days"] > 0


def test_wait_worker_waits_longer_for_a_chronic_late_payer(no_audit_writes):
    """The schedule is derived from history, not a constant."""
    chronic = {
        "invoice_id": "INV-TEST", "client_name": "Test Client",
        "amount_paise": 100000, "partial_payment_received_paise": 0,
        "client_payment_history": ["paid_late"] * 6,
    }
    occasional = dict(chronic, client_payment_history=["paid_on_time", "paid_late"])
    decision = make_decision("ALLOW", allowed_actions=["wait"])
    assert (WaitWorker().execute(chronic, {}, decision)["next_check_days"]
            > WaitWorker().execute(occasional, {}, decision)["next_check_days"])


def test_wait_worker_records_the_non_action(worker_invoice, no_audit_writes):
    """Doing nothing is a decision, so it gets a record with the reasoning attached."""
    WaitWorker().execute(worker_invoice, {}, make_decision("ALLOW", allowed_actions=["wait"]))
    assert [e["event_type"] for e in no_audit_writes] == ["wait_scheduled"]
    assert no_audit_writes[0]["payload"]["based_on"] == "historical_payment_pattern"


def test_information_worker_drafts_message_not_sends(worker_invoice, no_audit_writes):
    """`status` is "draft_created", never "sent" — a human decides whether it goes out."""
    result = InformationWorker().execute(
        worker_invoice, {}, make_decision("ALLOW", allowed_actions=["gather_information"]),
    )
    assert result["status"] == "draft_created"
    assert result["action_required"] == "human_must_review_and_send"
    assert "sent" not in result["status"]


def test_information_worker_draft_contains_invoice_id(worker_invoice, no_audit_writes):
    result = InformationWorker().execute(
        worker_invoice, {}, make_decision("ALLOW", allowed_actions=["gather_information"]),
    )
    assert "INV-TEST" in result["draft_message"]


def test_information_worker_draft_contains_rupee_amount(worker_invoice, no_audit_writes):
    """The client-facing figure is rupees derived from paise by integer division."""
    result = InformationWorker().execute(
        worker_invoice, {}, make_decision("ALLOW", allowed_actions=["gather_information"]),
    )
    assert "₹12,345" in result["draft_message"]
    assert "1234500" not in result["draft_message"]


def test_information_worker_draft_asserts_nothing_about_the_cause(worker_invoice, no_audit_writes):
    """The draft is written before the cause is known, so it may not accuse anyone."""
    draft = InformationWorker().execute(
        worker_invoice, {}, make_decision("ALLOW", allowed_actions=["gather_information"]),
    )["draft_message"].lower()
    for accusation in ("dispute", "overdue payment is your", "legal", "failed to pay", "refused"):
        assert accusation not in draft


def test_escalation_worker_carries_the_evidence_to_the_reviewer(worker_invoice, no_audit_writes):
    """Escalating is an outcome — the event must let a reviewer skip reconstruction."""
    diagnosis = {
        "diagnosed_cause": "promise_broken",
        "evidence_tier": "known",
        "evidence_cited": "Client promised payment on the 5th and did not pay.",
    }
    result = EscalationWorker().execute(
        worker_invoice, diagnosis,
        make_decision("REQUIRE_HUMAN",
                      reasons=["diagnosed_cause_promise_broken_requires_human_judgment"]),
    )
    assert result["status"] == "escalated"
    assert result["priority"] == "MEDIUM"
    assert result["assigned_to"] == "finance_team"

    payload = no_audit_writes[0]["payload"]
    assert payload["diagnosed_cause"] == "promise_broken"
    assert payload["evidence_cited"] == diagnosis["evidence_cited"]


def test_orchestrator_execute_logs_routing_then_delegates(worker_invoice, no_audit_writes):
    """The routing decision itself is auditable, not just its outcome."""
    result = Orchestrator().execute(
        worker_invoice, {}, make_decision("ALLOW", allowed_actions=["wait"]), rzp_client=None,
    )
    events = [e["event_type"] for e in no_audit_writes]
    assert events[0] == "orchestrator_routing"
    assert no_audit_writes[0]["payload"]["routed_to"] == "wait"
    assert result["worker"] == "wait"

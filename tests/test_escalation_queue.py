"""Tests for the human review queue — api/escalations.py.

The queue is the one place a person can authorise the system to act, so the
question every test here answers is: *what can a reviewer cause?*

The first section needs no database. It runs the real policy engine over the
whole 93-invoice corpus and establishes the invariant the queue rests on —
a reviewer chooses among the actions the engine already permitted for that
specific invoice, and the engine never permits a customer-facing action on a
REQUIRE_HUMAN one. That result is a property of the engine, not of the queue,
which is exactly why the queue can be trusted with it.

The second section is the round trip, and needs Postgres.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.escalations import (  # noqa: E402
    ActionNotPermitted,
    EscalationError,
    EscalationNotFound,
    EscalationStale,
    PRIORITY_ORDER,
    RESOLUTIONS,
    _invoice_record,
    _to_case_file,
    approvable_actions,
    escalation_summary,
    load_open_escalations,
    load_resolution_history,
    resolve_escalation,
)
from policy.engine import PolicyInput, evaluate  # noqa: E402
from tests.conftest import load_dataset, unique_invoice_id  # noqa: E402

# Actions that reach a client or ask them for money. Nothing a reviewer does
# through this queue may produce one.
CUSTOMER_FACING_ACTIONS = {"send_reminder", "create_payment_link"}


def _policy_inputs_for_whole_corpus():
    """Every corpus invoice as a PolicyInput, using its ground-truth labels.

    Ground truth rather than a model call: this asks what the *engine* permits,
    and a sampled diagnosis would make the answer depend on the weather.
    """
    for invoice in load_dataset("all_invoices.json"):
        outstanding = (
            invoice["amount_paise"] - invoice["partial_payment_received_paise"]
        )
        if outstanding <= 0:
            continue
        yield invoice, PolicyInput(
            invoice_id=invoice["invoice_id"],
            amount_paise=invoice["amount_paise"],
            outstanding_balance_paise=outstanding,
            days_overdue=invoice["days_overdue"],
            account_value_tier=invoice["account_value_tier"],
            evidence_tier=invoice.get("evidence_tier", "unknown"),
            recommended_action=invoice.get("correct_action", "gather_information"),
            requested_amount_paise=outstanding,
            existing_payment_link=invoice["payment_link_status"] == "active",
            client_notes=invoice.get("client_notes") or "",
            hard_stop=invoice.get("hard_stop", False),
            diagnosed_cause=invoice.get("true_cause", "unknown"),
            reminders_sent=invoice["reminders_sent"],
        )


# ─────────────────────────────────────────────────────────────────────────
# The invariant the queue rests on — no database needed
# ─────────────────────────────────────────────────────────────────────────

def test_no_escalation_in_the_corpus_permits_a_customer_facing_action():
    """A reviewer can never chase a client or raise a payment demand.

    This is the queue's central safety property, and it is not enforced *by*
    the queue — the queue only refuses to exceed `allowed_actions`. What makes
    it hold is that no REQUIRE_HUMAN decision the engine can produce contains
    `send_reminder` or `create_payment_link` in the first place.

    If a future rule adds one, this test fails here rather than the first time
    somebody clicks Approve on a disputed invoice.
    """
    offenders = []
    escalations_seen = 0
    for invoice, policy_input in _policy_inputs_for_whole_corpus():
        decision = evaluate(policy_input)
        if decision.decision != "REQUIRE_HUMAN":
            continue
        escalations_seen += 1
        leaked = CUSTOMER_FACING_ACTIONS.intersection(decision.allowed_actions)
        if leaked:
            offenders.append((invoice["invoice_id"], sorted(leaked)))

    assert escalations_seen > 0, "the corpus produced no escalations to check"
    assert offenders == [], (
        "a REQUIRE_HUMAN decision permits a customer-facing action, so the "
        f"review queue could authorise one: {offenders}"
    )


def test_a_disputed_invoice_offers_nothing_to_approve():
    """The reviewer handles a dispute personally; the system stays out of it.

    A dispute's decision permits `escalate_to_human` and
    `create_dispute_record`, both of which are handoffs to a person — which is
    what the review already is. So the queue offers no Approve action at all,
    and the reviewer records a verdict instead.
    """
    disputed = evaluate(PolicyInput(
        invoice_id="INV-DISPUTE",
        amount_paise=1_000_000,
        outstanding_balance_paise=1_000_000,
        days_overdue=20,
        account_value_tier="standard",
        evidence_tier="known",
        recommended_action="send_reminder",
        requested_amount_paise=1_000_000,
        existing_payment_link=False,
        client_notes="We are disputing this invoice — the quantities are wrong.",
        diagnosed_cause="dispute",
        reminders_sent=1,
    ))

    assert disputed.decision == "REQUIRE_HUMAN"
    assert approvable_actions("INV-DISPUTE", disputed.allowed_actions) == []


def test_approvable_actions_excludes_anything_that_re_escalates():
    """Approving a handoff would put the invoice straight back in the queue.

    `escalate_to_human` routes to the escalation worker, which writes a fresh
    `escalated_to_human` event — an infinite queue loop. Those are filtered
    out of what a reviewer is offered.
    """
    mixed = ["escalate_to_human", "gather_information", "create_dispute_record"]
    assert approvable_actions("INV-1", mixed) == ["gather_information"]


def test_approvable_actions_keeps_the_information_actions():
    """`gather_information` drafts outreach and never sends it — safe to authorise."""
    assert approvable_actions("INV-1", ["gather_information"]) == ["gather_information"]
    assert approvable_actions("INV-1", ["wait"]) == ["wait"]


def test_approvable_actions_is_empty_for_an_empty_permission_set():
    assert approvable_actions("INV-1", []) == []
    assert approvable_actions("INV-1", None) == []


def test_approvable_actions_never_widens_the_engines_permission_set():
    """Whatever comes back is a subset of what went in. Tightening-only."""
    for _invoice, policy_input in _policy_inputs_for_whole_corpus():
        decision = evaluate(policy_input)
        if decision.decision != "REQUIRE_HUMAN":
            continue
        approvable = approvable_actions(decision.invoice_id, decision.allowed_actions)
        assert set(approvable).issubset(set(decision.allowed_actions)), (
            f"{decision.invoice_id}: approvable set is not a subset of allowed_actions"
        )


# ─────────────────────────────────────────────────────────────────────────
# Input validation — refused before anything is read or written
# ─────────────────────────────────────────────────────────────────────────

def test_an_unknown_resolution_is_refused():
    with pytest.raises(EscalationError, match="resolution must be one of"):
        resolve_escalation("INV-1", resolution="maybe", actor="finance@example.com")


def test_an_unattributed_verdict_is_refused():
    """An approval nobody signed is not an audit trail."""
    with pytest.raises(EscalationError, match="actor is required"):
        resolve_escalation("INV-1", resolution="approved", actor="   ")


def test_the_three_resolutions_are_the_documented_ones():
    assert set(RESOLUTIONS) == {"approved", "rejected", "deferred"}


def test_priority_order_is_worst_first_and_matches_the_worker():
    from execution.workers import _PRIORITY_ORDER

    assert PRIORITY_ORDER == _PRIORITY_ORDER


# ─────────────────────────────────────────────────────────────────────────
# Case-file shaping — pure, no database
# ─────────────────────────────────────────────────────────────────────────

def _row(**overrides):
    row = {
        "invoice_id": "INV-9001",
        "escalated_at": "2026-08-27T10:00:00Z",
        "escalation_result": {"priority": "HIGH", "status": "escalated"},
        "current_decision": "REQUIRE_HUMAN",
        "reasons": ["strategic_account_requires_human_approval"],
        "allowed_actions": ["escalate_to_human", "gather_information"],
        "diagnosis": {
            "diagnosed_cause": "cash_flow_delay",
            "evidence_tier": "strong_inference",
            "evidence_cited": "Client mentioned a funding delay.",
            "recommended_action": "send_reminder",
        },
        "client_name": "Konkan Marine Logistics",
        "amount_paise": 5_200_000_00,
        "outstanding_balance_paise": 5_000_000_00,
        "days_overdue": 22,
        "account_value_tier": "strategic",
        "client_notes": "Funding round closes next week.",
    }
    row.update(overrides)
    return row


def test_case_file_separates_what_is_permitted_from_what_is_approvable():
    case = _to_case_file(_row())
    assert case["allowed_actions"] == ["escalate_to_human", "gather_information"]
    assert case["approvable_actions"] == ["gather_information"]


def test_case_file_carries_the_diagnosis_the_reviewer_needs():
    case = _to_case_file(_row())
    assert case["diagnosed_cause"] == "cash_flow_delay"
    assert case["evidence_tier"] == "strong_inference"
    assert case["evidence_cited"] == "Client mentioned a funding delay."
    # What the agent wanted, so the reviewer sees the refusal, not just the case.
    assert case["recommended_action"] == "send_reminder"


def test_case_file_defaults_priority_to_low_for_a_row_without_one():
    """Rows written before EscalationWorker recorded a priority still queue."""
    case = _to_case_file(_row(escalation_result=None))
    assert case["priority"] == "LOW"


def test_case_file_survives_a_missing_diagnosis():
    """A fail-closed escalation has a decision and no diagnosis."""
    case = _to_case_file(_row(diagnosis=None, allowed_actions=None))
    assert case["diagnosed_cause"] is None
    assert case["allowed_actions"] == []
    assert case["approvable_actions"] == []


def test_invoice_record_reconstructs_the_partial_payment_from_the_balance():
    """The workers take an invoice dict; only the balance survives in the table."""
    invoice = _invoice_record(_row())
    assert invoice["amount_paise"] == 5_200_000_00
    assert invoice["partial_payment_received_paise"] == 200_000_00
    assert invoice["amount_paise"] - invoice["partial_payment_received_paise"] == 5_000_000_00


def test_invoice_record_never_reports_a_negative_partial_payment():
    """A balance larger than the amount is bad data, not a negative payment."""
    invoice = _invoice_record(_row(amount_paise=100, outstanding_balance_paise=500))
    assert invoice["partial_payment_received_paise"] == 0


def test_invoice_record_is_never_marked_hard_stop_or_ground_truthed():
    """A reconstructed record must not smuggle in labels it does not have."""
    invoice = _invoice_record(_row())
    assert invoice["hard_stop"] is False
    assert invoice["true_cause"] == "unknown"
    assert invoice["scenario_type"] == "human_approved_escalation"


# ─────────────────────────────────────────────────────────────────────────
# Round trip — needs Postgres
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.db
class TestQueueRoundTrip:
    """End to end against a real database: escalate, review, resolve."""

    def _escalate(self, insert_test_invoice, **decision_overrides):
        """Puts one invoice in the queue the way the pipeline does, and returns its id."""
        from audit.trail import log_audit_event

        invoice = insert_test_invoice(client_name="ZZTest Queue Client")
        invoice_id = invoice["invoice_id"]

        decision = {
            "decision": "REQUIRE_HUMAN",
            "reasons": ["strategic_account_requires_human_approval"],
            "allowed_actions": ["escalate_to_human", "gather_information"],
            "blocked_actions": ["send_reminder"],
            "diagnosis": {
                "diagnosed_cause": "cash_flow_delay",
                "evidence_tier": "strong_inference",
                "evidence_cited": "Client mentioned a funding delay.",
                "recommended_action": "send_reminder",
            },
        }
        decision.update(decision_overrides)
        log_audit_event(invoice_id, "policy_decision", decision)
        log_audit_event(invoice_id, "escalated_to_human", {
            "reasons": decision["reasons"],
            "execution": {"worker": "escalation", "status": "escalated", "priority": "HIGH"},
        })
        return invoice_id

    def _find(self, invoice_id):
        return next(
            (c for c in load_open_escalations() if c["invoice_id"] == invoice_id), None
        )

    def test_an_escalated_invoice_appears_in_the_queue(self, insert_test_invoice):
        invoice_id = self._escalate(insert_test_invoice)
        case = self._find(invoice_id)

        assert case is not None, "escalated invoice is not in the open queue"
        assert case["priority"] == "HIGH"
        assert case["approvable_actions"] == ["gather_information"]

    def test_resolving_removes_it_from_the_queue(self, insert_test_invoice):
        invoice_id = self._escalate(insert_test_invoice)
        resolve_escalation(
            invoice_id, resolution="rejected",
            actor="finance@example.com", note="Calling the client directly.",
        )
        assert self._find(invoice_id) is None

    def test_a_rejection_is_recorded_rather_than_silently_dropped(self, insert_test_invoice):
        """An escalation that vanishes is indistinguishable from one nobody read."""
        from audit.trail import get_audit_trail

        invoice_id = self._escalate(insert_test_invoice)
        resolve_escalation(
            invoice_id, resolution="rejected",
            actor="finance@example.com", note="Handled on a call.",
        )

        trail = get_audit_trail(invoice_id)
        resolved = [e for e in trail if e["event_type"] == "escalation_resolved"]
        assert len(resolved) == 1
        record = resolved[0]["execution_result"]
        assert record["resolution"] == "rejected"
        assert record["actor"] == "finance@example.com"
        assert record["note"] == "Handled on a call."
        assert record["worker_execution"] is None, "a rejection must take no action"

    def test_a_rejection_contacts_nobody(self, insert_test_invoice):
        """No worker runs, so no orchestrator_routing event is written."""
        from audit.trail import get_audit_trail

        invoice_id = self._escalate(insert_test_invoice)
        resolve_escalation(invoice_id, resolution="rejected", actor="finance@example.com")

        events = [e["event_type"] for e in get_audit_trail(invoice_id)]
        assert "orchestrator_routing" not in events

    def test_approving_a_permitted_action_dispatches_it(self, insert_test_invoice):
        from audit.trail import get_audit_trail

        invoice_id = self._escalate(insert_test_invoice)
        result = resolve_escalation(
            invoice_id, resolution="approved", actor="finance@example.com",
            action="gather_information",
        )

        assert result["authorised_action"] == "gather_information"
        assert result["execution"]["worker"] == "information"

        events = [e["event_type"] for e in get_audit_trail(invoice_id)]
        assert "orchestrator_routing" in events, "approval did not go through the orchestrator"

    def test_approving_an_unpermitted_action_is_refused_and_writes_nothing(
        self, insert_test_invoice
    ):
        """The reviewer asks for a reminder on an invoice the engine never allowed one on."""
        from audit.trail import get_audit_trail

        invoice_id = self._escalate(insert_test_invoice)
        with pytest.raises(ActionNotPermitted, match="does not permit"):
            resolve_escalation(
                invoice_id, resolution="approved", actor="finance@example.com",
                action="send_reminder",
            )

        events = [e["event_type"] for e in get_audit_trail(invoice_id)]
        assert "escalation_resolved" not in events, "a refused approval was recorded as resolved"
        assert "orchestrator_routing" not in events
        assert self._find(invoice_id) is not None, "a refused approval closed the case"

    def test_approving_a_handoff_only_case_is_refused(self, insert_test_invoice):
        """A dispute permits nothing but handoffs, so Approve has nothing to do."""
        invoice_id = self._escalate(
            insert_test_invoice,
            allowed_actions=["escalate_to_human", "create_dispute_record"],
            reasons=["dispute_signal_detected_in_client_notes"],
        )
        with pytest.raises(ActionNotPermitted, match="no approvable action"):
            resolve_escalation(
                invoice_id, resolution="approved", actor="finance@example.com",
                action="create_dispute_record",
            )

    def test_approving_without_naming_an_action_is_refused(self, insert_test_invoice):
        invoice_id = self._escalate(insert_test_invoice)
        with pytest.raises(ActionNotPermitted, match="requires an action"):
            resolve_escalation(
                invoice_id, resolution="approved", actor="finance@example.com",
            )

    def test_resolving_an_invoice_with_no_open_escalation_is_refused(
        self, insert_test_invoice
    ):
        invoice = insert_test_invoice()
        with pytest.raises(EscalationNotFound):
            resolve_escalation(
                invoice["invoice_id"], resolution="rejected", actor="finance@example.com",
            )

    def test_resolving_twice_is_refused(self, insert_test_invoice):
        invoice_id = self._escalate(insert_test_invoice)
        resolve_escalation(invoice_id, resolution="rejected", actor="finance@example.com")
        with pytest.raises(EscalationNotFound):
            resolve_escalation(invoice_id, resolution="approved", actor="finance@example.com")

    def test_a_re_escalated_invoice_comes_back_into_the_queue(self, insert_test_invoice):
        """A resolution closes one escalation, not the invoice forever."""
        from audit.trail import log_audit_event

        invoice_id = self._escalate(insert_test_invoice)
        resolve_escalation(invoice_id, resolution="deferred", actor="finance@example.com")
        assert self._find(invoice_id) is None

        log_audit_event(invoice_id, "escalated_to_human", {
            "reasons": ["invoice_overdue_beyond_60_days"],
            "execution": {"worker": "escalation", "status": "escalated", "priority": "URGENT"},
        })
        case = self._find(invoice_id)
        assert case is not None, "a re-escalated invoice did not return to the queue"
        assert case["priority"] == "URGENT"

    def test_an_invoice_re_processed_into_an_allow_leaves_the_queue(
        self, insert_test_invoice
    ):
        """Found against the real database, not in a fixture.

        INV-1013 had been escalated in one run and re-processed into an ALLOW in
        a later one. Its `escalated_to_human` event was still unresolved, so it
        sat in the queue — and the case file was built from its *current*
        decision, an ALLOW, whose `allowed_actions` was `['create_payment_link']`.
        The queue was therefore offering a reviewer a payment demand, which no
        REQUIRE_HUMAN decision can ever permit.

        `resolve_escalation` refused it correctly, but only after the reviewer
        had clicked. The queue now lists invoices *currently* awaiting a human,
        which is not the same as invoices once escalated and never resolved.
        """
        from audit.trail import log_audit_event

        invoice_id = self._escalate(insert_test_invoice)
        assert self._find(invoice_id) is not None

        # The pipeline re-runs and this time the engine clears it.
        log_audit_event(invoice_id, "policy_decision", {
            "decision": "ALLOW",
            "reasons": ["no_blocking_rules_fired"],
            "allowed_actions": ["create_payment_link"],
            "blocked_actions": [],
        })

        assert self._find(invoice_id) is None, (
            "a superseded escalation is still in the queue"
        )

    def test_no_case_in_the_queue_ever_offers_a_customer_facing_action(
        self, insert_test_invoice
    ):
        """The surface-level version of the corpus invariant, over live rows."""
        self._escalate(insert_test_invoice)
        for case in load_open_escalations():
            assert not CUSTOMER_FACING_ACTIONS.intersection(case["allowed_actions"]), (
                f"{case['invoice_id']} offers a customer-facing action for approval"
            )
            assert not CUSTOMER_FACING_ACTIONS.intersection(case["approvable_actions"])

    def test_a_superseded_escalation_reports_staleness_not_absence(
        self, insert_test_invoice
    ):
        """409 and 404 are different facts for a reviewer on a stale browser tab.

        Dropping the case from the listing must not collapse "this was
        re-processed" into "no such thing" — the reviewer needs to know their
        judgment was about facts that have since changed.
        """
        from audit.trail import log_audit_event

        invoice_id = self._escalate(insert_test_invoice)
        log_audit_event(invoice_id, "policy_decision", {
            "decision": "ALLOW",
            "reasons": ["no_blocking_rules_fired"],
            "allowed_actions": ["create_payment_link"],
            "blocked_actions": [],
        })

        with pytest.raises(EscalationStale, match="re-processed"):
            resolve_escalation(
                invoice_id, resolution="approved", actor="finance@example.com",
                action="create_payment_link",
            )

    def test_a_blocked_invoice_never_enters_the_queue(self, insert_test_invoice):
        """BLOCK is not a handoff. There is no reviewer path to a blocked invoice."""
        from audit.trail import log_audit_event

        invoice = insert_test_invoice()
        invoice_id = invoice["invoice_id"]
        log_audit_event(invoice_id, "policy_decision", {
            "decision": "BLOCK",
            "reasons": ["legal_threat_detected_in_client_notes"],
            "allowed_actions": [],
            "blocked_actions": ["send_reminder", "create_payment_link"],
        })
        log_audit_event(invoice_id, "blocked_no_customer_action", {
            "execution": {"worker": "block", "status": "blocked"},
        })

        assert self._find(invoice_id) is None
        with pytest.raises(EscalationNotFound):
            resolve_escalation(invoice_id, resolution="approved", actor="x@example.com")

    def test_the_summary_counts_what_the_queue_holds(self, insert_test_invoice):
        before = escalation_summary()
        invoice_id = self._escalate(insert_test_invoice)
        after = escalation_summary()

        assert after["open_count"] == before["open_count"] + 1
        assert after["by_priority"]["HIGH"] == before["by_priority"]["HIGH"] + 1
        assert after["outstanding_under_review_paise"] > before["outstanding_under_review_paise"]

        resolve_escalation(invoice_id, resolution="rejected", actor="finance@example.com")
        assert escalation_summary()["open_count"] == before["open_count"]

    def test_resolution_history_records_who_decided_what(self, insert_test_invoice):
        invoice_id = self._escalate(insert_test_invoice)
        resolve_escalation(
            invoice_id, resolution="deferred", actor="cfo@example.com",
            note="Revisit after their funding round.",
        )

        entry = next(
            (h for h in load_resolution_history(limit=200) if h["invoice_id"] == invoice_id),
            None,
        )
        assert entry is not None
        assert entry["resolution"] == "deferred"
        assert entry["actor"] == "cfo@example.com"
        assert entry["note"] == "Revisit after their funding round."

    def test_the_queue_is_ordered_worst_first(self, insert_test_invoice):
        self._escalate(insert_test_invoice)
        cases = load_open_escalations()
        ranks = [PRIORITY_ORDER.get(c["priority"], 99) for c in cases]
        assert ranks == sorted(ranks), "the queue is not ordered by priority"

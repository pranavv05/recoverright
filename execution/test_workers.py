"""Routing and classification tests for the orchestrator-workers layer.

Everything here runs without a database, an LLM key, or a Razorpay client.
`Orchestrator.route` is pure and the two classification helpers
(`_determine_priority`, block-type lookup) are table-driven, which is the
point of separating routing from execution: the decision layer stays testable
offline even though the diagnosis upstream of it is a sampled model call.

The two BlockWorker tests do call `execute`, which writes an audit event, so
`log_audit_event` is stubbed out for those.
"""

import pytest

from policy.engine import PolicyDecision
from execution.workers import (
    BLOCK_TYPE_MAP,
    PRIORITY_MAP,
    BlockWorker,
    EscalationWorker,
    Orchestrator,
    _PRIORITY_PREFIXES,
)


def make_decision(decision, reasons=None, allowed_actions=None,
                  blocked_actions=None):
    return PolicyDecision(
        invoice_id="TEST",
        decision=decision,
        reasons=reasons or ["test_reason"],
        allowed_actions=allowed_actions or [],
        blocked_actions=blocked_actions or [],
    )


orchestrator = Orchestrator()


@pytest.fixture
def no_audit(monkeypatch):
    """Captures audit events instead of writing them to Postgres."""
    written = []
    monkeypatch.setattr(
        "execution.workers.log_audit_event",
        lambda invoice_id, event_type, payload: written.append(
            (invoice_id, event_type, payload)
        ),
    )
    return written


# ── Routing ────────────────────────────────────────────────────────────────

def test_block_routes_to_block_worker():
    d = make_decision("BLOCK")
    assert orchestrator.route({}, {}, d) == "block"


def test_require_human_routes_to_escalation():
    d = make_decision("REQUIRE_HUMAN")
    assert orchestrator.route({}, {}, d) == "escalation"


def test_allow_payment_link_routes_correctly():
    d = make_decision("ALLOW", allowed_actions=["create_payment_link"])
    assert orchestrator.route({}, {}, d) == "payment_link"


def test_allow_wait_routes_correctly():
    d = make_decision("ALLOW", allowed_actions=["wait"])
    assert orchestrator.route({}, {}, d) == "wait"


def test_allow_gather_information_routes_correctly():
    d = make_decision("ALLOW", allowed_actions=["gather_information"])
    assert orchestrator.route({}, {}, d) == "information"


def test_allow_send_reminder_routes_correctly():
    d = make_decision("ALLOW", allowed_actions=["send_reminder"])
    assert orchestrator.route({}, {}, d) == "information"


def test_allow_escalate_to_human_routes_to_escalation():
    d = make_decision("ALLOW", allowed_actions=["escalate_to_human"])
    assert orchestrator.route({}, {}, d) == "escalation"


def test_allow_flag_for_legal_routes_to_escalation():
    d = make_decision("ALLOW", allowed_actions=["flag_for_legal_escalation"])
    assert orchestrator.route({}, {}, d) == "escalation"


def test_allow_unknown_action_defaults_to_escalation():
    d = make_decision("ALLOW", allowed_actions=["completely_unknown_action"])
    assert orchestrator.route({}, {}, d) == "escalation"


# ── Escalation priority ────────────────────────────────────────────────────

def test_escalation_priority_urgent_for_legal():
    w = EscalationWorker()
    priority = w._determine_priority([
        "legal_threat_detected_in_client_notes",
        "invoice_overdue_beyond_30_days_escalation_level",
    ])
    assert priority == "URGENT"


def test_escalation_priority_high_for_dispute():
    w = EscalationWorker()
    priority = w._determine_priority([
        "dispute_signal_detected_in_client_notes",
    ])
    assert priority == "HIGH"


def test_escalation_priority_low_for_overdue_only():
    w = EscalationWorker()
    priority = w._determine_priority([
        "invoice_overdue_beyond_60_days",
    ])
    assert priority == "LOW"


# ── Block classification ───────────────────────────────────────────────────

def test_block_type_security_for_injection(no_audit):
    w = BlockWorker()
    inv = {"invoice_id": "TEST", "client_name": "Test", "amount_paise": 100000}
    diag = {}
    d = make_decision(
        "BLOCK", reasons=["prompt_injection_detected_in_client_notes"]
    )
    result = w.execute(inv, diag, d)
    assert result["block_type"] == "security"
    assert result["status"] == "blocked"


def test_block_type_legal_for_legal_threat(no_audit):
    w = BlockWorker()
    inv = {"invoice_id": "TEST", "client_name": "Test", "amount_paise": 100000}
    d = make_decision("BLOCK", reasons=["legal_threat_detected_in_client_notes"])
    result = w.execute(inv, {}, d)
    assert result["block_type"] == "legal"


# ── Regressions the spec's tables would not have caught ────────────────────
#
# The engine appends `matched_phrase:<text>` as a second reason on every
# content-detection rule, and interpolates the cause into the high-risk
# diagnosed-cause reason. Both broke naive exact-match classification.

def test_priority_ignores_matched_phrase_suffix_reason():
    """Real dispute decisions carry a second `matched_phrase:` reason."""
    w = EscalationWorker()
    priority = w._determine_priority([
        "dispute_signal_detected_in_client_notes",
        "matched_phrase:we were billed for 3 container shipments",
    ])
    assert priority == "HIGH"


def test_priority_matches_interpolated_diagnosed_cause_reason():
    """`diagnosed_cause_<cause>_requires_human_judgment` is built per-cause."""
    w = EscalationWorker()
    for cause in ("promise_broken", "deliberate_stall"):
        reason = f"diagnosed_cause_{cause}_requires_human_judgment"
        assert w._determine_priority([reason]) == "MEDIUM", reason


def test_priority_matches_interpolated_unrecognised_action_reason():
    w = EscalationWorker()
    priority = w._determine_priority([
        "unrecognised_recommended_action:delete_the_invoice",
    ])
    assert priority == "MEDIUM"


def test_block_type_picks_primary_reason_not_matched_phrase(no_audit):
    w = BlockWorker()
    d = make_decision("BLOCK", reasons=[
        "prompt_injection_detected_in_client_notes",
        "matched_phrase:ignore previous instructions",
    ])
    inv = {"invoice_id": "TEST", "client_name": "Test", "amount_paise": 100000}
    assert w.execute(inv, {}, d)["block_type"] == "security"


def test_block_worker_takes_no_customer_facing_action(no_audit):
    """The only thing a BLOCK may produce is one audit event."""
    w = BlockWorker()
    inv = {"invoice_id": "TEST", "client_name": "Test", "amount_paise": 100000}
    d = make_decision("BLOCK", reasons=["legal_threat_detected_in_client_notes"])
    w.execute(inv, {}, d)
    assert [e[1] for e in no_audit] == ["blocked_no_customer_action"]


def test_every_block_type_map_key_is_a_real_engine_reason():
    """Guards against the map drifting away from policy/engine.py."""
    import pathlib
    engine = pathlib.Path("policy/engine.py").read_text(encoding="utf-8")
    for reason in BLOCK_TYPE_MAP:
        assert f'"{reason}"' in engine, f"{reason} is not emitted by the engine"


def test_every_priority_map_key_matches_a_live_engine_reason_or_a_documented_prefix():
    """Guards against PRIORITY_MAP drifting the way it already has once.

    A policy rule rename left three PRIORITY_MAP keys matching nothing the
    engine emits any more (`promise_broken_or_deliberate_stall_requires_human`,
    `reminder_fatigue_5_or_more_reminders_sent`,
    `strong_inference_with_no_client_communication_evidence`) — silently, since
    an unmatched key just falls through to the same LOW default an unmapped
    reason gets, so nothing ever errored. Each key here must now be either a
    literal string that appears quoted in policy/engine.py — the same check
    `test_every_block_type_map_key_is_a_real_engine_reason` above makes for
    BLOCK_TYPE_MAP — or start with one of _PRIORITY_PREFIXES' declared
    prefixes, which is how the two f-string-built families
    (`account_tier_*`, `diagnosed_cause_*`) are covered: neither ever appears
    as a quoted literal in the engine, since the engine only ever produces one
    via string interpolation.
    """
    import pathlib
    engine = pathlib.Path("policy/engine.py").read_text(encoding="utf-8")
    prefixes = tuple(prefix for prefix, _priority in _PRIORITY_PREFIXES)

    unmatched = [
        reason for reason in PRIORITY_MAP
        if f'"{reason}"' not in engine and not reason.startswith(prefixes)
    ]
    assert unmatched == [], (
        f"PRIORITY_MAP key(s) match no literal reason in policy/engine.py and "
        f"no declared prefix in _PRIORITY_PREFIXES: {unmatched} — either the "
        f"engine's reason string changed and this map fell behind (delete or "
        f"rename the key to match), or a new dynamic reason needs a prefix"
    )


# ── Structural safety invariant ────────────────────────────────────────────

def test_block_and_require_human_never_route_to_a_customer_facing_worker():
    """No action string can talk a refusal into contacting the client."""
    customer_facing = {"payment_link"}
    actions = [
        "create_payment_link", "send_reminder", "gather_information", "wait",
        "escalate_to_human", "resend_existing_link", "flag_for_security_review",
        "anything_at_all", "",
    ]
    for decision in ("BLOCK", "REQUIRE_HUMAN"):
        for action in actions:
            d = make_decision(decision, allowed_actions=[action])
            assert orchestrator.route({}, {}, d) not in customer_facing


def test_route_is_total_over_every_known_action():
    """Every action the engine can emit maps to a worker that exists."""
    from policy.rules import KNOWN_ACTIONS
    for action in KNOWN_ACTIONS | {"send_diagnostic_reminder",
                                   "resend_existing_link", ""}:
        d = make_decision("ALLOW", allowed_actions=[action] if action else [])
        assert orchestrator.route({}, {}, d) in orchestrator._workers

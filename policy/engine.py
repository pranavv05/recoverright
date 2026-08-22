from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List
from policy.rules import (
    DAYS_OVERDUE_ESCALATION, DAYS_OVERDUE_HARD_REVIEW,
    MAX_REMINDERS_BEFORE_ESCALATION,
    HUMAN_APPROVAL_TIERS, HIGH_RISK_DIAGNOSED_CAUSES, HUMAN_ONLY_ACTIONS,
    WEAK_EVIDENCE_TIERS,
    LEGAL_KEYWORDS, DISPUTE_KEYWORDS, INJECTION_KEYWORDS
)

@dataclass
class PolicyInput:
    invoice_id: str
    amount_paise: int
    outstanding_balance_paise: int
    days_overdue: int
    account_value_tier: str
    evidence_tier: str
    recommended_action: str
    requested_amount_paise: int
    existing_payment_link: bool
    client_notes: str
    hard_stop: bool = False
    diagnosed_cause: str = "unknown"
    reminders_sent: int = 0

@dataclass
class PolicyDecision:
    invoice_id: str
    decision: str
    reasons: List[str]
    allowed_actions: List[str]
    blocked_actions: List[str]
    evaluated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_audit_entry(self) -> dict:
        """Returns a plain-dict view of the decision suitable for audit logging."""
        return {
            "invoice_id": self.invoice_id,
            "decision": self.decision,
            "reasons": self.reasons,
            "allowed_actions": self.allowed_actions,
            "blocked_actions": self.blocked_actions,
            "evaluated_at": self.evaluated_at
        }


def _detect_content(text: str, keywords: list) -> bool:
    """Returns True if any keyword appears in text (case-insensitive substring match)."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)


def evaluate(inp: PolicyInput) -> PolicyDecision:
    """Deterministically evaluates a policy input and returns ALLOW / BLOCK / REQUIRE_HUMAN.

    Zero LLM dependency. Evaluation order: content hard-stops → dispute →
    high-risk diagnosed cause → account sensitivity → financial invariants →
    evidence tier → severity → ALLOW.
    """

    # ── 1. Content hard-stops FIRST ──────────────────────────────
    if _detect_content(inp.client_notes, INJECTION_KEYWORDS):
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["prompt_injection_detected_in_client_notes"],
            allowed_actions=["flag_for_security_review"],
            blocked_actions=["all_automated_actions"]
        )

    if inp.hard_stop:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["hard_stop_flag_set_on_invoice"],
            allowed_actions=["flag_for_human_review"],
            blocked_actions=["all_automated_actions"]
        )

    if _detect_content(inp.client_notes, LEGAL_KEYWORDS):
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["legal_threat_detected_in_client_notes"],
            allowed_actions=["flag_for_legal_escalation"],
            blocked_actions=["all_automated_actions"]
        )

    # ── 2. Dispute detection ─────────────────────────────────────
    if _detect_content(inp.client_notes, DISPUTE_KEYWORDS):
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=["dispute_signal_detected_in_client_notes"],
            allowed_actions=["escalate_to_human", "create_dispute_record"],
            blocked_actions=["send_reminder", "send_collection_message",
                           "create_payment_link"]
        )

    # ── 3. High-risk diagnosed cause ─────────────────────────────
    # A broken promise or a deliberate stall both imply an automated
    # approach (a reminder) has already failed by definition — chasing
    # further without a human is exactly the damage this system exists
    # to avoid, regardless of what action the LLM proposed.
    if inp.diagnosed_cause in HIGH_RISK_DIAGNOSED_CAUSES:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[f"diagnosed_cause_{inp.diagnosed_cause}_requires_human_judgment"],
            allowed_actions=["escalate_to_human"],
            blocked_actions=["send_reminder", "send_collection_message",
                           "create_payment_link"]
        )

    # ── 4. Account sensitivity ───────────────────────────────────
    if inp.account_value_tier in HUMAN_APPROVAL_TIERS:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[f"account_tier_{inp.account_value_tier}_requires_human_approval"],
            allowed_actions=["escalate_to_human", "create_account_manager_review"],
            blocked_actions=["send_reminder", "send_collection_message"]
        )

    # ── 5. Financial invariants ───────────────────────────────────
    if inp.outstanding_balance_paise < 0:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["outstanding_balance_is_negative"],
            allowed_actions=[],
            blocked_actions=["all_actions"]
        )

    if inp.requested_amount_paise > inp.outstanding_balance_paise:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["requested_amount_exceeds_outstanding_balance"],
            allowed_actions=[],
            blocked_actions=["all_actions"]
        )

    if inp.recommended_action == "create_payment_link" and inp.existing_payment_link:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["active_payment_link_exists_idempotency_protection"],
            allowed_actions=["resend_existing_link"],
            blocked_actions=["create_payment_link"]
        )

    # ── 6. Evidence tier ─────────────────────────────────────────
    if inp.evidence_tier in WEAK_EVIDENCE_TIERS:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=["unknown_evidence_tier_cannot_trigger_autonomous_action"],
            allowed_actions=["gather_information", "send_diagnostic_reminder"],
            blocked_actions=["send_collection_message", "escalate_to_collections"]
        )

    # A "strong_inference" call with zero direct client communication and an
    # aggressive proposed action is inference stacked on inference — the LLM
    # cited nothing the client actually said. Require a human sanity check
    # rather than trusting a confidence label the client never confirmed.
    if (
        inp.evidence_tier == "strong_inference"
        and not inp.client_notes.strip()
        and inp.recommended_action == "send_reminder"
    ):
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=["unsupported_inference_no_client_communication"],
            allowed_actions=["gather_information"],
            blocked_actions=["send_reminder", "send_collection_message"]
        )

    # ── 7. Severity ──────────────────────────────────────────────
    if inp.days_overdue > DAYS_OVERDUE_HARD_REVIEW:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=["invoice_overdue_beyond_60_days"],
            allowed_actions=["escalate_to_human"],
            blocked_actions=["send_automated_reminder"]
        )

    # Repeated automated contact with no resolution — sending yet another
    # reminder is not working and needs human judgment, independent of
    # whatever cause the LLM diagnosed.
    if inp.reminders_sent >= MAX_REMINDERS_BEFORE_ESCALATION:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=["reminder_fatigue_repeated_contact_unresolved"],
            allowed_actions=["escalate_to_human"],
            blocked_actions=["send_reminder"]
        )

    # A recommended action that is itself a human handoff (escalate_to_human,
    # flag_for_legal, cancel_duplicate) is never something to "ALLOW" as if
    # it were autonomous execution — the LLM is already asking for a human,
    # regardless of what evidence tier or cause got it there.
    if inp.recommended_action in HUMAN_ONLY_ACTIONS:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=["recommended_action_is_a_human_handoff_not_autonomous"],
            allowed_actions=[inp.recommended_action],
            blocked_actions=[]
        )

    reasons = []
    if inp.days_overdue > DAYS_OVERDUE_ESCALATION:
        reasons.append("invoice_overdue_beyond_30_days_escalation_level")

    # ── 8. ALLOW ─────────────────────────────────────────────────
    return PolicyDecision(
        invoice_id=inp.invoice_id,
        decision="ALLOW",
        reasons=reasons if reasons else ["no_blocking_rules_fired"],
        allowed_actions=[inp.recommended_action],
        blocked_actions=[]
    )
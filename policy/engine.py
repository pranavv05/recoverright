"""Deterministic policy evaluator.

Every rule here is plain Python over a fixed input record. Two rules read
fields that originate from the LLM (`diagnosed_cause`, `evidence_tier`); both
are documented inline and both can only tighten the outcome, never loosen it.
Every other rule — content hard-stops, dispute detection, account tier,
financial invariants, severity — is computed from merchant/invoice data the
model never touches.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List

from policy.rules import (
    AMBIGUITY_PATTERNS,
    AUTONOMOUS_ACTIONS,
    COMMITMENT_LAPSE_PATTERNS,
    COMMITMENT_PATTERNS,
    DAYS_OVERDUE_ESCALATION,
    DAYS_OVERDUE_HARD_REVIEW,
    DISPUTE_NEGATION_PATTERNS,
    DISPUTE_PATTERNS,
    DOCUMENTATION_BLOCKER_PATTERNS,
    HIGH_RISK_DIAGNOSED_CAUSES,
    HUMAN_APPROVAL_TIERS,
    HUMAN_ONLY_ACTIONS,
    INJECTION_PATTERNS,
    KNOWN_ACTIONS,
    LEGAL_FALSE_POSITIVE_PATTERNS,
    LEGAL_PATTERNS,
    MAX_REMINDERS_BEFORE_ESCALATION,
    STALL_REPETITION_PATTERNS,
    UNVERIFIED_PAYMENT_CLAIM_PATTERNS,
    VALID_EVIDENCE_TIERS,
    WEAK_EVIDENCE_TIERS,
)


@dataclass
class PolicyInput:
    """The complete set of facts `evaluate()` is allowed to decide on.

    Deliberately a flat record of primitives rather than the raw invoice: the
    engine cannot reach for a field nobody listed here, so what the policy
    depends on is reviewable in one place. Only `evidence_tier` and
    `diagnosed_cause` originate from the LLM, and both are documented at
    their rule sites as tightening-only.
    """

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
    """One engine verdict: ALLOW, BLOCK, or REQUIRE_HUMAN, plus its reasons.

    `allowed_actions` — not the LLM's recommendation — is what the
    orchestrator routes on, so a decision carries its own permission set
    rather than deferring to whatever proposed it. `reasons` are stable
    snake_case identifiers, not prose: the dashboard, the worker priority
    table, and the evaluation all match on them.
    """

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
            "evaluated_at": self.evaluated_at,
        }


def _matches(text: str, patterns: list) -> str:
    """Returns the first matching pattern's matched text, or "" if none match."""
    if not text:
        return ""
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found.group(0)
    return ""


def detect_injection(client_notes: str) -> str:
    """Returns the matched injection phrase, or "" if the notes look clean."""
    return _matches(client_notes, INJECTION_PATTERNS)


def _spans(text: str, patterns: list) -> list:
    """Every (start, end) span in `text` matched by any pattern in `patterns`."""
    return [
        match.span()
        for pattern in patterns
        for match in pattern.finditer(text)
    ]


def _first_uncancelled(text: str, patterns: list, cancelling: list) -> str:
    """First match of `patterns` that is not swallowed by a `cancelling` match.

    A cue only counts when *that occurrence* of it survives. A note can deny
    one dispute and raise another in the same sentence ("not disputing the
    amount, but the goods arrived damaged"), or use an ambiguous word in both
    senses ("we advocate for prompt payment, but our advocate has written to
    you") — so the decision has to be made per occurrence, on position.

    An earlier revision compared *matched text* instead of position and
    excluded the hit itself from the corroboration search. That silently
    suppressed a genuine threat whenever a benign phrasing appeared anywhere
    in the same note: "we advocate for timely payment, but our attorney has
    advised us to hold" returned no legal hit at all, and "we are not
    disputing the amount, we are disputing the freight" returned no dispute.
    Both are false negatives on a real hard-stop, which is the one direction
    this engine must never fail in.
    """
    if not text:
        return ""
    cancelled = _spans(text, cancelling)
    for pattern in patterns:
        for match in pattern.finditer(text):
            start, end = match.span()
            # Only a cue fully inside a cancelling phrase is cancelled: the
            # negation "not in dispute" contains the cue "dispute", and the
            # false-positive reading "advocate for" contains "advocate".
            if any(c_start <= start and end <= c_end for c_start, c_end in cancelled):
                continue
            return match.group(0)
    return ""


def detect_legal(client_notes: str) -> str:
    """Returns the matched legal-threat phrase, or "" if none.

    "advocate" and "counsel" are ambiguous words — each has a common
    non-legal verb sense ("advocate for timely payment", "counsel your
    team"). LEGAL_PATTERNS excludes the adjacent-verb form via negative
    lookahead; LEGAL_FALSE_POSITIVE_PATTERNS is the second signal, applied
    per occurrence so a benign use elsewhere in the note cannot mask a real
    one.
    """
    return _first_uncancelled(client_notes, LEGAL_PATTERNS, LEGAL_FALSE_POSITIVE_PATTERNS)


def detect_broken_commitment(client_notes: str) -> tuple:
    """Returns (commitment_phrase, lapse_phrase), or ("", "") if the note is clean.

    Both halves are required. A commitment on its own is a promise that may
    still be kept; a lapse marker on its own ("still unpaid") is true of every
    overdue invoice. Only together do they evidence a promise already broken.

    Deliberately reads `client_notes` and nothing else. The neighbouring
    high-risk-cause rule keys on the model's `diagnosed_cause`, which means a
    broken promise the model labelled something milder never escalated — this
    detector reaches the same conclusion from merchant-authored evidence, so
    the outcome does not vary with a sampled LLM call.
    """
    commitment = _matches(client_notes, COMMITMENT_PATTERNS)
    if not commitment:
        return "", ""
    lapse = _matches(client_notes, COMMITMENT_LAPSE_PATTERNS)
    if not lapse:
        return "", ""
    return commitment, lapse


def detect_stall_repetition(client_notes: str) -> str:
    """Returns the matched repetition phrase, or "" if none.

    Fires on its own, unlike `detect_broken_commitment()`: an answer the
    client has repeated across reminder cycles is itself the evidence that
    automated contact has stopped working, with or without a promise attached.
    """
    return _matches(client_notes, STALL_REPETITION_PATTERNS)


def detect_dispute(client_notes: str) -> str:
    """Returns the matched dispute phrase, or "" if none.

    A dispute cue that is explicitly negated ("this one is not in dispute at
    all", "no objection to the amount") does not count. Substring matching
    without this check produced a false REQUIRE_HUMAN on any note that
    mentioned a dispute only to deny one — but the negation cancels only the
    cue it actually negates, so a note that denies one dispute while raising
    another still escalates.
    """
    return _first_uncancelled(client_notes, DISPUTE_PATTERNS, DISPUTE_NEGATION_PATTERNS)


def evaluate(inp: PolicyInput) -> PolicyDecision:
    """Deterministically evaluates a policy input and returns ALLOW / BLOCK / REQUIRE_HUMAN.

    Evaluation order: content hard-stops → dispute → unverified off-platform
    payment claim → documentation blocker → self-declared ambiguity → broken
    payment commitment → repeated non-answer → high-risk diagnosed cause →
    account sensitivity →
    financial invariants → action whitelist → evidence tier → severity → ALLOW.

    Order matters and is first-match-wins: the earlier a rule sits, the more
    absolute it is. Nothing after step 1 can turn a hard-stop back into an
    ALLOW, and ALLOW is reachable only by falling through every rule above it.
    """
    notes = inp.client_notes or ""

    # ── 1. Content hard-stops FIRST ──────────────────────────────
    injection_hit = detect_injection(notes)
    if injection_hit:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=[
                "prompt_injection_detected_in_client_notes",
                f"matched_phrase:{injection_hit[:80]}",
            ],
            allowed_actions=["flag_for_security_review"],
            blocked_actions=["all_automated_actions"],
        )

    # Checked before the generic hard_stop flag below: every legal_threat
    # invoice in the corpus also carries hard_stop=True, so if the generic
    # check ran first it would always win and "legal_threat_detected_in_
    # client_notes" / block_type "legal" could never actually fire — the
    # audit trail would call every legal threat a generic "data_error"
    # instead of naming the guardrail that actually caught it.
    legal_hit = detect_legal(notes)
    if legal_hit:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=[
                "legal_threat_detected_in_client_notes",
                f"matched_phrase:{legal_hit[:80]}",
            ],
            allowed_actions=["flag_for_legal_escalation"],
            blocked_actions=["all_automated_actions"],
        )

    if inp.hard_stop:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["hard_stop_flag_set_on_invoice"],
            allowed_actions=["flag_for_human_review"],
            blocked_actions=["all_automated_actions"],
        )

    # ── 2. Dispute detection ─────────────────────────────────────
    dispute_hit = detect_dispute(notes)
    if dispute_hit:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[
                "dispute_signal_detected_in_client_notes",
                f"matched_phrase:{dispute_hit[:80]}",
            ],
            allowed_actions=["escalate_to_human", "create_dispute_record"],
            blocked_actions=["send_reminder", "send_collection_message",
                             "create_payment_link"],
        )

    # ── 2b. Unverified off-platform payment claim ────────────────
    # The client says money already left their account by NEFT/IMPS/UPI/cheque
    # and our books do not show it. Chasing the balance before finance
    # reconciles the bank statement risks collecting twice.
    payment_claim_hit = _matches(notes, UNVERIFIED_PAYMENT_CLAIM_PATTERNS)
    if payment_claim_hit:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[
                "unverified_off_platform_payment_claim_must_be_reconciled_first",
                f"matched_phrase:{payment_claim_hit[:80]}",
            ],
            allowed_actions=["gather_information", "escalate_to_human"],
            blocked_actions=["send_reminder", "send_collection_message",
                             "create_payment_link"],
        )

    # ── 2b-ii. Documentation blocker ─────────────────────────────
    # The client is waiting on a document we owe them. Sending a payment
    # reminder cannot resolve it and reads as ignoring a reasonable request;
    # the unblocking action is ours to take.
    document_hit = _matches(notes, DOCUMENTATION_BLOCKER_PATTERNS)
    if document_hit:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[
                "documentation_blocker_client_awaiting_document_from_us",
                f"matched_phrase:{document_hit[:80]}",
            ],
            allowed_actions=["escalate_to_human", "gather_information"],
            blocked_actions=["send_reminder", "send_collection_message",
                             "create_payment_link"],
        )

    # ── 2c. Self-declared ambiguity in the evidence ──────────────
    # The record itself hedges between incompatible causes. There is no
    # defensible autonomous action when the evidence says "A or possibly B".
    ambiguity_hit = _matches(notes, AMBIGUITY_PATTERNS)
    if ambiguity_hit:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[
                "conflicting_or_hedged_evidence_requires_human_judgment",
                f"matched_phrase:{ambiguity_hit[:80]}",
            ],
            allowed_actions=["gather_information", "escalate_to_human"],
            blocked_actions=["send_reminder", "send_collection_message"],
        )

    # ── 2d. Broken payment commitment ────────────────────────────
    # The deterministic counterpart to rule 3 below, and deliberately ahead of
    # it: the client made a commitment to pay and it lapsed. That is evident
    # from the merchant's own note, so it is decided here from merchant data
    # rather than downstream from the model's `diagnosed_cause` — an invoice
    # whose broken promise the model labelled `chronic_late_payer` used to
    # clear every rule and reach ALLOW, which made a safety outcome contingent
    # on a sampled LLM call.
    commitment_hit, lapse_hit = detect_broken_commitment(notes)
    if commitment_hit:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[
                "broken_payment_commitment_requires_human_judgment",
                f"matched_phrase:{commitment_hit[:80]}",
                f"lapse_phrase:{lapse_hit[:80]}",
            ],
            allowed_actions=["escalate_to_human"],
            blocked_actions=["send_reminder", "send_collection_message",
                             "create_payment_link"],
        )

    # ── 2e. Repeated non-answer across reminder cycles ───────────
    # The stall's deterministic signature. Sits with the broken-commitment
    # rule and ahead of the LLM-dependent one for the same reason: it is
    # derived from the merchant's note, so it does not vary with the model.
    # Reminder fatigue (step 8) covers the same ground only when the model
    # also proposes another contact — this catches the stall regardless of
    # what the model proposed.
    stall_hit = detect_stall_repetition(notes)
    if stall_hit:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[
                "repeated_non_answer_across_reminder_cycles",
                f"matched_phrase:{stall_hit[:80]}",
            ],
            allowed_actions=["escalate_to_human"],
            blocked_actions=["send_reminder", "send_collection_message",
                             "create_payment_link"],
        )

    # ── 3. High-risk diagnosed cause ─────────────────────────────
    # LLM-DEPENDENT TRIGGER. A broken promise or a deliberate stall both imply
    # an automated approach (a reminder) has already failed by definition —
    # chasing further without a human is exactly the damage this system exists
    # to avoid. This rule can only tighten the outcome; a wrong diagnosis here
    # costs an unnecessary escalation, never an inappropriate action.
    if inp.diagnosed_cause in HIGH_RISK_DIAGNOSED_CAUSES:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[f"diagnosed_cause_{inp.diagnosed_cause}_requires_human_judgment"],
            allowed_actions=["escalate_to_human"],
            blocked_actions=["send_reminder", "send_collection_message",
                             "create_payment_link"],
        )

    # ── 4. Account sensitivity ───────────────────────────────────
    if inp.account_value_tier in HUMAN_APPROVAL_TIERS:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[f"account_tier_{inp.account_value_tier}_requires_human_approval"],
            allowed_actions=["escalate_to_human", "create_account_manager_review"],
            blocked_actions=["send_reminder", "send_collection_message"],
        )

    # ── 5. Financial invariants ───────────────────────────────────
    if inp.amount_paise <= 0:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["invoice_amount_is_not_positive"],
            allowed_actions=[],
            blocked_actions=["all_actions"],
        )

    if inp.outstanding_balance_paise < 0:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["outstanding_balance_is_negative"],
            allowed_actions=[],
            blocked_actions=["all_actions"],
        )

    # A partial payment cannot make the outstanding balance exceed the invoice
    # itself. If it does, the upstream record is corrupt — a negative
    # partial_payment in a merchant CSV is the realistic way this happens —
    # and no amount derived from it may be collected.
    if inp.outstanding_balance_paise > inp.amount_paise:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["outstanding_balance_exceeds_invoice_amount"],
            allowed_actions=["escalate_to_human"],
            blocked_actions=["all_actions"],
        )

    if inp.requested_amount_paise > inp.outstanding_balance_paise:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["requested_amount_exceeds_outstanding_balance"],
            allowed_actions=[],
            blocked_actions=["all_actions"],
        )

    if inp.recommended_action == "create_payment_link" and inp.existing_payment_link:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="BLOCK",
            reasons=["active_payment_link_exists_idempotency_protection"],
            allowed_actions=["resend_existing_link"],
            blocked_actions=["create_payment_link"],
        )

    # ── 6. Action and tier whitelists — fail closed ──────────────
    # The LLM is free to return any string. Anything the executor does not
    # recognise, or any evidence tier outside the documented three, is treated
    # as an unrecognised state and routed to a human rather than guessed at.
    if inp.recommended_action not in KNOWN_ACTIONS:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[f"unrecognised_recommended_action:{str(inp.recommended_action)[:60]}"],
            allowed_actions=["gather_information"],
            blocked_actions=["all_automated_actions"],
        )

    if inp.evidence_tier not in VALID_EVIDENCE_TIERS:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[f"unrecognised_evidence_tier:{str(inp.evidence_tier)[:60]}"],
            allowed_actions=["gather_information"],
            blocked_actions=["all_automated_actions"],
        )

    # ── 7. Evidence tier ─────────────────────────────────────────
    # LLM-DEPENDENT TRIGGER, tightening-only: an "unknown" tier removes
    # autonomy, it never grants it.
    if inp.evidence_tier in WEAK_EVIDENCE_TIERS:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=["unknown_evidence_tier_cannot_trigger_autonomous_action"],
            allowed_actions=["gather_information", "send_diagnostic_reminder"],
            blocked_actions=["send_collection_message", "escalate_to_collections"],
        )

    # The evidence tier is the model's own claim about how well grounded its
    # diagnosis is, so it is checked against the record rather than trusted.
    # "known" specifically asserts the client told us something. With an empty
    # client_notes field there is no client communication on record at all, so
    # the claim contradicts the invoice instead of describing it — and a
    # confidence label the record does not support must not be what buys
    # autonomy.
    #
    # "strong_inference" is treated differently on purpose: inferring from
    # payment history alone is legitimate with no notes (a reliably late payer
    # is diagnosable from their ledger). It is only stopped when the proposed
    # action would actually reach the client.
    if not notes.strip():
        if inp.evidence_tier == "known":
            return PolicyDecision(
                invoice_id=inp.invoice_id,
                decision="REQUIRE_HUMAN",
                reasons=["evidence_tier_known_contradicted_by_absent_client_communication"],
                allowed_actions=["gather_information", "wait"],
                blocked_actions=["send_reminder", "send_collection_message",
                                 "create_payment_link"],
            )

        # An inference with zero direct client communication, proposing an
        # action that contacts the client or asks for money, is inference
        # stacked on inference — the model cited nothing the client said.
        #
        # An earlier revision checked only `send_reminder`, so the same
        # unsupported inference proposing a payment link instead cleared every
        # rule and reached ALLOW. `wait` and `gather_information` stay
        # autonomous: neither contacts the client, and gathering evidence is
        # the correct response to having none.
        if (
            inp.evidence_tier == "strong_inference"
            and inp.recommended_action in ("send_reminder", "create_payment_link")
        ):
            return PolicyDecision(
                invoice_id=inp.invoice_id,
                decision="REQUIRE_HUMAN",
                reasons=["unsupported_inference_no_client_communication"],
                allowed_actions=["gather_information"],
                blocked_actions=["send_reminder", "send_collection_message",
                                 "create_payment_link"],
            )

    # ── 8. Severity ──────────────────────────────────────────────
    if inp.days_overdue > DAYS_OVERDUE_HARD_REVIEW:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=["invoice_overdue_beyond_60_days"],
            allowed_actions=["escalate_to_human"],
            blocked_actions=["send_automated_reminder"],
        )

    # Repeated automated contact with no resolution — sending yet another
    # reminder is not working and needs human judgment. Scoped to actions that
    # actually contact the client: an invoice that has had five reminders and
    # now needs `wait` or `gather_information` is not "reminder fatigue", and
    # escalating it was a false positive in the previous revision.
    if (
        inp.reminders_sent >= MAX_REMINDERS_BEFORE_ESCALATION
        and inp.recommended_action in ("send_reminder", "create_payment_link")
    ):
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=["reminder_fatigue_repeated_contact_unresolved"],
            allowed_actions=["escalate_to_human"],
            blocked_actions=["send_reminder"],
        )

    # A recommended action that is itself a human handoff (escalate_to_human,
    # flag_for_legal, cancel_duplicate) is never something to "ALLOW" as if
    # it were autonomous execution — the LLM is already asking for a human.
    if inp.recommended_action in HUMAN_ONLY_ACTIONS:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=["recommended_action_is_a_human_handoff_not_autonomous"],
            allowed_actions=[inp.recommended_action],
            blocked_actions=[],
        )

    # Belt and braces: by construction every remaining action is autonomous,
    # but an ALLOW is the only decision that reaches the executor, so the
    # invariant is asserted rather than assumed.
    if inp.recommended_action not in AUTONOMOUS_ACTIONS:
        return PolicyDecision(
            invoice_id=inp.invoice_id,
            decision="REQUIRE_HUMAN",
            reasons=[f"action_not_autonomously_executable:{inp.recommended_action}"],
            allowed_actions=["gather_information"],
            blocked_actions=["all_automated_actions"],
        )

    reasons = []
    if inp.days_overdue > DAYS_OVERDUE_ESCALATION:
        reasons.append("invoice_overdue_beyond_30_days_escalation_level")

    # ── 9. ALLOW ─────────────────────────────────────────────────
    return PolicyDecision(
        invoice_id=inp.invoice_id,
        decision="ALLOW",
        reasons=reasons if reasons else ["no_blocking_rules_fired"],
        allowed_actions=[inp.recommended_action],
        blocked_actions=[],
    )

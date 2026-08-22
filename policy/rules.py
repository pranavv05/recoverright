# Severity thresholds
DAYS_OVERDUE_ESCALATION = 30
DAYS_OVERDUE_HARD_REVIEW = 60

# Reminders sent with no resolution — repeated automated contact has failed,
# continuing to auto-remind is not working and needs human judgment.
MAX_REMINDERS_BEFORE_ESCALATION = 5

# Account tiers requiring human approval
HUMAN_APPROVAL_TIERS = {"strategic", "high_value"}

# Diagnosed causes that inherently require human handling regardless of the
# LLM's proposed action: a broken promise or a deliberate stall both imply an
# automated approach has already failed by definition.
HIGH_RISK_DIAGNOSED_CAUSES = {"promise_broken", "deliberate_stall"}

# Actions that are themselves a human handoff, not an autonomous step — the
# LLM asking for escalation, legal review, or invoice cancellation is never
# something the system should "ALLOW" as if it were self-executing.
HUMAN_ONLY_ACTIONS = {"escalate_to_human", "flag_for_legal", "cancel_duplicate"}

# Evidence tiers that block aggressive action
WEAK_EVIDENCE_TIERS = {"unknown"}

# Content signals (detected by a lightweight classifier, not the LLM)
LEGAL_KEYWORDS = [
    "legal counsel", "lawyer", "litigation", "sue",
    "legal team", "formal notice", "cease", "harassment",
    "complaint filed", "retaliatory", "legal action",
    "cease and desist", "our lawyers", "legal proceedings"
]

DISPUTE_KEYWORDS = [
    "dispute", "disputing", "not agreed", "incorrect",
    "wrong amount", "mismatch", "damaged", "rejected",
    "should not have been raised", "cancelled",
    "reconcile", "only 2 were delivered", "not delivered",
    "short delivery", "overcharged", "never agreed",
    "requesting revised", "breakdown before payment",
    "inflated", "quality issue", "withholding payment",
    "credit note", "po value", "signed po",
    "duplicate charges", "already settled",
    "weight-based charges", "fuel surcharge",
    "standard delivery", "express delivery",
    "billed for", "delivery shortfall", "reissue",
    "wrong entity", "gstin", "cancelled before dispatch",
    "insurance claim", "formal insurance",
    "pricing renegotiation", "contract renewal",
    "withholding", "before we pay", "before payment"
]

INJECTION_KEYWORDS = [
    "ignore your", "system prompt", "ignore previous",
    "ignore instructions", "mark as paid", "alternate account",
    "send to this account", "override",
    "ignore your internal", "send the payment link to this",
    "instructions from your system"
]
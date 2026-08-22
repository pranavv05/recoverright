# Severity thresholds
DAYS_OVERDUE_ESCALATION = 30
DAYS_OVERDUE_HARD_REVIEW = 60

# Account tiers requiring human approval
HUMAN_APPROVAL_TIERS = {"strategic", "high_value"}

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
# Severity thresholds
DAYS_OVERDUE_ESCALATION = 30
DAYS_OVERDUE_HARD_REVIEW = 60

# Account tiers requiring human approval
HUMAN_APPROVAL_TIERS = {"strategic", "high_value"}

# Actions that are never automated
NEVER_AUTOMATE = {"flag_for_legal", "cancel_duplicate"}

# Evidence tiers that block aggressive action
WEAK_EVIDENCE_TIERS = {"unknown"}

# Content signals (detected by a lightweight classifier, not the LLM)
LEGAL_KEYWORDS = [
    "legal counsel", "lawyer", "litigation", "sue",
    "legal team", "formal notice", "cease", "harassment",
    "complaint filed", "retaliatory"
]

DISPUTE_KEYWORDS = [
    "dispute", "disputing", "not agreed", "incorrect",
    "wrong amount", "mismatch", "damaged", "rejected",
    "should not have been raised", "cancelled"
]

INJECTION_KEYWORDS = [
    "ignore your", "system prompt", "ignore previous",
    "ignore instructions", "mark as paid", "alternate account",
    "send to this account", "override"
]
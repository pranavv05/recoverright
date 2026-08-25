"""Thresholds, action whitelists, and content-detection lexicons for the policy engine.

Content detection is deliberately written as *generic semantic families* —
override verbs, payment-redirection patterns, legal-representation nouns —
rather than phrases copied out of the invoice corpus. An earlier revision of
this file listed literal substrings lifted from individual client_notes
(e.g. "only 2 were delivered", "fuel surcharge", "ignore your internal");
those matched exactly one record each, inflated held-out safety metrics, and
caught nothing a real client would write. See README § Evaluation Methodology
for the disclosure.
"""

import re

# ── Severity thresholds ──────────────────────────────────────────────────
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
#
# NOTE: this rule reads the LLM's diagnosed_cause and is therefore the one
# place in the engine whose *trigger* is model-dependent. It can only ever
# make the outcome stricter (ALLOW → REQUIRE_HUMAN), never looser.
HIGH_RISK_DIAGNOSED_CAUSES = {"promise_broken", "deliberate_stall"}

# Actions that are themselves a human handoff, not an autonomous step — the
# LLM asking for escalation, legal review, or invoice cancellation is never
# something the system should "ALLOW" as if it were self-executing.
HUMAN_ONLY_ACTIONS = {"escalate_to_human", "flag_for_legal", "cancel_duplicate"}

# The complete set of actions the executor knows how to carry out. Anything
# outside this set — a hallucinated action, a renamed action, a prompt-injected
# action — must fail closed to REQUIRE_HUMAN rather than reaching execute_action().
KNOWN_ACTIONS = {
    "send_reminder",
    "create_payment_link",
    "gather_information",
    "escalate_to_human",
    "flag_for_legal",
    "cancel_duplicate",
    "wait",
}

# Actions the engine may autonomously ALLOW. Deliberately narrower than
# KNOWN_ACTIONS: HUMAN_ONLY_ACTIONS are recognised but always routed to a human.
AUTONOMOUS_ACTIONS = {
    "send_reminder",
    "create_payment_link",
    "gather_information",
    "wait",
}

VALID_EVIDENCE_TIERS = {"known", "strong_inference", "unknown"}

# Evidence tiers that block aggressive action
WEAK_EVIDENCE_TIERS = {"unknown"}


# ─────────────────────────────────────────────────────────────────────────
# Content detection
#
# All patterns are compiled regexes matched against lowercased notes with
# \b word boundaries, so "cease" no longer fires inside "deceased" and
# "sue" no longer fires inside "issue" (both real bugs in the substring
# version of this file).
# ─────────────────────────────────────────────────────────────────────────

def _compile(patterns: list) -> list:
    return [re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns]


# Client has engaged, or is invoking, legal process. Deliberately includes
# the Indian-English legal vocabulary an actual Indian B2B counterparty
# uses — advocate (the standard Indian term for a legal professional, not
# "lawyer"), vakeel (Hindi), attorney, solicitor, counsel/legal counsel,
# barrister, legal advisor — plus the phrases and statutory forums a
# receivables dispute in India actually escalates to: legal notice, legal
# recourse, court/consumer forum, arbitration, cease and desist, without
# prejudice. All patterns use \b word boundaries so "advocate" cannot match
# inside "advocacy" nor "counsel" inside "counselling".
#
# \b alone isn't enough for "advocate" and "counsel" specifically — both have
# a common non-legal verb sense as *separate whole words* ("advocate for
# timely payment", "counsel your team"), which \b does not exclude. Those are
# carved out with a negative lookahead here, and confirmed by
# LEGAL_FALSE_POSITIVE_PATTERNS + detect_legal() in engine.py, which applies
# the same deny-unless-corroborated logic used for dispute negation: a
# false-positive reading only suppresses the hit when nothing else in the
# note independently signals a legal threat.
LEGAL_PATTERNS = _compile([
    r"\b(lawyer|lawyers|attorney|attorneys|solicitor|solicitors|"
    r"barrister|barristers)\b",
    r"\badvocate(s)?\b(?!\s+for\b)",
    r"\bcounsel(s)?\b(?!\s+(your|them|him|her|us|our|his|its|their)\b)",
    r"\blegal\s+(counsel|team|department|cell|notice|action|proceedings?|"
    r"recourse|remedy|route|matter|advisor|advisors)\b",
    r"\breferred\s+\w{0,12}\s?to\s+(our\s+)?legal\s+team\b",
    r"\b(court|legal|judicial)\s+proceedings?\b",
    r"\bcourt\s+action\b",
    r"\b(litigation|litigate|sue|suing|sued|prosecute|arbitration|arbitral)\b",
    r"\bcease(\s+and\s+desist)?\b",
    r"\bwithout\s+prejudice\b",
    r"\b(formal|legal|statutory|demand)\s+notice\b",
    r"\b(complaint|case|suit|fir)\s+(filed|lodged|registered)\b",
    r"\b(consumer|civil|commercial)\s+court\b",
    r"\bconsumer\s+forum\b",
    r"\b(tribunal|nclt|msme\s+council|facilitation\s+council)\b",
    r"\bsection\s+138\b",                      # cheque dishonour, NI Act
    r"\b(harass|harassment|harassing|retaliat\w*|defamation)\b",
    r"\b(kanooni|vakil|vakeel|adalat|notice\s+bhej)\w*\b",   # transliterated Hindi
    r"\bcourt\s+(jaane|jaayenge|jaenge|jayenge|chalte)\w*\b",  # "going to court"
    r"\bkanoon(\s+ka)?\s+sahara\b",                             # "take legal recourse"
])

# The common non-legal sense of "advocate" (verb: to champion something) and
# "counsel" (verb: to advise someone) that the inline negative lookaheads
# above cannot fully rule out on their own — kept as a second signal so
# detect_legal() can confirm a false-positive reading the same way
# detect_dispute() confirms a negation, rather than trusting the lookahead in
# isolation.
LEGAL_FALSE_POSITIVE_PATTERNS = _compile([
    r"\badvocate(s)?\s+for\b",
    r"\bcounsel(s)?\s+(your|them|him|her|us|our|his|its|their)\b",
])

# Client contests the invoice itself — the amount, the goods, the paperwork,
# or whether it should have been raised at all. Semantic families only.
DISPUTE_PATTERNS = _compile([
    r"\b(dispute|disputed|disputing|disputes|contest(ing|ed)?|challeng(e|ing|ed))\b",
    r"\b(incorrect|wrong|inflated|excess(ive)?|overcharg\w*|overbill\w*|short[- ]?bill\w*)"
    r"\s+\w{0,12}\s?(amount|sum|price|rate|charge|charges|invoice|billing|entity|gstin)\b",
    r"\b(wrong|incorrect|mismatched?)\s+(amount|entity|gstin|po|figure|total)\b",
    r"\b(amount|invoice|billing|figure|total|quantity|qty)\s+\w{0,10}\s?"
    r"(mismatch\w*|discrepanc\w*|does\s+not\s+match|doesn'?t\s+match)\b",
    r"\b(damaged|defective|faulty|short[- ]?(delivered|delivery|shipped|supply)|"
    r"not\s+delivered|never\s+(delivered|received|arrived)|non[- ]?delivery)\b",
    r"\b(reject(ed|ing)?|return(ed|ing)?)\s+\w{0,10}\s?(goods|consignment|shipment|delivery|material)\b",
    r"\b(never|not)\s+(agreed|approved|authorised|authorized|sanctioned|signed\s+off)\b",
    # Refusing the terms — distinct from "cannot pay yet", which is cash flow.
    # `pay` is deliberately absent from the verb list for exactly that reason.
    r"\b(do\s+not|do\s+nt|don'?t|won'?t|will\s+not|refuse\s+to|cannot|can'?t)\s+"
    r"(accept|agree\s+(to|with)|acknowledge|honou?r|approve)\b",
    r"\b(more|higher|greater|less|lower|different)\s+than\s+"
    r"(the\s+|our\s+|what\s+was\s+)?(agreed|quoted|contracted|approved|discussed|negotiated|"
    r"the\s+po|the\s+contract)\b",
    r"\b(should\s+not\s+have\s+been\s+(raised|billed|invoiced)|raised\s+in\s+error|"
    r"billed\s+in\s+error)\b",
    # Double-billing claim, in noun and verb form alike — "duplicate charge"
    # and "duplicates charges already settled" are the same objection, and
    # matching only the noun let the verb phrasing through.
    r"\bduplicat\w+\b[\s\S]{0,20}?\b(invoice|invoices|billing|charge|charges|"
    r"amount|line\s+item)\b",
    r"\b(already|previously)\s+(settled|billed|invoiced|charged|paid\s+for)\b",
    r"\b(credit\s+note|debit\s+note|revised\s+invoice|reissue|re-?issue|rectif\w+)\b",
    r"\b(cancel|cancelled|canceled|cancellation)\s+\w{0,12}\s?"
    r"(order|po|invoice|shipment|consignment|dispatch|before)\b",
    r"\b(withhold\w*|holding\s+back|on\s+hold)\s+\w{0,10}\s?(payment|amount|settlement)\b",
    r"\b(quality|service|delivery|billing)\s+(issue|issues|problem|complaint|concern)s?\b",
    r"\b(against|exceeds|beyond|not\s+per)\s+(the\s+)?(po|purchase\s+order|contract|agreement|quote|quotation)\b",
    r"\b(reconcil\w+|breakdown|itemis\w+|itemiz\w+|clarif\w+)\s+\w{0,12}\s?"
    r"(before|prior\s+to)\s+\w{0,6}\s?(pay|payment|settl\w+|releas\w+)\b",
    r"\b(galat|galat\s+amount|nahi\s+manzoor|kam\s+maal|maal\s+kharab)\b",  # transliterated Hindi
])

# Phrases that, when they immediately precede a dispute cue, invert it:
# "this one's not in dispute at all" must not trip the dispute rule.
DISPUTE_NEGATION_PATTERNS = _compile([
    r"\b(not|no|never|isn'?t|aren'?t|wasn'?t|won'?t|nothing)\s+"
    r"(\w+\s+){0,3}(in\s+)?(dispute|disputed|disputing|contested|contesting|"
    r"an?\s+issue|a\s+problem|a\s+complaint)\b",
    r"\bno\s+(dispute|issue|problem|objection|disagreement|complaint)\b",
    r"\b(not|isn'?t|nothing)\s+(\w+\s+){0,2}(being\s+)?(disputed|contested|challenged|withheld)\b",
    r"\bundisputed\b",
])

# The client asserts money already moved outside our payment rails — NEFT,
# IMPS, RTGS, UPI, a cheque, a direct bank transfer — that our books have not
# reconciled. Collecting against such an invoice risks double-charging a
# client who has already paid, so it always goes to a human to verify against
# the bank statement first. This is the generalisable form of what an earlier
# revision encoded as the literal keyword "reconcile".
UNVERIFIED_PAYMENT_CLAIM_PATTERNS = _compile([
    r"\b(neft|imps|rtgs|upi)\b",
    r"\b(transferred|remitted|wired|deposited|credited|sent|paid)\b[\s\S]{0,60}?"
    r"\b(directly|to\s+your|into\s+your|your\s+(bank\s+)?account|by\s+(cheque|check|dd|"
    r"bank\s+transfer|wire))\b",
    r"\b(cheque|check|dd|demand\s+draft)\s+(no\.?|number|#)?\s*[\w-]*\s*"
    r"(has\s+been\s+|was\s+)?(sent|couriered|dispatched|deposited|handed)\b",
    r"\b(utr|imps\s+ref|transaction\s+(id|ref|reference)|payment\s+reference)\b",
    r"\b(already|has\s+been|have)\s+(paid|settled|cleared|remitted|transferred)\b"
    r"[\s\S]{0,40}?\b(check|verify|confirm|update)\s+\w{0,12}\s?(your|with)\b",
])

# The client is blocked on paperwork WE owe THEM — a GST-compliant tax
# invoice, an e-way bill, a TDS certificate, a signed PO or delivery challan.
# No reminder can unblock this; the action needed is on our side, so it goes
# to a human to issue the document. This is the generalisable form of what an
# earlier revision encoded as the literal keyword "gstin".
DOCUMENTATION_BLOCKER_PATTERNS = _compile([
    r"\b(gst|gst-?compliant|tax)\s+invoice\b",
    r"\bgstin\b",
    r"\b(e-?way\s*bill|lorry\s+receipt|delivery\s+challan|proof\s+of\s+delivery|"
    r"tds\s+certificate|form\s+16a|work\s+(completion|done)\s+certificate|"
    r"signed\s+(po|purchase\s+order|challan|acknowledg\w+))\b",
    r"\b(cannot|can'?t|unable\s+to|won'?t\s+be\s+able\s+to)\s+"
    r"(process|release|clear|make|initiate)\b[\s\S]{0,40}?\bwithout\b",
    r"\bwithout\s+(the\s+|a\s+|an\s+)?(tax\s+invoice|gst\s+invoice|invoice\s+copy|"
    r"document|documentation|paperwork|certificate|challan|e-?way\s*bill)\b",
    r"\binput\s+tax\s+credit\b",
    r"\b(share|send|issue|provide|resend|raise)\b[\s\S]{0,40}?"
    r"\b(tax\s+invoice|gst\s+invoice|e-?way\s*bill|challan|certificate)\b",
])

# The note itself hedges between two incompatible explanations. When the
# evidence on record says "it could be A or it could be B", the agent has no
# basis to pick one and act on it — that is precisely a human's call.
AMBIGUITY_PATTERNS = _compile([
    r"\b(could\s+be|might\s+be|may\s+be|either)\b[\s\S]{0,60}?\bor\s+(just|maybe|possibly|"
    r"perhaps|it\s+could|another|a\s+new)\b",
    r"\b(not\s+sure|unsure|unclear|hard\s+to\s+say|cannot\s+tell|can'?t\s+tell|ambiguous|"
    r"conflicting|contradictor\w+)\b[\s\S]{0,40}?\b(if|whether|which|what|why)\b",
    r"\b(claims?|says?|alleges?)\b[\s\S]{0,60}?\b(but|however|though|although)\b"
    r"[\s\S]{0,60}?\b(history|previously|before|pattern|record|past)\b",
    r"\b(genuine|real|legitimate)\b[\s\S]{0,30}?\bor\s+(just\s+)?(an?\s+)?"
    r"(excuse|stall|delay\s+tactic|pretext)\b",
])

# A payment commitment the client made and did not keep.
#
# Detected as two families that must BOTH appear in the note, because either
# one alone is ordinary collections correspondence: "we will pay on Friday"
# is a promise not yet broken, and "still unpaid" is the baseline condition
# of every invoice in the book. Only their co-occurrence describes a client
# who has already absorbed one round of trust — the point at which continuing
# to chase automatically stops being the right call and a human should decide.
#
# This exists because HIGH_RISK_DIAGNOSED_CAUSES reads the model's
# `diagnosed_cause`, so a broken promise the model labelled something milder
# (chronic_late_payer, forgotten_invoice) escaped escalation entirely and the
# invoice fell through to ALLOW. That made a safety outcome contingent on a
# sampled LLM call. These patterns read `client_notes` — merchant-authored
# data the model never touches — so the same evidence escalates on every run
# whether or not the model recognised it.
#
# Written as generic semantic families, per this file's header: commitment
# verbs and non-fulfilment markers, not phrases lifted from the corpus.
COMMITMENT_PATTERNS = _compile([
    # explicit promise / undertaking to pay
    r"\b(promis\w+|committ\w+|commitment|assur\w+|undert(ook|aken|ake)|pledg\w+|"
    r"guarante\w+|agreed)\b[\s\S]{0,60}?\b(pay|paid|payment|clear|clearing|settle|"
    r"settling|remit|remittance|transfer|dues|balance|amount|outstanding)\b",
    # reported speech: they said/told/confirmed they would pay
    r"\b(said|told|confirmed|informed|advised|stated|indicated)\b[\s\S]{0,50}?"
    r"\b(would|will|'?d|going\s+to|shall)\b[\s\S]{0,25}?"
    r"\b(pay|clear|settle|remit|release|process|transfer)\b",
    # payment asserted to be already in motion
    r"\b(cheque|check|payment|transfer|neft|rtgs|imps|funds)\b[\s\S]{0,40}?"
    r"\b(is\s+in\s+the\s+post|in\s+the\s+mail|on\s+the\s+way|has\s+been\s+"
    r"(sent|initiated|released)|being\s+processed|processing\s+today)\b",
    # the deferral commitment — the stall's characteristic promise to respond
    r"\b(will|shall|'?ll|and|to)\s+(revert|get\s+back|come\s+back|update\s+you|"
    r"take\s+(this\s+)?up|take\s+care\s+of\s+(it|this)|look\s+into\s+(it|this))\b",
    # reported speech asserting the payment is happening imminently, with no
    # modal to anchor the reported-speech pattern above ("said 'processing
    # today'", "confirmed releasing it tomorrow")
    r"\b(said|told|confirmed|assured|informed|advised)\b[\s\S]{0,40}?"
    r"\b(process\w*|clear\w*|releas\w*|pay\w*|initiat\w*|settl\w*)\b[\s\S]{0,20}?"
    r"\b(today|tomorrow|this\s+(week|month)|shortly|immediately|right\s+away)\b",
])

# The commitment above did not hold: a date that passed, money that never
# came, or the same answer repeated across reminder cycles.
COMMITMENT_LAPSE_PATTERNS = _compile([
    # a deadline that has come and gone
    r"\b(deadline|date|due\s+date|month|week|timeline)\b[\s\S]{0,30}?"
    r"\b(has\s+)?(now\s+)?(passed|elapsed|lapsed|expired|gone\s+by|closed)\b",
    r"\bpassed\s+(twice|again|already|without)\b",
    # the payment did not arrive
    r"\b(never\s+(arrived|came|materialis\w+|received)|did\s+not\s+(arrive|come|follow|"
    r"materialise)|didn'?t\s+(arrive|come|follow)|no\s+(payment|cheque|funds|remittance)\b"
    r"[\s\S]{0,25}?(received|since|yet|so\s+far)?|zero\s+payment)\b",
    r"\bstill\s+(unpaid|pending|outstanding|not\s+(paid|received|cleared))\b",
    # they went silent after committing
    r"\b(no\s+(response|reply|revert|update|follow-?up|further\s+(response|detail|reply))|"
    r"repeated\s+follow-?ups?\s+ignored|follow-?ups?\s+(ignored|unanswered)|"
    r"never\s+respond\w*)\b",
])

# The client has given the same non-answer across multiple reminder cycles.
#
# Unlike the two families above this one escalates on its own, because the
# repetition *is* the finding: an answer that has not changed in six weeks is
# not a commitment that might still be kept, it is evidence that automated
# contact has stopped working. Requiring a commitment phrase alongside it
# would miss exactly the stall that never promises anything — "this is with
# our approval committee" repeated for six weeks, which is the shape a
# deliberate stall actually takes.
#
# Every pattern here requires an explicit repetition marker (same/identical,
# an ordinal, a consecutive-period count). A note that merely describes one
# follow-up does not match.
STALL_REPETITION_PATTERNS = _compile([
    r"\b(same|identical)\s+(\w+\s+){0,2}(message|status|response|reply|excuse|"
    r"line|update|invoice|answer)\b",
    r"\b(second|third|fourth|fifth|sixth|\d+\s?(st|nd|rd|th))\s+time\b",
    r"\b(said|sent|quoted|repeated|told\s+us)\s+(this|the\s+same)\b[\s\S]{0,50}?"
    r"\b(ago|again|before|consecutive|reminders?|weeks?|months?|cycles?)\b",
    r"\b(for|over|across)\s+\d+\s+consecutive\s+"
    r"(weeks?|months?|reminders?|cycles?|follow-?ups?)\b",
    r"\b(in\s+response\s+to|after)\s+(the\s+)?(last\s+)?\d+\s+"
    r"(reminders?|follow-?ups?|emails?|calls?)\b",
    r"\b(repeatedly|over\s+and\s+over|every\s+single\s+time)\b",
])

# Attempts to steer the agent rather than describe the invoice. Detects
# semantic intent, not exact phrases, across five attack families: (1)
# instruction override — cancel/ignore/replace the system's rules, (2)
# payment redirection — move money to an account the invoice didn't specify,
# (3) invoice manipulation — declare the invoice paid/settled/closed without
# an actual payment, (4) credential/information extraction — solicit API
# keys, secrets, or the system prompt, (5) transliterated Hindi bypass
# attempts, plus raw Indian bank-account identifiers, which no legitimate
# client_notes field has any reason to carry.
INJECTION_PATTERNS = _compile([
    # override verb + directive object
    r"\b(ignore|disregard|forget|discard|skip|bypass|overrid\w+|suspend|"
    r"suppress|drop|set\s+aside|do\s+not\s+follow|stop\s+following)\b"
    r"[\s\S]{0,60}?\b(instruction|instructions|direction|directions|directive|directives|"
    r"rule|rules|policy|policies|protocol|guideline|guidelines|prompt|"
    r"escalation|restriction|restrictions|check|checks|guardrail|safeguard|"
    r"system|earlier|previous|prior|above|all)\b",
    # role / authority hijack
    r"\b(you\s+are\s+now|act\s+as|pretend\s+to\s+be|from\s+now\s+on|"
    r"new\s+(instruction|instructions|task|role)|as\s+(the\s+)?(admin|administrator|"
    r"system|developer|owner|superuser))\b",
    r"\b(system|developer|admin)\s+(prompt|message|instruction|override)\b",
    # payment redirection
    r"\b(send|transfer|remit|wire|deposit|route|redirect|pay)\b[\s\S]{0,50}?"
    r"\b(to\s+)?(this|the|another|alternate|alternative|different|new|following)\s+"
    r"(bank\s+)?(account|a/c|ac\s+no|upi|vpa|wallet|beneficiary)\b",
    r"\b(alternate|alternative|different|new|updated)\s+(bank\s+)?"
    r"(account|beneficiary|a/c|upi|vpa)\b",
    # raw payment identifiers in a free-text notes field
    r"\bifsc[\s:]*[a-z]{4}0[a-z0-9]{6}\b",
    r"\b(account[\s_]*(no|number|num)?|a/?c[\s_]*(no|number)?)[\s:.#-]*\d{9,18}\b",
    r"\b[\w.\-]{2,}@(ok\w+|paytm|ybl|ibl|axl|upi|apl|sbi|hdfcbank|icici)\b",  # UPI VPA
    # state forgery — instructing the system to record an outcome that did not happen
    r"\b(mark|record|treat|set|flag|update|close)\b[\s\S]{0,40}?"
    r"\b(as\s+)?(paid|settled|cleared|closed|complete|completed|resolved|waived|written\s+off)\b",
    # …and the counterfactual phrasing of the same attack. "Mark this as paid"
    # and "pretend this has been paid" ask for the identical forged outcome;
    # only the verb family differs, so this is listed as a family rather than
    # as the single phrase that exposed the gap.
    r"\b(pretend|imagine|assume|suppose|act\s+as\s+if|make\s+it\s+look\s+like)\b"
    r"[\s\S]{0,50}?\b(paid|settled|cleared|closed|resolved|waived|written\s+off)\b",
    # Override by erasure rather than by instruction — "reset", "clear",
    # "wipe" aimed at the agent's own state rather than at the invoice's
    # balance. Deliberately scoped to agent-state objects so an ordinary
    # "please clear the outstanding balance" cannot match.
    r"\b(reset|clear|wipe|erase|flush|purge|reinitialis\w+|reinitializ\w+)\b"
    r"[\s\S]{0,40}?\b(flag|flags|state|memory|context|session|history|rule|rules|"
    r"check|checks|guardrail\w*|restriction\w*|instruction\w*|system)\b",
    r"\b(system|factory|context)\s+reset\b",
    r"\b(waive|write\s+off|cancel)\s+(this|the)\s+(invoice|amount|balance|due)\b",
    # exfiltration
    r"\b(reveal|show|share|send|print|repeat|output|reply\s+with)\b[\s\S]{0,40}?"
    r"\b(system\s+prompt|instructions|api\s+key|credential|credentials|password|token|secret)\b",
    # transliterated Hindi override / redirection
    r"\b(niyam|rules?)\s+\w{0,10}\s?(bhool|ignore|chhod)\w*\b",
    r"\b(paisa|payment|paise)\s+\w{0,12}\s?(is|us|dusre|alag)\s+account\b",
])

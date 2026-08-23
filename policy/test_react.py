"""Tests for the ReAct two-call diagnosis pipeline: signal extraction, ReAct
reasoning-chain parsing, full diagnose(), and reasoning_trace audit logging.

The offline parsing test (test_parse_react_output_is_robust) needs neither
an API key nor a database. The other three call the real NVIDIA NIM API
(and, for the audit-trail test, the real database) — same pattern as
evaluation/run.py and policy/test_engine.py's fixtures, this codebase has no
mocked diagnosis path. They're skipped if NVIDIA_API_KEY isn't configured,
so `pytest` still runs cleanly without credentials.
"""

import json
import os

import pytest
from dotenv import load_dotenv

from diagnosis.diagnoser import (
    ACCOUNT_SENSITIVITY_LEVELS,
    CONTENT_SIGNALS,
    DAYS_OVERDUE_BUCKETS,
    ENGAGEMENT_SIGNALS,
    PAYMENT_HISTORY_PATTERNS,
    PAYMENT_LINK_SIGNAL_STATUSES,
    THOUGHT_KEYS,
    _extract_signals,
    _parse_react_output,
    diagnose,
)

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
API_KEY = os.environ.get("NVIDIA_API_KEY")
HAS_API_KEY = bool(API_KEY) and API_KEY != "your_nvidia_api_key_here"

requires_api = pytest.mark.skipif(not HAS_API_KEY, reason="NVIDIA_API_KEY not configured")

SIGNAL_ENUMS = {
    "engagement_signal": ENGAGEMENT_SIGNALS,
    "payment_history_pattern": PAYMENT_HISTORY_PATTERNS,
    "content_signal": CONTENT_SIGNALS,
    "days_overdue_bucket": DAYS_OVERDUE_BUCKETS,
    "account_sensitivity": ACCOUNT_SENSITIVITY_LEVELS,
    "payment_link_status": PAYMENT_LINK_SIGNAL_STATUSES,
}
REQUIRED_SIGNAL_FIELDS = set(SIGNAL_ENUMS) | {"content_detail", "evidence_available"}


def load_invoice(invoice_id: str) -> dict:
    """Loads a real invoice record from data/seen.json by id."""
    with open(os.path.join(DATA_DIR, "seen.json"), encoding="utf-8") as f:
        for invoice in json.load(f):
            if invoice["invoice_id"] == invoice_id:
                return invoice
    raise AssertionError(f"{invoice_id} not present in data/seen.json")


# ── Test 1: signal extraction (Call 1) ─────────────────────────────────────

@requires_api
def test_signal_extraction_returns_all_required_fields():
    """Call 1 must return all 8 signal fields, each holding a valid enum value."""
    invoice = load_invoice("INV-1001")
    signals = _extract_signals(invoice, API_KEY)

    assert REQUIRED_SIGNAL_FIELDS <= set(signals)

    for field, valid_values in SIGNAL_ENUMS.items():
        assert signals[field] in valid_values, f"{field}={signals[field]!r} not in {valid_values}"

    assert isinstance(signals["content_detail"], str)
    assert isinstance(signals["evidence_available"], bool)


# ── Test 2: ReAct output parsing is robust (offline) ────────────────────────

FULL_REACT_OUTPUT = """
Thought 1 — Engagement analysis:
The client opened one reminder but has not replied.

Thought 2 — Content analysis:
The client_notes state a specific delivery discrepancy.

Thought 3 — Payment history analysis:
Mostly on-time payments; within normal pattern.

Thought 4 — Evidence quality assessment:
Known — the client explicitly states the discrepancy.

Thought 5 — Cause and action determination:
This is a dispute; escalate to a human.

CONCLUSION:
{
  "invoice_id": "INV-TEST",
  "diagnosed_cause": "dispute",
  "evidence_tier": "known",
  "evidence_cited": "Client states a delivery discrepancy.",
  "recommended_action": "escalate_to_human",
  "reasoning": "Explicit dispute.",
  "suspicious_content_detected": false,
  "confidence": 0.9
}
"""

# Missing Thought 3 entirely.
MISSING_THOUGHT_OUTPUT = """
Thought 1 — Engagement analysis:
No engagement signals present.

Thought 2 — Content analysis:
Notes are empty.

Thought 4 — Evidence quality assessment:
Unknown, insufficient evidence.

Thought 5 — Cause and action determination:
Gather more information before acting.

CONCLUSION:
{
  "invoice_id": "INV-TEST",
  "diagnosed_cause": "unknown",
  "evidence_tier": "unknown",
  "evidence_cited": "No client communication on record.",
  "recommended_action": "gather_information",
  "reasoning": "Insufficient evidence.",
  "suspicious_content_detected": false,
  "confidence": 0.3
}
"""

# Just the CONCLUSION JSON, no Thought paragraphs at all.
JSON_ONLY_OUTPUT = """
{
  "invoice_id": "INV-TEST",
  "diagnosed_cause": "chronic_late_payer",
  "evidence_tier": "strong_inference",
  "evidence_cited": "Payment history shows repeated lateness.",
  "recommended_action": "send_reminder",
  "reasoning": "Pattern of late payment.",
  "suspicious_content_detected": false,
  "confidence": 0.6
}
"""


@pytest.mark.parametrize(
    "raw_output", [FULL_REACT_OUTPUT, MISSING_THOUGHT_OUTPUT, JSON_ONLY_OUTPUT],
    ids=["full", "missing_thought", "json_only"],
)
def test_parse_react_output_is_robust(raw_output):
    """A correctly formatted, a slightly malformed, and a minimally valid ReAct
    output must all parse into a valid diagnosis dict without crashing."""
    reasoning_trace, diagnosis = _parse_react_output(raw_output, "INV-TEST")

    assert set(reasoning_trace.keys()) == set(THOUGHT_KEYS)
    for value in reasoning_trace.values():
        assert isinstance(value, str)

    assert diagnosis["invoice_id"] == "INV-TEST"
    assert diagnosis["diagnosed_cause"]
    assert diagnosis["evidence_tier"] in {"known", "strong_inference", "unknown"}
    assert diagnosis["recommended_action"]


# ── Test 3: full diagnose() call on a real invoice ──────────────────────────

@requires_api
def test_diagnose_full_react_call_on_disputed_invoice():
    """INV-1001 is a dispute with an explicit client claim; the full two-call
    diagnose() must reach that conclusion and carry both the reasoning trace
    and signals.

    evidence_tier is asserted as either "known" or "strong_inference": across
    repeated live calls the 8B model splits roughly 5/8 "known" to 3/8
    "strong_inference" on this borderline case (the claim requires a small
    inferential step from "reconcile before we pay" to "dispute"), while
    diagnosed_cause and recommended_action are 100% stable. Pinning the tier
    to one value would make this test flaky on model sampling noise rather
    than on anything diagnose() controls.
    """
    invoice = load_invoice("INV-1001")
    diagnosis = diagnose(invoice, API_KEY)

    assert diagnosis["diagnosed_cause"] == "dispute"
    assert diagnosis["evidence_tier"] in ("known", "strong_inference")
    assert diagnosis["recommended_action"] == "escalate_to_human"

    trace = diagnosis["_reasoning_trace"]
    assert set(trace.keys()) == set(THOUGHT_KEYS)

    signals = diagnosis["_signals"]
    assert REQUIRED_SIGNAL_FIELDS <= set(signals)


# ── Test 4: reasoning_trace is logged to the audit trail ───────────────────

@requires_api
def test_reasoning_trace_logged_to_audit_trail():
    """process_invoice() must write a reasoning_trace audit event with all 5
    thoughts populated, for a real invoice run through the full pipeline."""
    from api.main import process_invoice
    from audit.trail import get_audit_trail

    invoice = load_invoice("INV-1001")
    process_invoice(invoice, API_KEY, rzp_client=None)

    trail = get_audit_trail("INV-1001")
    reasoning_events = [e for e in trail if e["event_type"] == "reasoning_trace"]
    assert reasoning_events, "no reasoning_trace event was logged for INV-1001"

    event = reasoning_events[-1]
    for key in THOUGHT_KEYS:
        assert event["diagnosis"].get(key), f"{key} was not populated in the logged reasoning_trace"
    assert "signals" in event["diagnosis"]

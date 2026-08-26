"""Shared fixtures for the tests/ suite.

Three conventions the rest of this directory relies on:

1. **No test ever calls a real LLM or a real Razorpay account.** Every test
   that touches diagnosis monkeypatches the OpenAI client or `diagnose`
   itself; every test that touches execution passes `rzp_client=None`. The
   whole suite runs with no API keys set.

2. **Database tests are additive, never destructive.** The project database
   holds a real processed batch (the dashboard and the submission demo read
   it), so `db_cleanup` deletes only the rows a test itself created, keyed on
   a per-test unique invoice_id prefix. `audit_log` and `recoveries` both
   cascade from `invoices`, so deleting the invoice row removes the whole
   trail. Set RECOVERRIGHT_TEST_TRUNCATE=1 to opt into a full truncate of the
   three tables instead — useful against a scratch database, ruinous against
   the demo one, which is why it is off by default.

3. **There is no pytest-asyncio in this project.** Async tests drive their own
   loop through `database.connection.run_async`, which is also the only loop
   psycopg's async connections can use on Windows.
"""

import json
import os
import uuid

import pytest
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

# The application calls load_dotenv() at import time, so a test file that
# happens to import api.main gets DATABASE_URL for free and one that does not
# silently skips every database test. Loading it here makes the environment
# identical no matter which subset of the suite is selected.
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# Every invoice_id a test writes to Postgres starts with this, so a stray row
# left behind by an interrupted run is identifiable and safe to delete.
TEST_ID_PREFIX = "ZZTEST-"


# ─────────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────────

def load_dataset(name: str) -> list:
    """Loads data/<name> and returns the parsed list of invoice records."""
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def unique_invoice_id(label: str = "inv") -> str:
    """A collision-free, clearly-marked invoice_id for a database test."""
    return f"{TEST_ID_PREFIX}{label}-{uuid.uuid4().hex[:12]}"


# ─────────────────────────────────────────────────────────────────────────
# Plain record fixtures
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_invoice():
    """A minimal, valid, entirely unremarkable invoice.

    Every field is a safe default: standard tier, clean notes, ten days
    overdue, no partial payment, no active link. Tests override only the one
    or two fields they are actually about, so a test's diff from this
    baseline *is* the thing under test.
    """
    return {
        "invoice_id": "INV-TEST-0001",
        "client_name": "Test Client Pvt Ltd",
        "client_type": "standard",
        "amount_paise": 10_00_000,
        "issue_date": "2026-07-01",
        "due_date": "2026-07-31",
        "days_overdue": 10,
        "partial_payment_received_paise": 0,
        "payment_link_status": "none",
        "payment_link_attempts": 0,
        "reminders_sent": 1,
        "reminders_opened": 1,
        "client_notes": "Ready to pay, just need the payment link resent.",
        "client_payment_history": ["paid_on_time", "paid_on_time"],
        "account_value_tier": "standard",
        "scenario_type": "test_fixture",
        "true_cause": "forgotten_invoice",
        "evidence_tier": "known",
        "recoverability": "recoverable",
        "correct_action": "send_reminder",
        "requires_human": False,
        "hard_stop": False,
    }


@pytest.fixture
def sample_diagnosis():
    """A minimal, valid, contract-shaped diagnosis dict.

    Matches exactly what `normalise_diagnosis()` returns — no more keys, no
    fewer — so a test that passes this to a worker is passing what the real
    pipeline passes.
    """
    return {
        "invoice_id": "INV-TEST-0001",
        "diagnosed_cause": "forgotten_invoice",
        "evidence_tier": "known",
        "recommended_action": "send_reminder",
        "evidence_cited": "Client acknowledged the invoice was missed.",
        "reasoning": "No dispute signal; history is clean; a reminder is proportionate.",
        "suspicious_content_detected": False,
        "confidence": 0.8,
    }


@pytest.fixture
def make_policy_input():
    """Factory for PolicyInput records, defaulting to a clean ALLOW-able invoice.

    Replaces the `make_input()` helper duplicated across policy/test_engine.py
    and policy/test_react.py. The defaults are deliberately identical to that
    helper's, so a test moved between the two files behaves the same —
    including the benign `client_notes`, which is present rather than empty
    because a "known" evidence tier on a note-less invoice is a
    contradiction the engine escalates on.
    """
    from policy.engine import PolicyInput

    def _make(**overrides):
        defaults = dict(
            invoice_id="INV-TEST",
            amount_paise=1_000_000,
            outstanding_balance_paise=1_000_000,
            days_overdue=10,
            account_value_tier="standard",
            evidence_tier="known",
            recommended_action="send_reminder",
            requested_amount_paise=1_000_000,
            existing_payment_link=False,
            client_notes="Ready to pay, just need the payment link resent.",
            hard_stop=False,
            diagnosed_cause="forgotten_invoice",
            reminders_sent=1,
        )
        defaults.update(overrides)
        return PolicyInput(**defaults)

    return _make


@pytest.fixture
def mock_llm_response():
    """Factory returning a raw model-shaped diagnosis dict, with overrides.

    This is the *pre*-normalisation shape — what the model returns inside its
    CONCLUSION JSON — so a test can feed it an out-of-contract value and watch
    `normalise_diagnosis()` fail closed on it.
    """
    def _make(**overrides):
        payload = {
            "diagnosed_cause": "forgotten_invoice",
            "evidence_tier": "known",
            "recommended_action": "send_reminder",
            "evidence_cited": "Client said the invoice slipped through the cracks.",
            "reasoning": "Clean history, no dispute signal, single missed invoice.",
            "suspicious_content_detected": False,
            "confidence": 0.82,
        }
        payload.update(overrides)
        return payload

    return _make


# ─────────────────────────────────────────────────────────────────────────
# Database fixtures
# ─────────────────────────────────────────────────────────────────────────

# `database_is_reachable()` and the `require_database` fixture live in the
# repo-root conftest.py, not here: test modules also live in policy/,
# execution/ and evaluation/, and a guard defined in this file would be
# invisible to all three. Fixtures below inherit it from there.


@pytest.fixture
def db_cleanup(require_database):
    """Yields a `register(invoice_id)` callable; removes those rows afterwards.

    Deleting the `invoices` row cascades to `audit_log` and `recoveries`
    (both carry ON DELETE CASCADE), so one delete per registered id clears the
    invoice's entire trail. Cleanup runs before and after the test body, which
    is what makes each test independent even when a previous run crashed
    part-way.

    Full-truncate mode is opt-in via RECOVERRIGHT_TEST_TRUNCATE=1. It is not
    the default because this project's database is also the demo database.
    """
    from database.connection import get_connection

    registered = []
    truncate = os.environ.get("RECOVERRIGHT_TEST_TRUNCATE") == "1"

    def _purge():
        with get_connection() as conn:
            with conn.cursor() as cur:
                if truncate:
                    cur.execute(
                        "TRUNCATE recoveries, audit_log, invoices RESTART IDENTITY CASCADE"
                    )
                    return
                # Registered ids plus any orphan left by an interrupted run.
                cur.execute(
                    "DELETE FROM invoices WHERE invoice_id = ANY(%s) OR invoice_id LIKE %s",
                    (registered or [""], TEST_ID_PREFIX + "%"),
                )

    def register(invoice_id: str) -> str:
        registered.append(invoice_id)
        return invoice_id

    _purge()
    yield register
    _purge()


@pytest.fixture
def insert_test_invoice(db_cleanup, sample_invoice):
    """Yields `create(**overrides) -> invoice_dict`, already persisted.

    Gives a test a real `invoices` row (satisfying the audit_log foreign key)
    under a unique, test-prefixed id, and registers it for cleanup.
    """
    from audit.trail import insert_invoice

    def _create(**overrides):
        invoice = dict(sample_invoice)
        invoice["invoice_id"] = db_cleanup(unique_invoice_id())
        invoice.update(overrides)
        insert_invoice(invoice)
        return invoice

    return _create


# ─────────────────────────────────────────────────────────────────────────
# FastAPI
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def fastapi_test_client():
    """A TestClient over the real app — no uvicorn, no network socket."""
    from fastapi.testclient import TestClient
    import api.main as main

    with TestClient(main.app) as client:
        yield client


@pytest.fixture
def mocked_pipeline_llm(monkeypatch):
    """Replaces `api.main.diagnose` with a canned diagnosis, and pins a fake API key.

    Every endpoint that ingests invoices (CSV upload, Razorpay sync, batch)
    runs one real LLM call per row. This fixture is what stops the test suite
    from spending API calls — and from depending on what a model happens to
    say on the day it runs.

    Returns a `set_diagnosis(**fields)` callable so a test can steer the
    pipeline down a specific branch (a dispute, an unknown evidence tier)
    without needing the model to cooperate.
    """
    import api.main as main

    current = {
        "diagnosed_cause": "forgotten_invoice",
        "evidence_tier": "known",
        "recommended_action": "send_reminder",
        "evidence_cited": "Mocked diagnosis — no LLM call was made.",
        "reasoning": "Mocked diagnosis — no LLM call was made.",
        "suspicious_content_detected": False,
        "confidence": 0.8,
    }

    def fake_diagnose(invoice, api_key=None):
        diagnosis = dict(current, invoice_id=invoice["invoice_id"])
        diagnosis["_reasoning_trace"] = {key: f"mocked {key}" for key in (
            "thought_1_engagement", "thought_2_content", "thought_3_payment_history",
            "thought_4_evidence_quality", "thought_5_cause_and_action",
        )}
        diagnosis["_signals"] = {
            "engagement_signal": "no_engagement",
            "payment_history_pattern": "no_history",
            "content_signal": "none",
            "notes_empty": not bool((invoice.get("client_notes") or "").strip()),
        }
        return diagnosis

    monkeypatch.setattr(main, "diagnose", fake_diagnose)
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key-not-a-real-credential")

    def set_diagnosis(**fields):
        current.update(fields)
        return current

    return set_diagnosis


@pytest.fixture
def no_audit_writes(monkeypatch):
    """Captures worker audit events in a list instead of writing to Postgres.

    Returns the list, so a test can assert on the event vocabulary a worker
    emits without needing a database at all.
    """
    written = []
    monkeypatch.setattr(
        "execution.workers.log_audit_event",
        lambda invoice_id, event_type, payload: written.append(
            {"invoice_id": invoice_id, "event_type": event_type, "payload": payload}
        ),
    )
    return written

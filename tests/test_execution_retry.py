"""Failure-recovery tests for the Razorpay execution path.

Two layers, exercised here with a fake client:

  * `create_payment_link_with_retry` — which errors are retried and which are
    surfaced immediately. Getting this backwards is expensive in both
    directions: retrying a 4xx wastes the budget on a request that can never
    succeed, and *not* retrying a 429 drops a recoverable invoice.
  * `PaymentLinkWorker`'s live branch — the five exception paths that turn an
    API failure into a result dict, so one invoice's outage cannot take down
    a batch.

`time.sleep` is stubbed throughout and the slept intervals are captured, which
makes the backoff schedule itself assertable instead of merely slow.

This file used to also test `execution.actions.execute_action` — a second,
unused dispatcher that duplicated `PaymentLinkWorker`'s routing and was never
called by the real pipeline (only by two demo scripts, which now call the
Orchestrator directly). It has been deleted rather than fixed, since the
routing and worker-execution behaviour it re-tested is already covered, via
the real production path, by:
  * `execution/test_workers.py` — `Orchestrator.route()` for every action,
    including `test_allow_unknown_action_defaults_to_escalation` for the
    fail-closed case `execute_action`'s `else` branch used to cover.
  * `tests/test_workers.py` — `WaitWorker`/`InformationWorker`'s actual
    execute() output (`test_wait_worker_records_the_non_action`,
    `test_information_worker_drafts_message_not_sends`, etc.), which covers
    what `execute_action`'s `wait`/`gather_information`/`send_reminder`
    branches used to check, more thoroughly.
No coverage was lost — only the dead code and the tests that existed solely
to describe it.
"""

import ast
import os
import re

import pytest

import execution.actions as actions
import execution.workers as workers
from execution.actions import (
    MaxRetriesExceeded,
    create_payment_link_with_retry,
)
from execution.razorpay_client import (
    DuplicateReferenceError,
    RateLimitError,
    ServerError,
)
from tests.conftest import PROJECT_ROOT

INVOICE = {
    "invoice_id": "INV-RETRY-001",
    "client_name": "Retry Test Client",
    "amount_paise": 250_000,
    "partial_payment_received_paise": 0,
}
OUTSTANDING = 250_000


class FakeRazorpay:
    """Raises a scripted sequence of errors, then succeeds.

    `script` is a list of exceptions-or-None consumed one per call; a None
    means "this call succeeds". Running off the end of the script also
    succeeds, so a test only has to describe the failures it cares about.
    """

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = 0

    def create_payment_link(self, invoice_id, amount_paise, client_name, description):
        self.calls += 1
        if self.script:
            error = self.script.pop(0)
            if error is not None:
                raise error
        return {
            "id": "plink_OK",
            "short_url": "https://rzp.io/i/OK",
            "reference_id": invoice_id,
            "amount": amount_paise,
        }


@pytest.fixture
def slept(monkeypatch):
    """Captures backoff intervals and makes them instant."""
    intervals = []
    monkeypatch.setattr(actions.time, "sleep", lambda s: intervals.append(s))
    return intervals


@pytest.fixture
def events(monkeypatch):
    written = []
    monkeypatch.setattr(
        actions, "log_audit_event",
        lambda invoice_id, event_type, payload: written.append((event_type, payload)),
    )
    return written


def types_of(events):
    return [event for event, _payload in events]


def call(client, max_retries=3):
    return create_payment_link_with_retry(
        rzp_client=client,
        invoice_id=INVOICE["invoice_id"],
        amount_paise=OUTSTANDING,
        client_name=INVOICE["client_name"],
        description="test",
        max_retries=max_retries,
    )


# ── Retried: rate limits ─────────────────────────────────────────────────

def test_first_attempt_success_neither_sleeps_nor_logs_a_retry(slept, events):
    result = call(FakeRazorpay())

    assert result["id"] == "plink_OK"
    assert slept == []
    assert types_of(events) == ["api_success"]


def test_a_rate_limit_is_retried_and_can_succeed(slept, events):
    client = FakeRazorpay([RateLimitError("429"), RateLimitError("429")])

    result = call(client)

    assert result["id"] == "plink_OK"
    assert client.calls == 3
    assert types_of(events) == ["api_retry", "api_retry", "api_success"]


def test_backoff_between_retries_is_exponential(slept, events):
    """attempt 0 → 1s, attempt 1 → 2s. The doubling is the point."""
    call(FakeRazorpay([RateLimitError("429"), RateLimitError("429")]))
    assert slept == [1, 2]


def test_exhausting_every_attempt_on_rate_limits_raises_max_retries(slept, events):
    client = FakeRazorpay([RateLimitError("429")] * 3)

    with pytest.raises(MaxRetriesExceeded) as excinfo:
        call(client)

    assert INVOICE["invoice_id"] in str(excinfo.value)
    assert client.calls == 3
    assert "api_failure_max_retries" in types_of(events)


def test_no_backoff_is_served_after_the_final_attempt(slept, events):
    """The loop is about to raise, so sleeping first only delays the failure.

    Two sleeps for three attempts, and the last api_retry event honestly
    records `wait_seconds: 0` rather than a delay that was never served.
    """
    call_failed = False
    try:
        call(FakeRazorpay([RateLimitError("429")] * 3))
    except MaxRetriesExceeded:
        call_failed = True

    assert call_failed
    assert slept == [1, 2]

    retry_waits = [
        payload["wait_seconds"] for event, payload in events if event == "api_retry"
    ]
    assert retry_waits == [1, 2, 0]


# ── Not retried: everything that retrying cannot fix ─────────────────────

def test_a_server_error_is_surfaced_immediately_without_retrying(slept, events):
    """5xx is Razorpay's problem — hammering it is not a recovery strategy."""
    client = FakeRazorpay([ServerError("503")])

    with pytest.raises(ServerError):
        call(client)

    assert client.calls == 1
    assert slept == []
    assert "api_failure" in types_of(events)


def test_a_client_error_is_surfaced_immediately_without_retrying(slept, events):
    """4xx means the request is wrong; a retry sends the same wrong request."""
    client = FakeRazorpay([ValueError("400 bad request")])

    with pytest.raises(ValueError):
        call(client)

    assert client.calls == 1
    assert slept == []


def test_a_duplicate_reference_is_surfaced_as_its_own_outcome(slept, events):
    """Razorpay's reference_id uniqueness refusing a second link is duplicate
    protection working, so it must not read as an outage in the trail."""
    client = FakeRazorpay([DuplicateReferenceError("exists")])

    with pytest.raises(DuplicateReferenceError):
        call(client)

    assert client.calls == 1
    assert "duplicate_link_prevented" in types_of(events)
    assert "api_failure" not in types_of(events)


# ── PaymentLinkWorker's live branch ──────────────────────────────────────

@pytest.fixture
def live_worker(monkeypatch, no_audit_writes):
    """PaymentLinkWorker with its collaborators stubbed, minus the API itself.

    `insert_recovery` and `reconcile_payment` are replaced so the worker's own
    branching is what gets exercised, not Postgres or a second HTTP call.
    """
    recoveries = []
    # Two distinct module-level names for the same function: the worker logs
    # through execution.workers, and create_payment_link_with_retry logs
    # through execution.actions. Stubbing only the first still reaches Postgres.
    monkeypatch.setattr(actions, "log_audit_event", lambda *a, **k: None)
    monkeypatch.setattr(workers, "insert_recovery", lambda **kw: recoveries.append(kw))
    monkeypatch.setattr(
        workers, "reconcile_payment",
        lambda **kw: {"status": "pending"},
    )
    monkeypatch.setattr(actions.time, "sleep", lambda s: None)
    return {"worker": workers.PaymentLinkWorker(), "recoveries": recoveries,
            "events": no_audit_writes}


def run_worker(live_worker, client):
    return live_worker["worker"].execute(
        invoice=dict(INVOICE), diagnosis={}, decision=None, rzp_client=client
    )


def test_live_link_creation_records_a_recovery_row_crediting_nothing(live_worker):
    """The row exists so the link can be reconciled later; it credits zero."""
    result = run_worker(live_worker, FakeRazorpay())

    assert result["status"] == "link_created"
    assert result["payment_link_id"] == "plink_OK"
    assert result["amount_requested_paise"] == OUTSTANDING
    assert len(live_worker["recoveries"]) == 1
    assert live_worker["recoveries"][0]["status"] == "link_created"
    assert "amount_recovered_paise" not in live_worker["recoveries"][0]


def test_live_link_creation_reconciles_inline(live_worker):
    """Verification is part of the pipeline, not a demo-only step."""
    result = run_worker(live_worker, FakeRazorpay())
    assert result["reconciliation"] == {"status": "pending"}


@pytest.mark.parametrize(
    "error,expected_status",
    [
        (DuplicateReferenceError("exists"), "duplicate_link_prevented"),
        (ServerError("503"), "failed_server_error"),
        (ValueError("400"), "failed_client_error"),
        (RuntimeError("something nobody predicted"), "failed"),
    ],
    ids=["duplicate", "server_error", "client_error", "unexpected"],
)
def test_the_worker_contains_every_failure_and_never_raises(
    live_worker, error, expected_status
):
    """One invoice's outage must not take down the other fifty-one.

    The RuntimeError case matters most: it is the branch for a defect rather
    than a known API condition, and it still has to return a result dict so
    the batch continues and the outcome never looks clean.
    """
    result = run_worker(live_worker, FakeRazorpay([error]))

    assert result["status"] == expected_status
    assert result["worker"] == "payment_link"
    assert "error" in result
    assert live_worker["recoveries"] == []


def test_a_rate_limited_link_reports_retries_exhausted(live_worker):
    result = run_worker(live_worker, FakeRazorpay([RateLimitError("429")] * 3))

    assert result["status"] == "failed_max_retries"
    assert live_worker["recoveries"] == []


def test_a_failed_worker_logs_the_failure_to_the_audit_trail(live_worker):
    run_worker(live_worker, FakeRazorpay([ServerError("503")]))

    logged = [e["event_type"] for e in live_worker["events"]]
    assert "worker_failed" in logged


# ── Regression guard: execution/actions.py cannot go dead again ──────────
#
# execute_action() was a full second dispatcher, called by nothing in api/,
# execution/workers.py, or audit/ — only by two demo scripts and by its own
# tests here. Nothing failed as it drifted from PaymentLinkWorker's actual
# behaviour, because nothing checked that a "production" function was
# reachable from production code at all. This asserts that property directly,
# so the next dead dispatcher fails a test instead of sitting unnoticed.

ACTIONS_PATH = os.path.join(PROJECT_ROOT, "execution", "actions.py")

# Directories whose reference to a name doesn't count as "used in
# production" — a demo script or a test file calling a function is exactly
# how execute_action stayed looking alive.
_NON_PRODUCTION_DIR_NAMES = {"demo", "tests"}
_SKIPPED_DIR_NAMES = {
    ".venv", "venv", "__pycache__", ".git", "node_modules", ".pytest_cache",
    ".coverage", "coverage", "dist", "build",
} | _NON_PRODUCTION_DIR_NAMES


def _top_level_function_names(path: str) -> set:
    """Public, module-level function names defined in a Python file, via AST."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return {
        node.name for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }


def _production_python_files():
    """Every first-party .py file that is not a test file and not a demo script.

    "Production" here means: reachable from the batch pipeline, the API, or
    the dashboard's backend — the paths a judge or a merchant actually
    exercises. A test file or a demo script referencing a name is exactly
    the situation this guard exists to catch, not evidence the name is safe.
    """
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIPPED_DIR_NAMES]
        for name in files:
            if not name.endswith(".py"):
                continue
            if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
                continue
            yield os.path.join(root, name)


def test_every_function_in_execution_actions_is_used_outside_tests_and_demos():
    """Every function execution/actions.py defines must be called from real code.

    Not "referenced somewhere" — referenced by at least one file that is
    neither a test nor a demo script. `execute_action` had references (two
    demo scripts, one test file) right up until it was deleted; that is
    precisely the gap this closes.
    """
    functions = _top_level_function_names(ACTIONS_PATH)
    assert functions, "execution/actions.py defines no top-level functions to check"

    production_sources = []
    for path in _production_python_files():
        if os.path.abspath(path) == os.path.abspath(ACTIONS_PATH):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            production_sources.append(f.read())

    unreferenced = [
        name for name in sorted(functions)
        if not any(re.search(rf"\b{re.escape(name)}\b", src) for src in production_sources)
    ]

    assert unreferenced == [], (
        f"execution/actions.py defines function(s) not referenced by any "
        f"non-test, non-demo source file: {unreferenced}. If real code no "
        f"longer needs one of these, delete the function rather than leaving "
        f"it reachable only from tests/ or demo/ — that is how execute_action "
        f"went dead without anything failing."
    )

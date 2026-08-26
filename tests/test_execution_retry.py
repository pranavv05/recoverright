"""Failure-recovery tests for the Razorpay execution path.

Three layers, all previously untested, all exercised here with a fake client:

  * `create_payment_link_with_retry` — which errors are retried and which are
    surfaced immediately. Getting this backwards is expensive in both
    directions: retrying a 4xx wastes the budget on a request that can never
    succeed, and *not* retrying a 429 drops a recoverable invoice.
  * `execute_action` — the dispatcher the demo scripts call, which must map
    every exception onto a status string rather than propagating.
  * `PaymentLinkWorker`'s live branch — the five exception paths that turn an
    API failure into a result dict, so one invoice's outage cannot take down
    a batch.

`time.sleep` is stubbed throughout and the slept intervals are captured, which
makes the backoff schedule itself assertable instead of merely slow.
"""

import pytest

import execution.actions as actions
import execution.workers as workers
from execution.actions import (
    MaxRetriesExceeded,
    create_payment_link_with_retry,
    execute_action,
)
from execution.razorpay_client import (
    DuplicateReferenceError,
    RateLimitError,
    ServerError,
)

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


# ── execute_action: every failure becomes a status, never an exception ───

def test_execute_action_creates_a_link_and_reports_the_outstanding_amount(slept, events):
    result = execute_action("create_payment_link", INVOICE, OUTSTANDING, FakeRazorpay())

    assert result["status"] == "link_created"
    assert result["payment_link_id"] == "plink_OK"
    assert result["amount_paise"] == OUTSTANDING


@pytest.mark.parametrize(
    "error,expected_status",
    [
        (RateLimitError("429"), "failed_max_retries"),
        (ServerError("503"), "failed_server_error"),
        (ValueError("400"), "failed_client_error"),
        (DuplicateReferenceError("exists"), "duplicate_link_prevented"),
    ],
    ids=["rate_limit", "server_error", "client_error", "duplicate"],
)
def test_execute_action_maps_every_api_failure_onto_a_status(
    slept, events, error, expected_status
):
    """No exception may escape the dispatcher — a batch has to keep moving."""
    script = [error] * 3 if isinstance(error, RateLimitError) else [error]

    result = execute_action(
        "create_payment_link", INVOICE, OUTSTANDING, FakeRazorpay(script)
    )

    assert result["status"] == expected_status


@pytest.mark.parametrize(
    "action,expected_status",
    [
        ("send_reminder", "reminder_simulated"),
        ("gather_information", "information_gathering_queued"),
        ("wait", "no_action_within_pattern"),
    ],
)
def test_execute_action_handles_the_non_payment_actions(events, action, expected_status):
    result = execute_action(action, INVOICE, OUTSTANDING, rzp_client=None)
    assert result["status"] == expected_status


def test_execute_action_fails_closed_on_an_action_it_does_not_know(events):
    """Anything the policy engine should have caught still must not silently pass."""
    result = execute_action("launch_the_missiles", INVOICE, OUTSTANDING, rzp_client=None)

    assert result["status"] == "unhandled_action"
    assert result["action"] == "launch_the_missiles"
    assert "action_unknown" in types_of(events)


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

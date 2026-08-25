"""Tests for the parallel batch path.

Five properties, one per thing that could go wrong once fifty-two invoices are
in flight at once:

    1. the LLM semaphore caps in-flight calls, and the pacer caps the rate
       at which they start — two different limits, both needed
    2. one invoice raising does not take the batch down with it
    3. concurrent writes through the async pool do not tread on each other
    4. a batch of N invoices returns exactly N results
    5. given the same diagnosis, the async pipeline does exactly what the
       synchronous one does — same decision, same worker, same audit trail

The fifth is the one that matters most and the one a live batch cannot show:
diagnosis is a sampled model call, so two runs of identical code already
disagree on a handful of invoices. Pinning the diagnosis removes that variance
and tests the refactor rather than the model.

Only test 3 touches Postgres, and it skips itself when no database is
reachable. The rest run offline: the LLM client is replaced with a fake, so
nothing here spends an API call or depends on what a model happens to say.

There is no pytest-asyncio in this project's dependencies, so each test drives
its own event loop through `database.connection.run_async` — which is also the
only way to get a loop psycopg can use on Windows.
"""

import asyncio
import json
import time

import pytest

import api.main as main
import diagnosis.diagnoser as diagnoser
import execution.workers as workers
from database.connection import (
    ASYNC_POOL_MAX_SIZE,
    close_pool,
    get_async_connection,
    run_async,
)
from diagnosis.diagnoser import MAX_CONCURRENT_LLM_CALLS


def run(coro):
    """Runs one coroutine on a fresh loop the async pool can use."""
    return run_async(coro)


# ─────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────

class ConcurrencyRecorder:
    """Counts how many callers are inside the tracked section simultaneously."""

    def __init__(self):
        self.current = 0
        self.peak = 0
        self.calls = 0

    def enter(self):
        self.calls += 1
        self.current += 1
        self.peak = max(self.peak, self.current)

    def exit(self):
        self.current -= 1


class FakeCompletions:
    """Stands in for client.chat.completions, recording overlap while it 'works'."""

    def __init__(self, recorder, hold_seconds=0.02):
        self._recorder = recorder
        self._hold = hold_seconds

    async def create(self, **kwargs):
        self._recorder.enter()
        try:
            await asyncio.sleep(self._hold)
        finally:
            self._recorder.exit()
        return _fake_response("some content")


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, recorder, hold_seconds=0.02):
        self.chat = FakeChat(FakeCompletions(recorder, hold_seconds))


def _fake_response(content: str):
    """Builds the minimal duck-typed shape _call_nvidia_async() reads."""
    class _Message:
        def __init__(self, text):
            self.content = text

    class _Choice:
        def __init__(self, text):
            self.message = _Message(text)
            self.finish_reason = "stop"

    class _Response:
        def __init__(self, text):
            self.choices = [_Choice(text)]

    return _Response(content)


def make_invoice(n: int) -> dict:
    """A minimal invoice with every field the pipeline reads."""
    return {
        "invoice_id": f"TEST-PARALLEL-{n:03d}",
        "client_name": f"Test Client {n}",
        "client_type": "standard",
        "amount_paise": 100_000,
        "due_date": "2026-01-01",
        "days_overdue": 20,
        "partial_payment_received_paise": 0,
        "payment_link_status": "none",
        "payment_link_attempts": 0,
        "reminders_sent": 1,
        "reminders_opened": 0,
        "client_notes": "",
        "client_payment_history": ["paid_on_time"],
        "account_value_tier": "standard",
        "scenario_type": "test",
        "true_cause": "unknown",
        "hard_stop": False,
    }


# ─────────────────────────────────────────────────────────────────────────
# Test 1 — the semaphore caps concurrent LLM calls
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def no_pacing(monkeypatch):
    """Neutralises the request pacer so a test can observe the semaphore alone.

    The pacer and the semaphore limit different things — requests *started*
    per second versus requests *in flight* — and with both active the pacer is
    the binding constraint for the instant fake calls used here: twenty calls
    spaced 0.4s apart never overlap, so peak concurrency is 1 and the
    semaphore is never exercised at all. Zeroing the interval isolates the
    property each test is actually about.
    """
    monkeypatch.setattr(diagnoser, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(diagnoser, "_request_interval", 0.0)
    monkeypatch.setattr(diagnoser, "_next_slot_at", 0.0)
    monkeypatch.setattr(diagnoser, "_cooldown_until", 0.0)


def test_llm_semaphore_caps_concurrency_at_the_configured_limit(no_pacing):
    """Twenty simultaneous calls must never put more than the cap on the wire."""
    recorder = ConcurrencyRecorder()
    client = FakeClient(recorder)

    async def scenario():
        await asyncio.gather(*(
            diagnoser._call_nvidia_async(
                client, f"INV-{i}",
                system_prompt="s", user_message="u",
                max_tokens=10, json_mode=False, call_label="test",
            )
            for i in range(20)
        ))
        return recorder

    result = run(scenario())

    assert result.calls == 20, "every call should have been made"
    assert result.peak <= MAX_CONCURRENT_LLM_CALLS, (
        f"peak concurrency {result.peak} exceeded the "
        f"{MAX_CONCURRENT_LLM_CALLS}-call limit"
    )
    assert result.peak == MAX_CONCURRENT_LLM_CALLS, (
        f"peak concurrency was only {result.peak} — the semaphore is throttling "
        f"below its limit, or the calls never actually overlapped"
    )


def test_llm_semaphore_queues_the_excess_rather_than_dropping_it(no_pacing):
    """The calls beyond the limit wait their turn; none is skipped or lost."""
    recorder = ConcurrencyRecorder()
    client = FakeClient(recorder)

    async def scenario():
        results = await asyncio.gather(*(
            diagnoser._call_nvidia_async(
                client, f"INV-{i}",
                system_prompt="s", user_message="u",
                max_tokens=10, json_mode=False, call_label="test",
            )
            for i in range(MAX_CONCURRENT_LLM_CALLS * 3)
        ))
        return results, recorder

    results, recorder = run(scenario())

    assert len(results) == MAX_CONCURRENT_LLM_CALLS * 3
    assert all(r == "some content" for r in results)
    assert recorder.current == 0, "every slot should have been released"


# ─────────────────────────────────────────────────────────────────────────
# Test 1b — the request pacer
#
# The semaphore alone was not enough. A 52-invoice batch stayed under a
# six-in-flight cap and still had fourteen invoices exhaust a six-attempt
# retry budget on 429s, because a concurrency cap does not bound the rate at
# which requests *start*. These pin the mechanism that fixed it.
# ─────────────────────────────────────────────────────────────────────────

def test_pacer_spaces_request_starts_by_the_configured_interval():
    """N slots take at least (N-1) intervals — the rate limit's actual unit."""
    interval = 0.05
    n = 6

    async def scenario():
        diagnoser.reset_rate_limit_state()
        diagnoser._request_interval = interval
        start = time.monotonic()
        await asyncio.gather(*(diagnoser._acquire_pace_slot() for _ in range(n)))
        return time.monotonic() - start

    elapsed = run(scenario())
    assert elapsed >= interval * (n - 1) * 0.8, (
        f"{n} slots took {elapsed:.3f}s — the pacer is not spacing request starts"
    )


def test_pacer_widens_on_rate_limit_and_recovers_on_success():
    """Multiplicative increase, geometric decay — and never past its bounds."""
    diagnoser.reset_rate_limit_state()
    baseline = diagnoser._request_interval

    diagnoser.note_rate_limited()
    widened = diagnoser._request_interval
    assert widened > baseline, "a 429 must slow the batch down"

    # Decay is gated on a run of clean responses, not a single one.
    for _ in range(diagnoser._SUCCESSES_BEFORE_SPEEDUP):
        diagnoser.note_request_succeeded()
    assert diagnoser._request_interval < widened, "sustained success must speed back up"

    for _ in range(200):
        diagnoser.note_rate_limited()
    assert diagnoser._request_interval <= diagnoser.MAX_REQUEST_INTERVAL_SECONDS

    for _ in range(2000):
        diagnoser.note_request_succeeded()
    assert diagnoser._request_interval >= diagnoser.MIN_REQUEST_INTERVAL_SECONDS

    diagnoser.reset_rate_limit_state()


def test_backoff_is_jittered_so_a_synchronised_herd_does_not_retry_together():
    """The property that actually broke the batch.

    With a bare `2 ** attempt`, fifty-two tasks that were rate limited at the
    same instant sleep for the identical interval and collide again on every
    retry. Distinct delays are the fix, so distinctness is what is asserted.
    """
    delays = {diagnoser._backoff_delay(3) for _ in range(50)}
    assert len(delays) > 25, "backoff is not jittered — retries will collide"
    cap = diagnoser.BACKOFF_BASE_SECONDS * (2 ** 2)
    assert all(0 <= d <= cap for d in delays), "jittered delay escaped its cap"


def test_backoff_honours_a_server_retry_after_header():
    """When the server says how long to wait, that is a floor, not a hint."""

    class FakeResponse:
        headers = {"retry-after": "7"}

    class FakeError(Exception):
        response = FakeResponse()

    assert diagnoser._retry_after_seconds(FakeError()) == 7.0
    assert diagnoser._backoff_delay(1, retry_after=7.0) >= 7.0

    class NoHeaders(Exception):
        response = None

    assert diagnoser._retry_after_seconds(NoHeaders()) == 0.0


def test_cooldown_from_one_task_is_observed_by_every_other_task():
    """A 429 is information about the shared endpoint, not about one invoice."""

    async def scenario():
        diagnoser.reset_rate_limit_state()
        diagnoser._open_cooldown(0.15)
        start = time.monotonic()
        await asyncio.gather(*(diagnoser._await_cooldown() for _ in range(4)))
        return time.monotonic() - start

    elapsed = run(scenario())
    diagnoser.reset_rate_limit_state()
    assert elapsed >= 0.15, (
        f"tasks resumed after {elapsed:.3f}s — one task's 429 did not hold the others back"
    )


# ─────────────────────────────────────────────────────────────────────────
# Test 2 — one invoice's failure does not take out the others
# ─────────────────────────────────────────────────────────────────────────

def test_one_failing_diagnosis_does_not_stop_the_other_invoices(monkeypatch):
    """A diagnose_async() that raises is contained to its own invoice, and that
    invoice fails closed to a human rather than leaving the batch."""
    failing_id = "TEST-PARALLEL-003"
    audited = []

    async def fake_diagnose_async(invoice, api_key=None):
        if invoice["invoice_id"] == failing_id:
            raise RuntimeError("NVIDIA NIM exploded for this invoice")
        return {
            "invoice_id": invoice["invoice_id"],
            "diagnosed_cause": "forgotten_invoice",
            "evidence_tier": "unknown",
            "recommended_action": "gather_information",
            "evidence_cited": "",
            "reasoning": "",
            "suspicious_content_detected": False,
            "confidence": 0.5,
            "_reasoning_trace": {},
            "_signals": {},
        }

    async def fake_audit(invoice_id, event_type, payload):
        audited.append((invoice_id, event_type))

    async def fake_insert(invoice):
        return None

    monkeypatch.setattr(main, "diagnose_async", fake_diagnose_async)
    monkeypatch.setattr(main, "log_audit_event_async", fake_audit)
    monkeypatch.setattr(main, "insert_invoice_async", fake_insert)
    monkeypatch.setattr(workers, "log_audit_event_async", fake_audit)

    invoices = [make_invoice(i) for i in range(6)]

    async def scenario():
        return await asyncio.gather(
            *(main.process_invoice_async(inv, "fake-key") for inv in invoices),
            return_exceptions=True,
        )

    results = run(scenario())

    assert len(results) == len(invoices)
    assert not any(isinstance(r, BaseException) for r in results), (
        "process_invoice_async should absorb a diagnosis failure, not raise"
    )

    failed = [r for r in results if r["invoice_id"] == failing_id]
    survived = [r for r in results if r["invoice_id"] != failing_id]

    assert len(failed) == 1
    assert failed[0]["status"] == "diagnosis_failed"
    assert "exploded" in failed[0]["error"]
    # Failing closed: the invoice still got a decision and a worker, so it is
    # in a human's queue rather than silently gone from the run.
    assert failed[0]["policy_decision"] == "REQUIRE_HUMAN"
    assert failed[0]["worker"]

    assert len(survived) == 5
    for result in survived:
        assert result.get("policy_decision") in ("ALLOW", "BLOCK", "REQUIRE_HUMAN")
        assert result.get("worker")

    assert (failing_id, "diagnosis_failed") in audited


def test_gather_return_exceptions_keeps_position_and_lets_siblings_finish():
    """The contract run_batch_async() relies on: one raise, the rest still land."""
    async def ok(value):
        await asyncio.sleep(0)
        return value

    async def boom():
        raise ValueError("this one only")

    async def scenario():
        return await asyncio.gather(
            ok("a"), boom(), ok("c"), return_exceptions=True
        )

    results = run(scenario())

    assert results[0] == "a"
    assert isinstance(results[1], ValueError)
    assert results[2] == "c"


# ─────────────────────────────────────────────────────────────────────────
# Test 3 — concurrent writes through the async pool
# ─────────────────────────────────────────────────────────────────────────

def _database_reachable() -> bool:
    try:
        from database.connection import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _database_reachable(),
    reason="Postgres is not reachable — start it with docker compose up -d",
)
def test_concurrent_writes_through_the_async_pool_all_succeed():
    """Thirty concurrent invoice + audit writes land without contention.

    Thirty is deliberately above ASYNC_POOL_MAX_SIZE, so the pool has to queue
    callers rather than hand every one its own connection.
    """
    from audit.trail import insert_invoice_async, log_audit_event_async

    count = 30
    assert count > ASYNC_POOL_MAX_SIZE
    invoices = [make_invoice(i) for i in range(count)]

    async def write_one(invoice):
        await insert_invoice_async(invoice)
        await log_audit_event_async(invoice["invoice_id"], "test_parallel_write", {
            "decision": "ALLOW",
            "reasons": ["concurrent_write_test"],
            "execution": {"status": "test"},
        })

    async def scenario():
        try:
            outcomes = await asyncio.gather(
                *(write_one(inv) for inv in invoices), return_exceptions=True
            )
            async with get_async_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT COUNT(*) AS n FROM invoices "
                        "WHERE invoice_id LIKE 'TEST-PARALLEL-%%'"
                    )
                    invoice_rows = (await cur.fetchone())["n"]
                    await cur.execute(
                        "SELECT COUNT(*) AS n FROM audit_log "
                        "WHERE event_type = 'test_parallel_write'"
                    )
                    audit_rows = (await cur.fetchone())["n"]
                    # ON DELETE CASCADE takes the audit rows with them.
                    await cur.execute(
                        "DELETE FROM invoices WHERE invoice_id LIKE 'TEST-PARALLEL-%%'"
                    )
            return outcomes, invoice_rows, audit_rows
        finally:
            await close_pool()

    outcomes, invoice_rows, audit_rows = run(scenario())

    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert not failures, f"concurrent writes failed: {failures[:3]}"
    assert invoice_rows == count
    assert audit_rows == count


# ─────────────────────────────────────────────────────────────────────────
# Test 4 — result count matches dataset size
# ─────────────────────────────────────────────────────────────────────────

def test_run_batch_async_returns_one_result_per_invoice(monkeypatch, tmp_path):
    """Ten invoices in, ten results out — including the ones that failed."""
    dataset = [make_invoice(i) for i in range(10)]
    dataset_path = tmp_path / "ten_invoices.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")

    failing_id = "TEST-PARALLEL-007"

    async def fake_diagnose_async(invoice, api_key=None):
        if invoice["invoice_id"] == failing_id:
            raise RuntimeError("diagnosis failed for this one")
        return {
            "invoice_id": invoice["invoice_id"],
            "diagnosed_cause": "forgotten_invoice",
            "evidence_tier": "unknown",
            "recommended_action": "gather_information",
            "evidence_cited": "",
            "reasoning": "",
            "suspicious_content_detected": False,
            "confidence": 0.5,
            "_reasoning_trace": {},
            "_signals": {},
        }

    async def noop_audit(invoice_id, event_type, payload):
        return None

    async def noop_insert(invoice):
        return None

    monkeypatch.setenv("NVIDIA_API_KEY", "test-key-not-used")
    monkeypatch.setattr(main, "diagnose_async", fake_diagnose_async)
    monkeypatch.setattr(main, "log_audit_event_async", noop_audit)
    monkeypatch.setattr(main, "insert_invoice_async", noop_insert)
    monkeypatch.setattr(workers, "log_audit_event_async", noop_audit)

    results = run(main.run_batch_async(str(dataset_path)))

    assert len(results) == len(dataset) == 10

    returned_ids = [r["invoice_id"] for r in results]
    assert returned_ids == [inv["invoice_id"] for inv in dataset], (
        "results must stay in dataset order even though invoices finish out of order"
    )

    failed = [r for r in results if r.get("status") == "diagnosis_failed"]
    assert len(failed) == 1 and failed[0]["invoice_id"] == failing_id

    # Every invoice gets a decision, the failed one included: it fails closed
    # to REQUIRE_HUMAN rather than leaving the batch without one.
    decided = [r for r in results if r.get("policy_decision")]
    assert len(decided) == 10
    assert failed[0]["policy_decision"] == "REQUIRE_HUMAN"

    # Nothing escaped as a bare exception record.
    assert not [r for r in results if r.get("status") == "error"]


def test_run_batch_async_respects_the_limit_argument(monkeypatch, tmp_path):
    """`limit` truncates the batch the same way it does in the sequential runner."""
    dataset = [make_invoice(i) for i in range(10)]
    dataset_path = tmp_path / "ten_invoices.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")

    async def fake_diagnose_async(invoice, api_key=None):
        return {
            "invoice_id": invoice["invoice_id"],
            "diagnosed_cause": "forgotten_invoice",
            "evidence_tier": "unknown",
            "recommended_action": "gather_information",
            "evidence_cited": "",
            "reasoning": "",
            "suspicious_content_detected": False,
            "confidence": 0.5,
            "_reasoning_trace": {},
            "_signals": {},
        }

    async def noop_audit(invoice_id, event_type, payload):
        return None

    async def noop_insert(invoice):
        return None

    monkeypatch.setenv("NVIDIA_API_KEY", "test-key-not-used")
    monkeypatch.setattr(main, "diagnose_async", fake_diagnose_async)
    monkeypatch.setattr(main, "log_audit_event_async", noop_audit)
    monkeypatch.setattr(main, "insert_invoice_async", noop_insert)
    monkeypatch.setattr(workers, "log_audit_event_async", noop_audit)

    results = run(main.run_batch_async(str(dataset_path), limit=3))

    assert len(results) == 3


# ─────────────────────────────────────────────────────────────────────────
# Test 5 — the async pipeline is the synchronous one, invoice for invoice
#
# The refactor's central claim is that it changed how many invoices run at
# once and nothing else. Live batches cannot demonstrate that: diagnosis is a
# sampled model call, so two runs of the *same* code already disagree on a
# handful of invoices. Pinning the diagnosis to a fixed value removes that
# variance and leaves only the thing under test — whether the two pipelines
# do the same work with the same input.
# ─────────────────────────────────────────────────────────────────────────

# Diagnoses chosen to reach a different worker each time: an ALLOW that routes
# to payment_link, an escalation, a block via hard_stop, and a wait.
EQUIVALENCE_CASES = [
    (
        "payment_link_failure",
        {"diagnosed_cause": "payment_link_failure", "evidence_tier": "explicit_client_statement",
         "recommended_action": "create_payment_link"},
        {},
    ),
    (
        "escalation",
        {"diagnosed_cause": "promise_broken", "evidence_tier": "explicit_client_statement",
         "recommended_action": "create_payment_link"},
        {},
    ),
    (
        "block_hard_stop",
        {"diagnosed_cause": "dispute", "evidence_tier": "explicit_client_statement",
         "recommended_action": "create_payment_link"},
        {"hard_stop": True},
    ),
    (
        "chronic_late_payer_wait",
        {"diagnosed_cause": "chronic_late_payer", "evidence_tier": "documented_pattern",
         "recommended_action": "wait"},
        {"client_payment_history": ["paid_late"] * 5},
    ),
    (
        "unknown_information",
        {"diagnosed_cause": "unknown", "evidence_tier": "unknown",
         "recommended_action": "gather_information"},
        {},
    ),
    (
        "high_value_tier",
        {"diagnosed_cause": "forgotten_invoice", "evidence_tier": "explicit_client_statement",
         "recommended_action": "create_payment_link"},
        {"account_value_tier": "high_value"},
    ),
]


def _fixed_diagnosis(invoice, overrides):
    """The diagnosis dict both pipelines will be handed, byte for byte."""
    diagnosis = {
        "invoice_id": invoice["invoice_id"],
        "diagnosed_cause": "unknown",
        "evidence_tier": "unknown",
        "recommended_action": "gather_information",
        "evidence_cited": "fixed evidence for the equivalence test",
        "reasoning": "fixed reasoning",
        "suspicious_content_detected": False,
        "confidence": 0.9,
        "_reasoning_trace": {f"thought_{i}": f"thought {i} text" for i in range(1, 6)},
        "_signals": {"engagement_signal": "no_engagement"},
    }
    diagnosis.update(overrides)
    return diagnosis


@pytest.mark.parametrize("label,diagnosis_overrides,invoice_overrides", EQUIVALENCE_CASES)
def test_async_pipeline_matches_sync_pipeline_given_the_same_diagnosis(
    monkeypatch, label, diagnosis_overrides, invoice_overrides
):
    """Same invoice, same diagnosis → same decision, same worker, same audit trail."""
    invoice = make_invoice(1)
    invoice.update(invoice_overrides)

    sync_events = []
    async_events = []

    def fake_diagnose(inv, api_key=None):
        return _fixed_diagnosis(inv, diagnosis_overrides)

    async def fake_diagnose_async(inv, api_key=None):
        return _fixed_diagnosis(inv, diagnosis_overrides)

    def fake_log(invoice_id, event_type, payload):
        sync_events.append((invoice_id, event_type, payload))

    async def fake_log_async(invoice_id, event_type, payload):
        async_events.append((invoice_id, event_type, payload))

    def fake_insert(inv):
        return None

    async def fake_insert_async(inv):
        return None

    # Synchronous run.
    monkeypatch.setattr(main, "diagnose", fake_diagnose)
    monkeypatch.setattr(main, "log_audit_event", fake_log)
    monkeypatch.setattr(main, "insert_invoice", fake_insert)
    monkeypatch.setattr(workers, "log_audit_event", fake_log)
    sync_result = main.process_invoice(invoice, "fake-key")

    # Asynchronous run.
    monkeypatch.setattr(main, "diagnose_async", fake_diagnose_async)
    monkeypatch.setattr(main, "log_audit_event_async", fake_log_async)
    monkeypatch.setattr(main, "insert_invoice_async", fake_insert_async)
    monkeypatch.setattr(workers, "log_audit_event_async", fake_log_async)
    async_result = run(main.process_invoice_async(invoice, "fake-key"))

    assert async_result["policy_decision"] == sync_result["policy_decision"], label
    assert async_result["policy_reasons"] == sync_result["policy_reasons"], label
    assert async_result["worker"] == sync_result["worker"], label
    assert async_result["diagnosis"] == sync_result["diagnosis"], label
    assert async_result["execution"] == sync_result["execution"], label

    # The audit trail must be the same events, in the same order, carrying the
    # same payloads — an outcome that is right but unexplained is not the same
    # outcome.
    assert [e[1] for e in async_events] == [e[1] for e in sync_events], label
    assert [e[2] for e in async_events] == [e[2] for e in sync_events], label


def test_async_pipeline_matches_sync_pipeline_on_a_zero_balance_invoice(monkeypatch):
    """The skip path — no diagnosis, no policy call — behaves identically too."""
    invoice = make_invoice(2)
    invoice["partial_payment_received_paise"] = invoice["amount_paise"]

    sync_events = []
    async_events = []

    monkeypatch.setattr(main, "log_audit_event", lambda i, e, p: sync_events.append((e, p)))
    monkeypatch.setattr(main, "insert_invoice", lambda inv: None)

    async def fake_log_async(i, e, p):
        async_events.append((e, p))

    async def fake_insert_async(inv):
        return None

    monkeypatch.setattr(main, "log_audit_event_async", fake_log_async)
    monkeypatch.setattr(main, "insert_invoice_async", fake_insert_async)

    sync_result = main.process_invoice(invoice, "fake-key")
    async_result = run(main.process_invoice_async(invoice, "fake-key"))

    assert sync_result == async_result
    assert sync_result["status"] == "skipped"
    assert async_events == sync_events


# ─────────────────────────────────────────────────────────────────────────
# Test 6 — the endpoint reaches the batch on whatever loop it is served on
#
# `/api/run-batch` is an async endpoint, so it runs on the server's event
# loop. On Windows that is a ProactorEventLoop, which psycopg's async
# connections cannot use, so _dispatch_batch_async() hands the batch to a
# worker thread with a selector loop instead. That fallback is easy to break
# and invisible until a deployment on Windows fails, so it is tested here.
# ─────────────────────────────────────────────────────────────────────────

def _mock_pipeline(monkeypatch):
    """Replaces the LLM and both audit writes so a batch runs offline."""
    async def fake_diagnose_async(invoice, api_key=None):
        return {
            "invoice_id": invoice["invoice_id"],
            "diagnosed_cause": "forgotten_invoice",
            "evidence_tier": "unknown",
            "recommended_action": "gather_information",
            "evidence_cited": "",
            "reasoning": "",
            "suspicious_content_detected": False,
            "confidence": 0.5,
            "_reasoning_trace": {},
            "_signals": {},
        }

    async def noop_audit(invoice_id, event_type, payload):
        return None

    async def noop_insert(invoice):
        return None

    monkeypatch.setenv("NVIDIA_API_KEY", "test-key-not-used")
    monkeypatch.setattr(main, "diagnose_async", fake_diagnose_async)
    monkeypatch.setattr(main, "log_audit_event_async", noop_audit)
    monkeypatch.setattr(main, "insert_invoice_async", noop_insert)
    monkeypatch.setattr(workers, "log_audit_event_async", noop_audit)


def _write_dataset(tmp_path, count=4):
    dataset_path = tmp_path / "dispatch_invoices.json"
    dataset_path.write_text(
        json.dumps([make_invoice(i) for i in range(count)]), encoding="utf-8"
    )
    return str(dataset_path)


def test_dispatch_runs_the_batch_directly_on_a_compatible_loop(monkeypatch, tmp_path):
    """When the serving loop can drive the pool, no thread hand-off happens."""
    _mock_pipeline(monkeypatch)
    dataset_path = _write_dataset(tmp_path)

    def exploding_blocking(*args, **kwargs):
        raise AssertionError("should not have been handed to a thread")

    monkeypatch.setattr(main, "run_batch_blocking", exploding_blocking)

    results = run(main._dispatch_batch_async(dataset_path))

    assert len(results) == 4


@pytest.mark.skipif(
    not hasattr(asyncio, "ProactorEventLoop"),
    reason="ProactorEventLoop only exists on Windows",
)
def test_dispatch_falls_back_to_a_worker_thread_on_an_incompatible_loop(
    monkeypatch, tmp_path
):
    """A ProactorEventLoop — what uvicorn serves on under Windows — still works."""
    _mock_pipeline(monkeypatch)
    dataset_path = _write_dataset(tmp_path)

    with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
        loop = runner.get_loop()
        assert not main.loop_supports_async_pg(loop), (
            "this test is pointless unless the loop really is incompatible"
        )
        results = runner.run(main._dispatch_batch_async(dataset_path))

    assert len(results) == 4
    assert all(r.get("policy_decision") for r in results)


# ─────────────────────────────────────────────────────────────────────────
# Rate pacing — the control the concurrency cap does not provide
# ─────────────────────────────────────────────────────────────────────────

def test_rate_pacer_spaces_out_every_consecutive_request_start():
    """Asserts the gap between *each* pair of starts, not just the total span.

    A pacer that released seven requests at once and the eighth a second later
    would pass a total-elapsed check while doing nothing useful. What the rate
    limit actually counts is the spacing of consecutive starts, so that is what
    is measured.
    """
    gap = 0.05
    starts = []

    async def scenario():
        diagnoser.reset_rate_limit_state()
        diagnoser._request_interval = gap

        async def one():
            await diagnoser._acquire_pace_slot()
            starts.append(asyncio.get_running_loop().time())

        await asyncio.gather(*(one() for _ in range(8)))

    try:
        run(scenario())
    finally:
        diagnoser.reset_rate_limit_state()

    assert len(starts) == 8
    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    # Generous slack for timer granularity; the point is that starts are
    # spread rather than simultaneous.
    assert all(g >= gap * 0.5 for g in gaps), f"requests were not paced apart: {gaps}"
    assert starts[-1] - starts[0] >= gap * 5, "eight paced calls should span several gaps"


def test_retry_after_header_is_preferred_over_a_guessed_backoff():
    """When the server says how long to wait, guessing is worse than listening."""
    class _Response:
        headers = {"retry-after": "3"}

    class _Error(Exception):
        response = _Response()

    assert diagnoser._retry_after_seconds(_Error()) == 3.0
    # Attempt 5 would otherwise draw from a much wider window; the header floors it.
    assert diagnoser._backoff_delay(5, diagnoser._retry_after_seconds(_Error())) >= 3.0


def test_backoff_is_capped_however_many_attempts_have_failed():
    """The ceiling holds at absurd attempt counts, so a batch cannot stall forever."""
    assert diagnoser._backoff_delay(20) <= diagnoser.BACKOFF_MAX_SECONDS
    assert diagnoser._backoff_delay(200) <= diagnoser.BACKOFF_MAX_SECONDS
    assert diagnoser._backoff_delay(1, retry_after=10_000) <= diagnoser.BACKOFF_MAX_SECONDS

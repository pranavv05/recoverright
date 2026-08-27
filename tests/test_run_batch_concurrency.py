"""Concurrency guard on POST /api/run-batch.

The dashboard's Run Batch button is an unauthenticated POST that spends real
LLM quota against a rate-limited free-tier key (see README § Limitations). A
second concurrent call while one batch is already running does not run a
second, independent batch faster — it competes with the first for the same
request budget, on top of re-processing the same data/seen.json for no
benefit. `api.routes._batch_lock` exists to reject the second call outright
rather than let it start.

Offline: `_dispatch_batch_async` and `score_client_health` are replaced with
fakes, so this needs no database, no NVIDIA key, and no Razorpay client. The
fake dispatcher blocks on an `asyncio.Event` the test controls, which is what
makes the race deterministic — a fake that returned immediately would let
the second request always arrive after the first had already released the
lock, making it impossible for this test to ever observe (or lose) the race
it exists to check.
"""

import asyncio

import httpx
from httpx import ASGITransport

import api.main as main
import api.routes as routes
from database.connection import run_async


def run(coro):
    """Runs one coroutine to completion on a fresh event loop."""
    return run_async(coro)


def test_a_concurrent_run_batch_request_is_rejected_with_409(monkeypatch):
    """First call proceeds and completes; a second call while it runs gets 409.

    The dispatcher must be reached exactly once — the guard has to stop the
    second call before it starts a second batch, not merely report a 409
    alongside one that ran anyway.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def fake_dispatch(dataset_path, rzp_client=None):
        nonlocal call_count
        call_count += 1
        started.set()
        await release.wait()
        return [{"invoice_id": "INV-FAKE", "policy_decision": "ALLOW"}]

    monkeypatch.setattr(routes, "_dispatch_batch_async", fake_dispatch)
    monkeypatch.setattr(routes, "score_client_health", lambda: [])
    # A fresh lock per test: the module-level singleton in api.routes would
    # otherwise carry a lock instance across tests and across whichever
    # order the full suite happens to run them in.
    monkeypatch.setattr(routes, "_batch_lock", asyncio.Lock())

    async def scenario():
        transport = ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first_task = asyncio.create_task(client.post("/api/run-batch"))

            # Deterministic handoff rather than a fixed sleep: proceed the
            # instant the first request has actually reached the dispatcher
            # and is holding the lock, whichever event loop tick that lands on.
            await asyncio.wait_for(started.wait(), timeout=5)

            # Bounded rather than a bare `await`: if the guard under test is
            # ever removed, the second request reaches the same fake
            # dispatcher and blocks on `release` too, which this test only
            # sets *after* getting a response — without the guard, that is a
            # deadlock, not a failing assertion. A regression must fail this
            # test in a few seconds, not hang the suite forever; discovered
            # by hitting exactly this hang once while verifying the guard
            # actually matters, and killing the run by hand.
            try:
                second_response = await asyncio.wait_for(
                    client.post("/api/run-batch"), timeout=5,
                )
            finally:
                release.set()
            first_response = await first_task
            return first_response, second_response

    first_response, second_response = run(scenario())

    assert first_response.status_code == 200
    assert first_response.json()["processed"] == 1

    assert second_response.status_code == 409
    assert "already running" in second_response.json()["detail"].lower()

    assert call_count == 1, "the second request must never reach the dispatcher at all"


def test_a_third_request_after_the_first_completes_is_accepted(monkeypatch):
    """The lock releases when a batch finishes — it is not stuck open forever."""
    call_count = 0

    async def fake_dispatch(dataset_path, rzp_client=None):
        nonlocal call_count
        call_count += 1
        return []

    monkeypatch.setattr(routes, "_dispatch_batch_async", fake_dispatch)
    monkeypatch.setattr(routes, "score_client_health", lambda: [])
    monkeypatch.setattr(routes, "_batch_lock", asyncio.Lock())

    async def scenario():
        transport = ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.post("/api/run-batch")
            second = await client.post("/api/run-batch")
            return first, second

    first_response, second_response = run(scenario())

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert call_count == 2

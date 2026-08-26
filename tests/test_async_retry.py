"""Async retry, backoff, cooldown and pacing in diagnosis.diagnoser.

`tests/test_diagnosis.py` covers the synchronous retry loop. Its async twin —
the one an actual batch runs — was untested, and it is the more interesting of
the two: it carries the four mechanisms README § Limitations describes as what
makes NVIDIA NIM's free tier survivable.

Each is asserted separately here, because each fixes a different failure and
three of them are individually insufficient:

  * **Full-jitter backoff** — a bare `2 ** attempt` makes tasks that were rate
    limited together retry together, colliding identically on every wave.
  * **A shared cooldown** — a 429 is information about the endpoint, not about
    the one invoice that received it, so every task must observe it.
  * **An adaptive pacer** — a semaphore caps requests *in flight*; a rate limit
    counts requests *started*. A batch can sit under its concurrency cap and
    still be far over the rate limit.
  * **Which errors are retried at all** — a 429 or a 5xx is transient, a 401
    never is, and burning six attempts on a bad credential wastes the budget
    an actually-recoverable invoice needed.

Backoff constants are shrunk to milliseconds rather than stubbing
`asyncio.sleep`, so the real timing path executes without the real waits.
"""

import httpx
import openai as openai_module
import pytest

import diagnosis.diagnoser as diagnoser
from database.connection import run_async
from tests.test_diagnosis import FakeClient, FakeResponse

VALID_JSON = '{"engagement_signal": "no_engagement"}'


def run(coro):
    return run_async(coro)


@pytest.fixture(autouse=True)
def fast_and_isolated(monkeypatch):
    """Millisecond backoff, and a clean pacer/cooldown for every test.

    The pacer and cooldown are module-level state shared across a batch by
    design, so without an explicit reset one test's 429 would widen the next
    test's request interval.
    """
    monkeypatch.setattr(diagnoser, "BACKOFF_BASE_SECONDS", 0.001)
    monkeypatch.setattr(diagnoser, "BACKOFF_MAX_SECONDS", 0.002)
    monkeypatch.setattr(diagnoser, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(diagnoser, "MAX_REQUEST_INTERVAL_SECONDS", 0.01)
    diagnoser.reset_rate_limit_state()
    yield
    diagnoser.reset_rate_limit_state()


def rate_limit_error(retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return openai_module.RateLimitError(
        "429 too many requests",
        response=httpx.Response(
            429, headers=headers,
            request=httpx.Request("POST", "https://example.invalid"),
        ),
        body=None,
    )


def status_error(code):
    return openai_module.APIStatusError(
        f"{code} error",
        response=httpx.Response(
            code, request=httpx.Request("POST", "https://example.invalid")
        ),
        body=None,
    )


def scripted(*outcomes):
    """A responder that yields each outcome in turn; exceptions are raised."""
    remaining = list(outcomes)

    def responder(_kwargs):
        outcome = remaining.pop(0) if remaining else FakeResponse(VALID_JSON)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return responder


def call(responder, parse_fn=None, max_tokens=700):
    client = FakeClient(responder, is_async=True)
    return diagnoser._call_nvidia_async(
        client, "INV-ASYNC-1",
        system_prompt="sys", user_message="user",
        max_tokens=max_tokens, json_mode=True,
        call_label="signal extraction", parse_fn=parse_fn,
    )


# ── Which errors are retried ─────────────────────────────────────────────

def test_a_rate_limited_call_is_retried_and_can_succeed():
    content = run(call(scripted(rate_limit_error(), rate_limit_error())))
    assert content == VALID_JSON


def test_a_server_error_is_retried_and_can_succeed():
    content = run(call(scripted(status_error(503))))
    assert content == VALID_JSON


def test_a_connection_error_is_retried_and_can_succeed():
    error = openai_module.APIConnectionError(
        request=httpx.Request("POST", "https://example.invalid")
    )
    content = run(call(scripted(error)))
    assert content == VALID_JSON


def test_a_timeout_is_retried_and_can_succeed():
    error = openai_module.APITimeoutError(
        request=httpx.Request("POST", "https://example.invalid")
    )
    content = run(call(scripted(error)))
    assert content == VALID_JSON


def test_a_client_error_is_not_retried_at_all():
    """A 401 is permanent. Retrying it six times spends the budget an
    actually-recoverable invoice needed, and still ends in the same failure."""
    calls = {"n": 0}

    def responder(_kwargs):
        calls["n"] += 1
        raise status_error(401)

    with pytest.raises(ValueError, match="NVIDIA NIM API error 401"):
        run(call(responder))

    assert calls["n"] == 1


def test_exhausting_every_attempt_raises_rather_than_returning_none():
    """Returning None would let a caller read "no diagnosis" as "nothing wrong"."""
    def responder(_kwargs):
        raise rate_limit_error()

    with pytest.raises(ValueError, match="after .* attempts"):
        run(call(responder))


def test_the_retry_budget_is_exactly_max_retries():
    calls = {"n": 0}

    def responder(_kwargs):
        calls["n"] += 1
        raise rate_limit_error()

    with pytest.raises(ValueError):
        run(call(responder))

    assert calls["n"] == diagnoser.MAX_RETRIES


# ── Bad responses are transient model failures, not permanent ────────────

def test_a_truncated_response_is_retried():
    truncated = FakeResponse('{"partial": ', finish_reason="length")
    content = run(call(scripted(truncated)))
    assert content == VALID_JSON


def test_an_empty_response_is_retried():
    content = run(call(scripted(FakeResponse(""))))
    assert content == VALID_JSON


def test_an_unparseable_response_is_retried_when_a_parser_is_given():
    """A malformed body is a transient model failure — the same prompt
    resampled usually parses — so it is retried rather than surfaced."""
    import json

    result = run(
        call(scripted(FakeResponse("not json at all")), parse_fn=json.loads)
    )
    assert result == {"engagement_signal": "no_engagement"}


# ── Full-jitter backoff ──────────────────────────────────────────────────

def test_backoff_is_drawn_from_a_range_not_a_fixed_interval():
    """The whole point: two tasks that failed together must not wake together.

    A bare `2 ** attempt` returns one value, so this samples the same attempt
    repeatedly and requires that it varies.
    """
    delays = {diagnoser._backoff_delay(3) for _ in range(50)}
    assert len(delays) > 1


def test_backoff_never_exceeds_the_configured_ceiling(monkeypatch):
    monkeypatch.setattr(diagnoser, "BACKOFF_BASE_SECONDS", 1.5)
    monkeypatch.setattr(diagnoser, "BACKOFF_MAX_SECONDS", 45.0)

    for attempt in range(1, 12):
        assert 0.0 <= diagnoser._backoff_delay(attempt) <= 45.0


def test_a_server_supplied_retry_after_is_honoured_as_a_floor(monkeypatch):
    """When the server says how long its window has left, that beats a guess —
    but jitter is still added so tasks released by it stay spread."""
    monkeypatch.setattr(diagnoser, "BACKOFF_MAX_SECONDS", 45.0)
    assert diagnoser._backoff_delay(1, retry_after=10.0) >= 10.0


@pytest.mark.parametrize(
    "raw,expected", [("5", 5.0), ("0", 0.0), ("not-a-number", 0.0), (None, 0.0)]
)
def test_retry_after_parsing_degrades_to_zero_rather_than_raising(
    monkeypatch, raw, expected
):
    """An unparseable header must fall back to jitter, not crash the retry.

    The ceiling is restored to its real value here because `_retry_after_seconds`
    clamps to it — under this module's millisecond test ceiling every header
    would parse to 0.002 and the parsing itself would go unasserted.
    """
    monkeypatch.setattr(diagnoser, "BACKOFF_MAX_SECONDS", 45.0)
    error = rate_limit_error(retry_after=raw) if raw is not None else rate_limit_error()
    assert diagnoser._retry_after_seconds(error) == expected


def test_a_retry_after_beyond_the_ceiling_is_clamped_to_it(monkeypatch):
    """A server asking for an hour must not stall the batch for an hour."""
    monkeypatch.setattr(diagnoser, "BACKOFF_MAX_SECONDS", 45.0)
    assert diagnoser._retry_after_seconds(rate_limit_error(retry_after="3600")) == 45.0


def test_a_negative_retry_after_is_floored_at_zero(monkeypatch):
    monkeypatch.setattr(diagnoser, "BACKOFF_MAX_SECONDS", 45.0)
    assert diagnoser._retry_after_seconds(rate_limit_error(retry_after="-10")) == 0.0


# ── The shared cooldown ──────────────────────────────────────────────────

def test_a_rate_limit_opens_a_cooldown_every_other_task_will_observe():
    """One task's 429 must pause the whole batch's next request.

    Backing off only the task that received it leaves the others hammering an
    API that has just said stop, so each collects its own 429 and the batch
    spends its retry budget learning the same fact N times.
    """
    assert diagnoser._cooldown_until == 0.0

    run(call(scripted(rate_limit_error())))

    assert diagnoser._cooldown_until > 0.0


def test_a_server_error_also_opens_the_shared_cooldown():
    """A 5xx from a shared endpoint is shared information too."""
    run(call(scripted(status_error(503))))
    assert diagnoser._cooldown_until > 0.0


def test_reset_clears_the_cooldown_between_batches():
    run(call(scripted(rate_limit_error())))
    assert diagnoser._cooldown_until > 0.0

    diagnoser.reset_rate_limit_state()

    assert diagnoser._cooldown_until == 0.0


# ── The adaptive pacer ───────────────────────────────────────────────────

def test_a_rate_limit_widens_the_interval_between_request_starts(monkeypatch):
    """Concurrency and rate are different quantities; this bounds the second."""
    monkeypatch.setattr(diagnoser, "MIN_REQUEST_INTERVAL_SECONDS", 0.4)
    monkeypatch.setattr(diagnoser, "MAX_REQUEST_INTERVAL_SECONDS", 4.0)
    diagnoser.reset_rate_limit_state()

    before = diagnoser._request_interval
    diagnoser.note_rate_limited()

    assert diagnoser._request_interval > before


def test_the_interval_is_capped_however_many_rate_limits_arrive(monkeypatch):
    monkeypatch.setattr(diagnoser, "MIN_REQUEST_INTERVAL_SECONDS", 0.4)
    monkeypatch.setattr(diagnoser, "MAX_REQUEST_INTERVAL_SECONDS", 4.0)
    diagnoser.reset_rate_limit_state()

    for _ in range(50):
        diagnoser.note_rate_limited()

    assert diagnoser._request_interval == 4.0


def test_sustained_success_narrows_the_interval_back_down(monkeypatch):
    """Without decay a batch parks at the ceiling after one early burst and
    takes far longer than the endpoint actually requires."""
    monkeypatch.setattr(diagnoser, "MIN_REQUEST_INTERVAL_SECONDS", 0.4)
    monkeypatch.setattr(diagnoser, "MAX_REQUEST_INTERVAL_SECONDS", 4.0)
    diagnoser.reset_rate_limit_state()

    diagnoser.note_rate_limited()
    widened = diagnoser._request_interval

    for _ in range(diagnoser._SUCCESSES_BEFORE_SPEEDUP):
        diagnoser.note_request_succeeded()

    assert diagnoser._request_interval < widened


def test_a_single_success_does_not_immediately_speed_the_batch_back_up(monkeypatch):
    """Recovery is deliberate, not instant — one good response after a 429 is
    not evidence the window has reopened."""
    monkeypatch.setattr(diagnoser, "MIN_REQUEST_INTERVAL_SECONDS", 0.4)
    monkeypatch.setattr(diagnoser, "MAX_REQUEST_INTERVAL_SECONDS", 4.0)
    diagnoser.reset_rate_limit_state()

    diagnoser.note_rate_limited()
    widened = diagnoser._request_interval
    diagnoser.note_request_succeeded()

    assert diagnoser._request_interval == widened


def test_the_interval_never_falls_below_the_configured_floor(monkeypatch):
    monkeypatch.setattr(diagnoser, "MIN_REQUEST_INTERVAL_SECONDS", 0.4)
    diagnoser.reset_rate_limit_state()

    for _ in range(100):
        diagnoser.note_request_succeeded()

    assert diagnoser._request_interval == 0.4


def test_reset_restores_the_configured_pace_between_batches(monkeypatch):
    """A run's timing should be a property of the run, not of what preceded it."""
    monkeypatch.setattr(diagnoser, "MIN_REQUEST_INTERVAL_SECONDS", 0.4)
    monkeypatch.setattr(diagnoser, "MAX_REQUEST_INTERVAL_SECONDS", 4.0)

    diagnoser.note_rate_limited()
    diagnoser.reset_rate_limit_state()

    assert diagnoser._request_interval == 0.4

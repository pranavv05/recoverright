"""Diagnosis module tests. Every LLM call is mocked — no API key, no network.

Two things are being tested here, and they are not the same thing:

  - **Parsing**: can the module survive what a 8B model actually emits?
    Markdown fences, a missing Thought, JSON with no prose around it, a
    truncated response. These are formatting failures and must degrade, not
    raise.
  - **Fail-closed normalisation**: what happens when the model returns a
    value that is *well-formed but not in the contract*? An invented cause,
    an invented evidence tier, an invented action. These must be coerced to
    the most cautious value and the original preserved under `*_raw`, because
    the alternative is an arbitrary model string reaching the policy engine.

The mock replaces `openai.OpenAI` / `openai.AsyncOpenAI` at the point
diagnosis/diagnoser.py looks them up, so the retry loop, the truncation check
and the empty-response check are all exercised by the real code path.
"""

import asyncio
import json

import pytest

import diagnosis.diagnoser as diagnoser
from database.connection import run_async
from diagnosis.diagnoser import (
    THOUGHT_KEYS,
    _extract_json,
    _parse_react_output,
    normalise_diagnosis,
    normalise_signals,
)


# ─────────────────────────────────────────────────────────────────────────
# Fake OpenAI client
# ─────────────────────────────────────────────────────────────────────────

class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = FakeMessage(content)
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [FakeChoice(content, finish_reason)]


class FakeCompletions:
    def __init__(self, responder):
        self._responder = responder

    def create(self, **kwargs):
        return self._responder(kwargs)


class FakeAsyncCompletions:
    def __init__(self, responder, delay=0.0, tracker=None):
        self._responder = responder
        self._delay = delay
        self._tracker = tracker

    async def create(self, **kwargs):
        if self._tracker is not None:
            self._tracker.enter()
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            return self._responder(kwargs)
        finally:
            if self._tracker is not None:
                self._tracker.exit()


class FakeClient:
    def __init__(self, responder, is_async=False, delay=0.0, tracker=None):
        completions = (
            FakeAsyncCompletions(responder, delay, tracker) if is_async
            else FakeCompletions(responder)
        )
        self.chat = type("Chat", (), {"completions": completions})()


class ConcurrencyTracker:
    """Records the high-water mark of simultaneously in-flight calls."""

    def __init__(self):
        self.active = 0
        self.peak = 0

    def enter(self):
        self.active += 1
        self.peak = max(self.peak, self.active)

    def exit(self):
        self.active -= 1


SIGNALS_JSON = json.dumps({
    "engagement_signal": "client_replied",
    "payment_history_pattern": "reliable",
    "content_signal": "none",
    "content_detail": "Client says it slipped through the cracks.",
    "days_overdue_bucket": "1-15",
    "account_sensitivity": "standard",
    "payment_link_status": "none",
    "evidence_available": True,
    "history_trend": "stable",
    "sudden_change_from_reliable": False,
    "lateness_consistency": "irregular",
    "specific_claim": "none",
    "willingness_to_pay": "unconditional",
    "future_commitment": "specific_date",
    "technical_issue_mentioned": False,
})

CONCLUSION = {
    "diagnosed_cause": "forgotten_invoice",
    "evidence_tier": "known",
    "recommended_action": "send_reminder",
    "evidence_cited": "Client acknowledged the invoice was missed.",
    "reasoning": "Clean history, explicit acknowledgement, no dispute signal.",
    "suspicious_content_detected": False,
    "confidence": 0.85,
}


def react_output(conclusion=None, thoughts=5):
    """Builds a realistic Call-2 response: N Thought paragraphs then CONCLUSION JSON."""
    body = "\n".join(
        f"Thought {n}: This is the reasoning for step {n}."
        for n in range(1, thoughts + 1)
    )
    payload = json.dumps(conclusion if conclusion is not None else CONCLUSION)
    return f"{body}\n\nCONCLUSION:\n{payload}"


@pytest.fixture
def mock_openai(monkeypatch):
    """Replaces openai.OpenAI with a fake whose two calls return canned text.

    Returns a `configure(signals=..., react=...)` callable so a test can pin
    exactly what each of the two ReAct calls emits. Call 1 is identified by
    its `response_format` (json_mode=True); Call 2 by its absence.
    """
    state = {"signals": SIGNALS_JSON, "react": react_output(), "calls": [], "responder": None}

    def responder(kwargs):
        state["calls"].append(kwargs)
        # A test that needs per-attempt behaviour (a flaky first response, say)
        # supplies its own responder rather than reaching around the fixture.
        if state["responder"] is not None:
            return state["responder"](kwargs)
        is_signal_call = "response_format" in kwargs
        content = state["signals"] if is_signal_call else state["react"]
        if isinstance(content, Exception):
            raise content
        if isinstance(content, FakeResponse):
            return content
        return FakeResponse(content)

    monkeypatch.setattr(
        diagnoser.openai, "OpenAI",
        lambda **kwargs: FakeClient(responder),
    )
    # The 0.5s intra-diagnosis stagger is real behaviour but pure latency;
    # the tests assert on output, not on pacing.
    monkeypatch.setattr(diagnoser.time, "sleep", lambda seconds: None)

    def configure(**overrides):
        state.update(overrides)
        return state

    configure.state = state
    return configure


@pytest.fixture
def invoice(sample_invoice):
    return dict(sample_invoice, client_notes="Apologies, this slipped through the cracks.")


# ─────────────────────────────────────────────────────────────────────────
# Contract — the shape diagnose() must always return
# ─────────────────────────────────────────────────────────────────────────

def test_diagnose_returns_all_required_fields(mock_openai, invoice):
    result = diagnoser.diagnose(invoice, api_key="test-key")
    for field in ("invoice_id", "diagnosed_cause", "evidence_tier", "evidence_cited",
                  "recommended_action", "reasoning", "suspicious_content_detected"):
        assert field in result, f"diagnosis missing required field {field!r}"
    assert result["invoice_id"] == invoice["invoice_id"]
    assert result["diagnosed_cause"] == "forgotten_invoice"


def test_diagnose_returns_reasoning_trace_with_five_thoughts(mock_openai, invoice):
    """The visible reasoning chain is what makes the model's logic auditable."""
    result = diagnoser.diagnose(invoice, api_key="test-key")
    trace = result["_reasoning_trace"]
    assert set(trace) == set(THOUGHT_KEYS)
    assert all(trace[key] for key in THOUGHT_KEYS)


def test_diagnose_returns_signals(mock_openai, invoice):
    """Call 1's eight-plus observable signals reach the audit trail intact."""
    result = diagnoser.diagnose(invoice, api_key="test-key")
    signals = result["_signals"]
    for field in ("engagement_signal", "payment_history_pattern", "content_signal",
                  "days_overdue_bucket", "account_sensitivity", "payment_link_status",
                  "evidence_available", "notes_empty"):
        assert field in signals, f"signals missing {field!r}"
    assert signals["notes_empty"] is False


def test_diagnose_makes_exactly_two_llm_calls(mock_openai, invoice):
    """ReAct is two calls: extract signals, then reason over them. Not one, not three."""
    diagnoser.diagnose(invoice, api_key="test-key")
    assert len(mock_openai.state["calls"]) == 2


def test_diagnose_fences_the_untrusted_client_notes_in_the_prompt(mock_openai, invoice):
    """Merchant/client text is delimited so it reads as data, not as instructions."""
    diagnoser.diagnose(invoice, api_key="test-key")
    first_user_message = mock_openai.state["calls"][0]["messages"][1]["content"]
    assert diagnoser.NOTES_FENCE in first_user_message


def test_diagnose_without_api_key_raises_before_any_network_call(invoice):
    with pytest.raises(ValueError, match="NVIDIA_API_KEY"):
        diagnoser.diagnose(invoice, api_key=None)


# ─────────────────────────────────────────────────────────────────────────
# Malformed output — parsing must degrade, not raise
# ─────────────────────────────────────────────────────────────────────────

def test_diagnose_handles_markdown_fenced_json(mock_openai, invoice):
    """The model wrapping Call 1's JSON in ```json fences must not fail the diagnosis."""
    mock_openai(signals=f"```json\n{SIGNALS_JSON}\n```")
    result = diagnoser.diagnose(invoice, api_key="test-key")
    assert result["diagnosed_cause"] == "forgotten_invoice"


def test_extract_json_strips_bare_and_labelled_fences():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('```\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_diagnose_handles_missing_thoughts(mock_openai, invoice):
    """Three of five Thoughts present: parsing succeeds, the absent keys stay present and empty."""
    mock_openai(react=react_output(thoughts=3))
    result = diagnoser.diagnose(invoice, api_key="test-key")
    trace = result["_reasoning_trace"]
    assert set(trace) == set(THOUGHT_KEYS)
    assert trace["thought_1_engagement"]
    assert trace["thought_4_evidence_quality"] == ""
    assert trace["thought_5_cause_and_action"] == ""


def test_diagnose_handles_json_only_output(mock_openai, invoice):
    """No Thoughts, no CONCLUSION marker — just the JSON. The diagnosis still lands."""
    mock_openai(react=json.dumps(CONCLUSION))
    result = diagnoser.diagnose(invoice, api_key="test-key")
    assert result["diagnosed_cause"] == "forgotten_invoice"
    assert all(value == "" for value in result["_reasoning_trace"].values())


def test_diagnose_handles_prose_after_the_conclusion(mock_openai, invoice):
    mock_openai(react=react_output() + "\n\nLet me know if you need anything else!")
    result = diagnoser.diagnose(invoice, api_key="test-key")
    assert result["recommended_action"] == "send_reminder"


def test_parse_react_output_ignores_json_inside_a_thought():
    """Brace counting must not let JSON quoted inside a Thought hijack the conclusion."""
    text = (
        'Thought 1: the model wrote {"not": {"the": "conclusion"}} in its prose.\n'
        "Thought 2: reasoning.\nThought 3: reasoning.\n"
        "Thought 4: reasoning.\nThought 5: reasoning.\n"
        f"CONCLUSION:\n{json.dumps(CONCLUSION)}"
    )
    _trace, diagnosis = _parse_react_output(text, "INV-TEST")
    assert diagnosis["diagnosed_cause"] == "forgotten_invoice"


def test_parse_react_output_raises_on_output_with_no_json():
    with pytest.raises(ValueError, match="not parseable"):
        _parse_react_output("Thought 1: I have no idea what to do here.", "INV-TEST")


def test_parse_react_output_raises_on_empty_output():
    with pytest.raises(ValueError, match="was empty"):
        _parse_react_output("   ", "INV-TEST")


# ─────────────────────────────────────────────────────────────────────────
# Fail-closed normalisation
# ─────────────────────────────────────────────────────────────────────────

def test_diagnose_normalises_invalid_cause(mock_openai, invoice, mock_llm_response):
    """`payment_failure` is not in VALID_CAUSES, so it degrades to `unknown`.

    The model is free to invent a plausible-sounding cause; the contract is
    not. What it actually said is preserved under `coerced_fields` so the
    audit trail shows the substitution rather than hiding it.
    """
    mock_openai(react=react_output(mock_llm_response(diagnosed_cause="payment_failure")))
    result = diagnoser.diagnose(invoice, api_key="test-key")
    assert result["diagnosed_cause"] == "unknown"
    assert result["coerced_fields"]["diagnosed_cause_raw"] == "payment_failure"


def test_diagnose_normalises_invalid_evidence_tier(mock_openai, invoice, mock_llm_response):
    mock_openai(react=react_output(mock_llm_response(evidence_tier="high_confidence")))
    result = diagnoser.diagnose(invoice, api_key="test-key")
    assert result["evidence_tier"] == "unknown"
    assert result["coerced_fields"]["evidence_tier_raw"] == "high_confidence"


def test_diagnose_normalises_invalid_action_to_gather_information(
    mock_openai, invoice, mock_llm_response
):
    """An invented action degrades to the least aggressive one in the contract."""
    mock_openai(react=react_output(mock_llm_response(recommended_action="wire_the_funds")))
    result = diagnoser.diagnose(invoice, api_key="test-key")
    assert result["recommended_action"] == "gather_information"


def test_normalise_diagnosis_clamps_confidence_into_range():
    assert normalise_diagnosis({"confidence": 7.5}, "INV-TEST")["confidence"] == 1.0
    assert normalise_diagnosis({"confidence": -3}, "INV-TEST")["confidence"] == 0.0
    assert normalise_diagnosis({"confidence": "nonsense"}, "INV-TEST")["confidence"] == 0.0


def test_normalise_diagnosis_defaults_everything_when_given_an_empty_object():
    """An empty JSON object is well-formed and must produce the safest possible diagnosis."""
    result = normalise_diagnosis({}, "INV-TEST")
    assert result["diagnosed_cause"] == "unknown"
    assert result["evidence_tier"] == "unknown"
    assert result["recommended_action"] == "gather_information"
    assert result["suspicious_content_detected"] is False


def test_normalise_diagnosis_rejects_a_non_object():
    with pytest.raises(ValueError, match="expected a JSON object"):
        normalise_diagnosis(["not", "an", "object"], "INV-TEST")


def test_normalise_diagnosis_truncates_unbounded_model_prose():
    """A model that rambles cannot write an unbounded string into the audit trail."""
    result = normalise_diagnosis({"reasoning": "x" * 10_000, "evidence_cited": "y" * 10_000},
                                 "INV-TEST")
    assert len(result["reasoning"]) == 2000
    assert len(result["evidence_cited"]) == 1000


def test_normalise_signals_computes_notes_empty_from_the_invoice_not_the_model(sample_invoice):
    """`notes_empty` is a fact the invoice already answers — the model is not consulted."""
    signals = normalise_signals({"notes_empty": False}, dict(sample_invoice, client_notes="   "))
    assert signals["notes_empty"] is True


def test_normalise_signals_falls_back_to_the_invoices_own_tier(sample_invoice):
    signals = normalise_signals({"account_sensitivity": "vip"},
                                dict(sample_invoice, account_value_tier="strategic"))
    assert signals["account_sensitivity"] == "strategic"


def test_normalise_signals_rejects_a_non_object(sample_invoice):
    with pytest.raises(ValueError, match="expected a JSON object"):
        normalise_signals("just a string", sample_invoice)


# ─────────────────────────────────────────────────────────────────────────
# API failure — must raise a typed error, never return a partial diagnosis
# ─────────────────────────────────────────────────────────────────────────

def test_diagnose_raises_on_api_timeout(mock_openai, invoice, monkeypatch):
    """Five timed-out attempts end in ValueError, not None and not an unhandled exception.

    Returning None here would be the dangerous failure: a caller that treats
    a missing diagnosis as "nothing suspicious" would hand a clean-looking
    record to the policy engine.
    """
    import httpx
    import openai as openai_module

    monkeypatch.setattr(diagnoser.time, "sleep", lambda seconds: None)
    mock_openai(signals=openai_module.APITimeoutError(
        request=httpx.Request("POST", "https://example.invalid")
    ))
    with pytest.raises(ValueError, match="failed for .* after .* attempts"):
        diagnoser.diagnose(invoice, api_key="test-key")


def test_diagnose_raises_on_non_retryable_api_error(mock_openai, invoice):
    """A 401 is permanent — it must fail immediately rather than burn five retries."""
    import httpx
    import openai as openai_module

    error = openai_module.APIStatusError(
        "unauthorized",
        response=httpx.Response(401, request=httpx.Request("POST", "https://example.invalid")),
        body=None,
    )
    mock_openai(signals=error)
    with pytest.raises(ValueError, match="NVIDIA NIM API error 401"):
        diagnoser.diagnose(invoice, api_key="test-key")


def test_diagnose_retries_a_truncated_response_then_succeeds(mock_openai, invoice):
    """`finish_reason == "length"` is a transient model failure, so it is retried."""
    attempts = {"n": 0}

    def flaky(kwargs):
        if "response_format" in kwargs:
            return FakeResponse(SIGNALS_JSON)
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeResponse('{"diagnosed_cause": "forg', finish_reason="length")
        return FakeResponse(react_output())

    mock_openai(responder=flaky)
    result = diagnoser.diagnose(invoice, api_key="test-key")
    assert result["diagnosed_cause"] == "forgotten_invoice"
    assert attempts["n"] == 2


def test_diagnose_retries_an_empty_response(mock_openai, invoice):
    attempts = {"n": 0}

    def flaky(kwargs):
        if "response_format" in kwargs:
            return FakeResponse(SIGNALS_JSON)
        attempts["n"] += 1
        return FakeResponse("" if attempts["n"] == 1 else react_output())

    mock_openai(responder=flaky)
    result = diagnoser.diagnose(invoice, api_key="test-key")
    assert result["evidence_tier"] == "known"
    assert attempts["n"] == 2


# ─────────────────────────────────────────────────────────────────────────
# Async path — same contract, plus the concurrency cap
# ─────────────────────────────────────────────────────────────────────────

def test_diagnose_async_returns_the_same_contract_as_diagnose(monkeypatch, invoice):
    """The async twin must be indistinguishable in output, or the two paths have drifted."""
    def responder(kwargs):
        return FakeResponse(SIGNALS_JSON if "response_format" in kwargs else react_output())

    monkeypatch.setattr(diagnoser, "_get_async_client",
                        lambda api_key: FakeClient(responder, is_async=True))
    monkeypatch.setattr(diagnoser, "INTRA_DIAGNOSIS_DELAY_SECONDS", 0)
    # See the note in test_diagnosis.py: pacing is asserted separately.
    monkeypatch.setattr(diagnoser, "MIN_SECONDS_BETWEEN_LLM_CALLS", 0)

    result = run_async(diagnoser.diagnose_async(invoice, api_key="test-key"))
    assert result["diagnosed_cause"] == "forgotten_invoice"
    assert set(result["_reasoning_trace"]) == set(THOUGHT_KEYS)
    assert "_signals" in result


def test_diagnose_semaphore_limits_concurrency_to_six(monkeypatch, sample_invoice):
    """Ten invoices in flight must never put more than MAX_CONCURRENT_LLM_CALLS on the wire.

    NVIDIA NIM's free tier will not take fifty-two simultaneous requests. The
    cap is the thing that makes the parallel batch usable at all, so it is
    asserted by observing actual in-flight calls rather than by reading the
    semaphore's internal counter.
    """
    tracker = ConcurrencyTracker()

    def responder(kwargs):
        return FakeResponse(SIGNALS_JSON if "response_format" in kwargs else react_output())

    monkeypatch.setattr(
        diagnoser, "_get_async_client",
        lambda api_key: FakeClient(responder, is_async=True, delay=0.02, tracker=tracker),
    )
    monkeypatch.setattr(diagnoser, "INTRA_DIAGNOSIS_DELAY_SECONDS", 0)
    # See the note in test_diagnosis.py: pacing is asserted separately.
    monkeypatch.setattr(diagnoser, "MIN_SECONDS_BETWEEN_LLM_CALLS", 0)

    async def run_ten():
        invoices = [dict(sample_invoice, invoice_id=f"INV-{n}") for n in range(10)]
        return await asyncio.gather(
            *(diagnoser.diagnose_async(inv, api_key="test-key") for inv in invoices)
        )

    results = run_async(run_ten())

    assert len(results) == 10
    assert tracker.peak <= diagnoser.MAX_CONCURRENT_LLM_CALLS, (
        f"{tracker.peak} concurrent LLM calls exceeded the cap of "
        f"{diagnoser.MAX_CONCURRENT_LLM_CALLS}"
    )
    assert tracker.peak > 1, "calls did not actually run concurrently — cap not exercised"


def test_diagnose_async_one_failure_does_not_take_down_the_others(monkeypatch, sample_invoice):
    """A batch is a set of independent invoices; one bad diagnosis must not cascade."""
    def responder(kwargs):
        content = kwargs["messages"][1]["content"]
        if "INV-BAD" in content and "response_format" not in kwargs:
            return FakeResponse("the model produced no JSON at all")
        return FakeResponse(SIGNALS_JSON if "response_format" in kwargs else react_output())

    monkeypatch.setattr(diagnoser, "_get_async_client",
                        lambda api_key: FakeClient(responder, is_async=True))
    monkeypatch.setattr(diagnoser, "INTRA_DIAGNOSIS_DELAY_SECONDS", 0)
    # See the note in test_diagnosis.py: pacing is asserted separately.
    monkeypatch.setattr(diagnoser, "MIN_SECONDS_BETWEEN_LLM_CALLS", 0)

    async def run_three():
        invoices = [dict(sample_invoice, invoice_id=name)
                    for name in ("INV-OK1", "INV-BAD", "INV-OK2")]
        return await asyncio.gather(
            *(diagnoser.diagnose_async(inv, api_key="test-key") for inv in invoices),
            return_exceptions=True,
        )

    results = run_async(run_three())
    assert results[0]["diagnosed_cause"] == "forgotten_invoice"
    assert isinstance(results[1], ValueError)
    assert results[2]["diagnosed_cause"] == "forgotten_invoice"

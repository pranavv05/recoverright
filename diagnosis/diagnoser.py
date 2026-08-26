import asyncio
import json
import logging
import os
import random
import re
import time

import openai

from diagnosis.prompts import (
    REACT_SYSTEM_PROMPT,
    REACT_USER_TEMPLATE,
    SIGNAL_EXTRACTION_SYSTEM_PROMPT,
    SIGNAL_EXTRACTION_USER_TEMPLATE,
)
from policy.rules import KNOWN_ACTIONS, VALID_EVIDENCE_TIERS

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """Reads a positive int from the environment, falling back to `default`."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer — using default %d", name, raw, default)
        return default
    if value < 1:
        log.warning("%s=%d must be >= 1 — using default %d", name, value, default)
        return default
    return value


def _env_float(name: str, default: float) -> float:
    """Reads a non-negative float from the environment, falling back to `default`."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number — using default %s", name, raw, default)
        return default
    if value < 0:
        log.warning("%s=%s must be >= 0 — using default %s", name, value, default)
        return default
    return value


NVIDIA_BASE_URL = os.getenv(
    "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
)
# meta/llama-3.3-70b-instruct was consistently timing out on NVIDIA's free
# tier at the time of integration; meta/llama-3.1-8b-instruct responded
# reliably and fast in repeated live checks.
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct")
MAX_RETRIES = _env_int("NVIDIA_MAX_RETRIES", 6)
REQUEST_TIMEOUT_SECONDS = _env_float("NVIDIA_REQUEST_TIMEOUT_SECONDS", 30.0)

# Stagger between the two ReAct calls for a single invoice, so a burst of
# invoices being diagnosed concurrently doesn't double the request rate at
# the exact same instant. The per-invoice concurrency limit lives with the
# batch runner; this only spaces out the two calls within one diagnosis.
INTRA_DIAGNOSIS_DELAY_SECONDS = _env_float("NVIDIA_INTRA_DIAGNOSIS_DELAY", 0.5)

# ── Backoff ──────────────────────────────────────────────────────────────
# Exponential, but *jittered*, and that jitter is the whole point rather
# than a nicety. A batch fans 52 invoices out at once; they hit the free
# tier's rate limit at roughly the same instant, and with a bare `2 **
# attempt` they then all sleep for exactly the same 2s and retry in
# lockstep. Every retry wave collides exactly like the first, all six
# attempts are burned inside a few seconds, and the batch reports
# `diagnosis_failed` for most of the book while the API was merely busy.
# Full jitter (`random.uniform(0, cap)`, per AWS's "Exponential Backoff and
# Jitter") spreads a synchronised herd across the window instead.
BACKOFF_BASE_SECONDS = _env_float("NVIDIA_BACKOFF_BASE_SECONDS", 1.5)
BACKOFF_MAX_SECONDS = _env_float("NVIDIA_BACKOFF_MAX_SECONDS", 45.0)


def _retry_after_seconds(error) -> float:
    """Seconds requested by a `Retry-After` header, or 0.0 if absent/unparseable.

    The server knows how long its own window has left; when it says so,
    that beats any locally guessed backoff. Only the delta-seconds form is
    honoured — the HTTP-date form is not what these APIs send, and guessing
    at clock skew to parse it would be worse than falling back to jitter.
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return 0.0
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return 0.0
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return 0.0
    return min(max(seconds, 0.0), BACKOFF_MAX_SECONDS)


def _backoff_delay(attempt: int, retry_after: float = 0.0) -> float:
    """Full-jitter exponential backoff, floored by any server `Retry-After`.

    `attempt` is 1-based. The cap grows as base * 2**(attempt-1), and the
    actual sleep is drawn uniformly from [0, cap] so concurrent callers
    that failed together do not retry together.
    """
    cap = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
    jittered = random.uniform(0.0, cap)
    # Retry-After is a floor, not a replacement: if every task honoured the
    # identical server-supplied delay and nothing else, they would resynchronise
    # on it. Adding a fraction of the jitter keeps them spread.
    if retry_after > 0:
        return min(retry_after + jittered * 0.25, BACKOFF_MAX_SECONDS)
    return jittered


VALID_CAUSES = {
    "dispute", "payment_link_failure", "chronic_late_payer", "forgotten_invoice",
    "wrong_contact", "cash_flow_delay", "promise_broken", "deliberate_stall",
    "legal_threat", "duplicate_invoice", "unknown",
}

# Valid values for the signals extracted by Call 1. Kept in code as well as
# in the prompt so a malformed model response degrades to a safe default
# instead of reaching the audit trail (and the ReAct reasoning call) as an
# arbitrary string — the same fail-closed pattern normalise_diagnosis() uses.
ENGAGEMENT_SIGNALS = {
    "client_initiated_contact", "opened_reminders_no_response",
    "no_engagement", "partial_engagement", "client_replied",
}
PAYMENT_HISTORY_PATTERNS = {
    "perfect", "reliable", "chronic_late", "deteriorating", "missed_recently", "no_history",
}
CONTENT_SIGNALS = {
    "explicit_dispute", "explicit_complaint", "technical_failure_reported",
    "payment_intent_expressed", "legal_language", "suspicious_instruction",
    "silence", "none",
}
DAYS_OVERDUE_BUCKETS = {"1-15", "16-30", "31-60", "60+"}
ACCOUNT_SENSITIVITY_LEVELS = {"standard", "high_value", "strategic"}
PAYMENT_LINK_SIGNAL_STATUSES = {"expired_confirmed", "active", "none", "unknown"}

# Granular signals added to give Call 2 more precise inputs than a single
# broad category — see diagnosis/prompts.py for the differentiators each one
# is meant to support.
HISTORY_TRENDS = {"improving", "stable", "deteriorating", "insufficient_data"}
LATENESS_CONSISTENCY_LEVELS = {"consistent_same_amount", "irregular", "not_applicable"}
SPECIFIC_CLAIM_TYPES = {"quantity", "amount", "quality", "none"}
WILLINGNESS_TO_PAY_LEVELS = {"unconditional", "conditional", "refused", "none_expressed"}
FUTURE_COMMITMENT_TYPES = {"specific_date", "vague_future", "none"}

# The five reasoning-chain keys, in order. Always present in the returned
# trace, even when the model's output didn't contain that Thought — a
# missing Thought degrades to "", it never removes the key.
THOUGHT_KEYS = [
    "thought_1_engagement",
    "thought_2_content",
    "thought_3_payment_history",
    "thought_4_evidence_quality",
    "thought_5_cause_and_action",
]

# Marks the boundary of untrusted merchant/client text inside the prompt.
NOTES_FENCE = "<<<CLIENT_NOTES>>>"


def _invoice_context(invoice: dict) -> dict:
    """Formats one invoice's fields for the diagnosis prompt templates.

    client_notes is untrusted third-party text. It is stripped of the fence
    marker and wrapped in it, so a note cannot close the field early and forge
    the surrounding structure. Shared by both the signal-extraction and
    ReAct-reasoning user prompts so the two calls see identical invoice data.
    """
    outstanding = (
        invoice["amount_paise"] - invoice["partial_payment_received_paise"]
    ) // 100

    notes = (invoice.get("client_notes") or "").replace(NOTES_FENCE, "")

    return dict(
        invoice_id=invoice["invoice_id"],
        client_name=invoice["client_name"],
        client_type=invoice["client_type"],
        amount_rupees=invoice["amount_paise"] // 100,
        outstanding_rupees=outstanding,
        days_overdue=invoice["days_overdue"],
        payment_link_status=invoice["payment_link_status"],
        payment_link_attempts=invoice["payment_link_attempts"],
        reminders_sent=invoice["reminders_sent"],
        reminders_opened=invoice["reminders_opened"],
        payment_history=", ".join(invoice["client_payment_history"]),
        account_value_tier=invoice["account_value_tier"],
        notes_fence=NOTES_FENCE,
        client_notes=notes or "(none)",
    )


def _bucket_days_overdue(days_overdue: int) -> str:
    """Deterministic fallback bucket, used when the model's own bucket is invalid."""
    if days_overdue <= 15:
        return "1-15"
    if days_overdue <= 30:
        return "16-30"
    if days_overdue <= 60:
        return "31-60"
    return "60+"


def _pick(raw: dict, field: str, valid: set, fallback: str) -> tuple:
    """Returns (value, raw_value_if_coerced). Lowercases and validates against `valid`."""
    value = raw.get(field)
    cleaned = value.strip().lower() if isinstance(value, str) else None
    if cleaned in valid:
        return cleaned, None
    return fallback, value


def normalise_signals(raw: dict, invoice: dict) -> dict:
    """Coerces Call 1's raw signal extraction into the signal contract, failing closed.

    Same philosophy as normalise_diagnosis(): every enum field is validated
    against its whitelist, and an unrecognised value degrades to a safe
    default rather than reaching Call 2 (or the audit trail) verbatim. Some
    fields have a data-derived fallback instead of a fixed one, because the
    invoice record already answers them unambiguously — `notes_empty` is
    computed from the invoice directly rather than trusted from the model at
    all, since it is a fact the model has no reason to ever get wrong, and
    Call 2's evidence-tier calibration leans on it being reliable.
    """
    invoice_id = invoice.get("invoice_id", "?")
    if not isinstance(raw, dict):
        raise ValueError(
            f"Signal extraction for {invoice_id} was {type(raw).__name__}, expected a JSON object"
        )

    engagement, engagement_raw = _pick(raw, "engagement_signal", ENGAGEMENT_SIGNALS, "no_engagement")
    history, history_raw = _pick(raw, "payment_history_pattern", PAYMENT_HISTORY_PATTERNS, "no_history")
    content_signal, content_signal_raw = _pick(raw, "content_signal", CONTENT_SIGNALS, "none")

    bucket_fallback = _bucket_days_overdue(invoice.get("days_overdue", 0))
    bucket, bucket_raw = _pick(raw, "days_overdue_bucket", DAYS_OVERDUE_BUCKETS, bucket_fallback)

    tier = invoice.get("account_value_tier")
    sensitivity_fallback = tier if tier in ACCOUNT_SENSITIVITY_LEVELS else "standard"
    sensitivity, sensitivity_raw = _pick(
        raw, "account_sensitivity", ACCOUNT_SENSITIVITY_LEVELS, sensitivity_fallback
    )

    link_status, link_status_raw = _pick(
        raw, "payment_link_status", PAYMENT_LINK_SIGNAL_STATUSES, "unknown"
    )

    history_trend, history_trend_raw = _pick(raw, "history_trend", HISTORY_TRENDS, "insufficient_data")
    lateness_consistency, lateness_consistency_raw = _pick(
        raw, "lateness_consistency", LATENESS_CONSISTENCY_LEVELS, "not_applicable"
    )
    specific_claim, specific_claim_raw = _pick(raw, "specific_claim", SPECIFIC_CLAIM_TYPES, "none")
    willingness, willingness_raw = _pick(
        raw, "willingness_to_pay", WILLINGNESS_TO_PAY_LEVELS, "none_expressed"
    )
    future_commitment, future_commitment_raw = _pick(
        raw, "future_commitment", FUTURE_COMMITMENT_TYPES, "none"
    )

    signals = {
        "engagement_signal": engagement,
        "payment_history_pattern": history,
        "content_signal": content_signal,
        "content_detail": str(raw.get("content_detail") or "")[:500],
        "days_overdue_bucket": bucket,
        "account_sensitivity": sensitivity,
        "payment_link_status": link_status,
        "evidence_available": bool(raw.get("evidence_available")),
        "history_trend": history_trend,
        "sudden_change_from_reliable": bool(raw.get("sudden_change_from_reliable")),
        "lateness_consistency": lateness_consistency,
        "specific_claim": specific_claim,
        "willingness_to_pay": willingness,
        "future_commitment": future_commitment,
        "technical_issue_mentioned": bool(raw.get("technical_issue_mentioned")),
        "notes_empty": not bool((invoice.get("client_notes") or "").strip()),
    }

    coerced = {
        f"{name}_raw": value
        for name, value in (
            ("engagement_signal", engagement_raw),
            ("payment_history_pattern", history_raw),
            ("content_signal", content_signal_raw),
            ("days_overdue_bucket", bucket_raw),
            ("account_sensitivity", sensitivity_raw),
            ("payment_link_status", link_status_raw),
            ("history_trend", history_trend_raw),
            ("lateness_consistency", lateness_consistency_raw),
            ("specific_claim", specific_claim_raw),
            ("willingness_to_pay", willingness_raw),
            ("future_commitment", future_commitment_raw),
        )
        if value is not None
    }
    if coerced:
        signals["coerced_fields"] = coerced
        log.warning(
            "[%s] signal extraction returned out-of-contract values, coerced to safe defaults: %s",
            invoice_id, coerced
        )

    return signals


def normalise_diagnosis(raw: dict, invoice_id: str) -> dict:
    """Coerces a raw model response into the diagnosis contract, failing closed.

    The model is free to return anything. Rather than letting an unexpected
    string reach the policy engine, every enum field is lowercased and checked
    against its whitelist; anything unrecognised degrades to the most cautious
    value (`unknown` / `gather_information`) and the original is preserved
    under `*_raw` so the audit trail shows what the model actually said.
    """
    if not isinstance(raw, dict):
        raise ValueError(
            f"Diagnosis for {invoice_id} was {type(raw).__name__}, expected a JSON object"
        )

    cause, cause_raw = _pick(raw, "diagnosed_cause", VALID_CAUSES, "unknown")
    tier, tier_raw = _pick(raw, "evidence_tier", VALID_EVIDENCE_TIERS, "unknown")
    action, action_raw = _pick(raw, "recommended_action", KNOWN_ACTIONS, "gather_information")

    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    diagnosis = {
        "invoice_id": invoice_id,
        "diagnosed_cause": cause,
        "evidence_tier": tier,
        "recommended_action": action,
        "evidence_cited": str(raw.get("evidence_cited") or "")[:1000],
        "reasoning": str(raw.get("reasoning") or "")[:2000],
        "suspicious_content_detected": bool(raw.get("suspicious_content_detected")),
        "confidence": confidence,
    }

    coerced = {
        f"{name}_raw": value
        for name, value in (
            ("diagnosed_cause", cause_raw),
            ("evidence_tier", tier_raw),
            ("recommended_action", action_raw),
        )
        if value is not None
    }
    if coerced:
        diagnosis["coerced_fields"] = coerced
        log.warning(
            "[%s] model returned out-of-contract values, coerced to safe defaults: %s",
            invoice_id, coerced
        )

    return diagnosis


def _extract_json(raw: str) -> dict:
    """Parses the model's message content, tolerating markdown fences."""
    clean = (raw or "").strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        if len(parts) > 1:
            clean = parts[1]
        if clean.lstrip().lower().startswith("json"):
            clean = clean.lstrip()[4:]
    return json.loads(clean.strip())


def _extract_json_block(text: str) -> str:
    """Finds the first brace-balanced `{...}` object in `text` and returns its source text.

    Used to locate the CONCLUSION JSON inside a ReAct response that also
    contains five Thought paragraphs, so JSON appearing incidentally inside a
    Thought's own prose (rare, but not impossible) doesn't get picked up
    early — brace counting keeps a nested `{` from closing the match short.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in output")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("unterminated JSON object in output")


_THOUGHT_HEADER_RE = re.compile(r"^[\s\-—]*[A-Za-z ]{0,40}:\s*\n?", re.UNICODE)


def _extract_thoughts(thoughts_text: str) -> dict:
    """Splits the reasoning-chain portion of a ReAct response into its 5 Thought paragraphs.

    Defensive by design: any Thought marker the model omitted just leaves
    that key as "" rather than raising, since the CONCLUSION JSON is the
    thing diagnose() cannot proceed without — the prose trace is best-effort.
    """
    markers = []
    for n in range(1, 6):
        match = re.search(rf"Thought\s*{n}\b", thoughts_text, re.IGNORECASE)
        markers.append((n, match.end() if match else None))

    trace = {}
    for idx, (n, end) in enumerate(markers):
        key = THOUGHT_KEYS[n - 1]
        if end is None:
            trace[key] = ""
            continue
        # Find where the next present Thought marker *starts*, so it isn't
        # included in this segment's content.
        segment_end = None
        for future_n, future_end in markers[idx + 1:]:
            if future_end is not None:
                future_match = re.search(rf"Thought\s*{future_n}\b", thoughts_text, re.IGNORECASE)
                segment_end = future_match.start()
                break
        segment = thoughts_text[end:segment_end] if segment_end is not None else thoughts_text[end:]
        trace[key] = _THOUGHT_HEADER_RE.sub("", segment.strip(), count=1).strip()

    return trace


def _split_conclusion(text: str) -> tuple:
    """Separates a ReAct response into (thoughts_text, json_source).

    Looks for the JSON block after "CONCLUSION:" first, since that's the
    documented format; if that marker is missing or the JSON isn't found
    after it, falls back to scanning the whole response for the last brace-
    balanced object (handles a model that emits only the Thoughts followed
    directly by JSON, or only the JSON with no Thoughts at all).
    """
    marker = re.search(r"CONCLUSION\s*:", text, re.IGNORECASE)
    if marker:
        thoughts_text = text[:marker.start()]
        remainder = text[marker.end():]
        try:
            return thoughts_text, _extract_json_block(remainder)
        except ValueError:
            pass
    return text, _extract_json_block(text)


def _parse_react_output(raw_output: str, invoice_id: str) -> tuple:
    """Splits Call 2's raw text into (reasoning_trace, diagnosis).

    `diagnosis` is already normalised (normalise_diagnosis()) and ready to
    merge with the reasoning trace and signals in diagnose(). Parsing is
    defensive about formatting but not about content: if no JSON object can
    be found at all, that is a real failure and raises ValueError with the
    raw output logged for debugging, exactly as an unparseable single-call
    diagnosis used to.
    """
    text = (raw_output or "").strip()
    if not text:
        raise ValueError(f"ReAct output for {invoice_id} was empty")

    try:
        thoughts_text, json_source = _split_conclusion(text)
        diagnosis_raw = json.loads(json_source)
    except (ValueError, json.JSONDecodeError) as e:
        log.error(
            "[%s] could not parse ReAct output: %s\n--- raw output ---\n%s\n--- end raw output ---",
            invoice_id, e, text[:4000]
        )
        raise ValueError(f"ReAct output for {invoice_id} was not parseable: {e}") from e

    reasoning_trace = _extract_thoughts(thoughts_text)
    diagnosis = normalise_diagnosis(diagnosis_raw, invoice_id)
    return reasoning_trace, diagnosis


def _call_nvidia(
    client, invoice_id: str, system_prompt: str, user_message: str, *,
    max_tokens: int, json_mode: bool, call_label: str, parse_fn=None
):
    """Shared retry loop for both ReAct calls.

    Retries on rate limits, network errors, 5xx, truncation, and (when
    `parse_fn` is given) an unparseable response — a malformed response is a
    transient model failure, not a permanent one. Returns `parse_fn(content)`
    if given, otherwise the raw text content.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            kwargs = dict(
                model=NVIDIA_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.1,
                max_tokens=max_tokens,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            response = client.chat.completions.create(**kwargs)
        except openai.RateLimitError as e:
            last_error = f"rate limited: {e}"
            delay = _backoff_delay(attempt, _retry_after_seconds(e))
            log.warning("[%s] %s rate limited (attempt %d/%d) — retrying in %.1fs",
                        invoice_id, call_label, attempt, MAX_RETRIES, delay)
            time.sleep(delay)
            continue
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            last_error = f"network error: {e}"
            delay = _backoff_delay(attempt)
            log.warning("[%s] %s request failed (attempt %d/%d): %s — retrying in %.1fs",
                        invoice_id, call_label, attempt, MAX_RETRIES, last_error, delay)
            time.sleep(delay)
            continue
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                last_error = f"{e.status_code} server error: {e.message}"
                delay = _backoff_delay(attempt, _retry_after_seconds(e))
                log.warning("[%s] %s server error (attempt %d/%d): %s — retrying in %.1fs",
                            invoice_id, call_label, attempt, MAX_RETRIES, last_error, delay)
                time.sleep(delay)
                continue
            raise ValueError(
                f"NVIDIA NIM API error {e.status_code} for {invoice_id} ({call_label}): {e.message}"
            ) from e

        choice = response.choices[0]
        content = choice.message.content

        if choice.finish_reason == "length":
            last_error = f"response truncated at max_tokens: {str(content)[:200]}"
            log.warning("[%s] %s truncated response (attempt %d/%d)",
                        invoice_id, call_label, attempt, MAX_RETRIES)
            continue

        if not content or not content.strip():
            last_error = "empty response"
            log.warning("[%s] %s empty response (attempt %d/%d)",
                        invoice_id, call_label, attempt, MAX_RETRIES)
            continue

        if parse_fn is None:
            return content

        try:
            return parse_fn(content)
        except (json.JSONDecodeError, ValueError) as e:
            last_error = f"unparseable response: {e} — raw: {str(content)[:300]}"
            log.warning("[%s] %s unparseable response (attempt %d/%d): %s",
                        invoice_id, call_label, attempt, MAX_RETRIES, e)
            continue

    raise ValueError(
        f"NVIDIA NIM {call_label} failed for {invoice_id} after {MAX_RETRIES} attempts: {last_error}"
    )


def _extract_signals(invoice: dict, api_key: str) -> dict:
    """Call 1: reads the invoice and extracts the 8 observable signals, without interpreting them."""
    if not api_key:
        raise ValueError(
            f"_extract_signals() called without an API key for {invoice.get('invoice_id', '?')} — "
            "set NVIDIA_API_KEY in .env"
        )
    invoice_id = invoice.get("invoice_id", "?")
    user_message = SIGNAL_EXTRACTION_USER_TEMPLATE.format(**_invoice_context(invoice))
    # max_retries=0: this function already implements its own controlled
    # backoff loop in _call_nvidia — the SDK's own internal retries would
    # compound with it unpredictably.
    client = openai.OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key, max_retries=0)

    raw = _call_nvidia(
        client, invoice_id,
        system_prompt=SIGNAL_EXTRACTION_SYSTEM_PROMPT,
        user_message=user_message,
        max_tokens=700,
        json_mode=True,
        call_label="signal extraction",
        parse_fn=_extract_json,
    )
    return normalise_signals(raw, invoice)


def _reason_with_react(invoice: dict, signals: dict, api_key: str) -> str:
    """Call 2: given the signals from Call 1, produces the Thought-1..5 chain plus CONCLUSION JSON."""
    if not api_key:
        raise ValueError(
            f"_reason_with_react() called without an API key for {invoice.get('invoice_id', '?')} — "
            "set NVIDIA_API_KEY in .env"
        )
    invoice_id = invoice.get("invoice_id", "?")
    user_message = REACT_USER_TEMPLATE.format(
        **_invoice_context(invoice),
        signals_json=json.dumps(signals, indent=2),
    )
    client = openai.OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key, max_retries=0)

    # Not json_mode: the expected output is the five Thought paragraphs
    # followed by a JSON object, not a single JSON document.
    return _call_nvidia(
        client, invoice_id,
        system_prompt=REACT_SYSTEM_PROMPT,
        user_message=user_message,
        max_tokens=1500,
        json_mode=False,
        call_label="ReAct reasoning",
    )


def diagnose(invoice: dict, api_key: str = None) -> dict:
    """Diagnoses why an invoice is unpaid using two sequential LLM calls (ReAct).

    Call 1 extracts observable signals without interpreting them. Call 2
    reasons through those signals in a visible Thought-1..5 chain before
    committing to a diagnosis, making the model's reasoning explicit in the
    audit trail rather than hidden inside a single forward pass.

    Returns a normalised diagnosis dict with two extra keys for audit
    logging — `_reasoning_trace` and `_signals` — which api/pipeline.py must
    strip before the diagnosis reaches the policy engine.
    """
    invoice_id = invoice.get("invoice_id", "?")

    signals = _extract_signals(invoice, api_key)

    # Stagger the two calls so a batch of concurrently-diagnosed invoices
    # doesn't burst both calls for every invoice at the same instant.
    time.sleep(INTRA_DIAGNOSIS_DELAY_SECONDS)

    react_output = _reason_with_react(invoice, signals, api_key)
    reasoning_trace, diagnosis = _parse_react_output(react_output, invoice_id)

    diagnosis["_reasoning_trace"] = reasoning_trace
    diagnosis["_signals"] = signals

    return diagnosis


# ─────────────────────────────────────────────────────────────────────────
# Async diagnosis
#
# The same two-call ReAct sequence as diagnose(), driven by AsyncOpenAI so a
# batch can have many invoices in flight at once. Everything that decides
# *what* the diagnosis says — the prompts, the retry policy,
# normalise_signals(), normalise_diagnosis(), _parse_react_output() — is
# shared with the synchronous path, so the two cannot drift apart in
# behaviour.
#
# The semaphore below is the rate limit. NVIDIA NIM's free tier will not take
# fifty-two simultaneous requests; a small number in flight is what the batch
# is allowed to hold. It is enforced around the HTTP call itself rather than
# around a whole diagnosis, so a request never occupies a slot while it is
# only sleeping between retries.
#
# Four rather than six: at six, a 52-invoice batch (104 requests) reliably
# tripped the free tier's limit, and because every task then backed off by
# the same un-jittered interval they retried in lockstep and burned all
# their attempts inside a few seconds. Four in flight, full-jitter backoff,
# and the cooldown gate below are three independent parts of the same fix —
# lowering concurrency alone only makes the collision less likely, it does
# not stop a herd that has already synchronised.
# ─────────────────────────────────────────────────────────────────────────

MAX_CONCURRENT_LLM_CALLS = _env_int("NVIDIA_MAX_CONCURRENCY", 4)

# Concurrency alone is not a rate limit. NVIDIA NIM's free tier meters
# requests per minute, so six *concurrent* requests still becomes several
# hundred requests per minute when each one returns in under a second — which
# is how a 52-invoice batch (104 calls) reliably walked into HTTP 429 and
# finished with every invoice undiagnosed.
#
_llm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)
_llm_semaphore_loop = None

# ── Shared cooldown gate + adaptive pacer ────────────────────────────────
# A 429 is information about the *shared* upstream window, not about the one
# invoice that happened to receive it. Backing off only that task leaves the
# other in-flight tasks hammering an API that has just said stop, so each of
# them collects its own 429 and the batch spends its retry budget learning
# the same fact N times over.
#
# So a 429 anywhere opens a cooldown that *every* task checks before its next
# request. The gate is advisory and monotonic-clock based: it never blocks a
# request that is already in flight, it only delays the next one, and a later
# 429 can extend the window but never shorten it.
#
# The cooldown alone is still not enough, and the reason is worth stating
# because it is the mistake this file made twice. A semaphore caps how many
# requests are *in flight*; a rate limit counts how many *start* per unit
# time. Those are different quantities, and they only coincide when latency
# is constant. Four concurrent slots against a fast endpoint issues requests
# far quicker than four-per-second, so the batch can sit under its
# concurrency cap and still be well over the rate limit — which is exactly
# what happened: fourteen invoices exhausted a six-attempt retry budget on
# 429s while never exceeding four in flight.
#
# The pacer below therefore limits the thing the server actually measures:
# the interval between request starts. It widens multiplicatively on a 429
# and decays back geometrically on sustained success, so a batch finds the
# endpoint's real capacity instead of assuming one.
_cooldown_until = 0.0
_pacer_lock = None
_pacer_lock_loop = None
_next_slot_at = 0.0

MIN_REQUEST_INTERVAL_SECONDS = _env_float("NVIDIA_MIN_REQUEST_INTERVAL", 0.4)
MAX_REQUEST_INTERVAL_SECONDS = _env_float("NVIDIA_MAX_REQUEST_INTERVAL", 4.0)
_request_interval = MIN_REQUEST_INTERVAL_SECONDS

# Multiplicative increase on a 429, geometric decay on success — the same
# shape as TCP congestion control, and for the same reason: overshooting
# costs a retry budget, undershooting costs only a little throughput.
#
# Recovery is deliberately brisk. A first pass tried 0.93 every 5 successes
# and the interval never came back down: the opening burst pushed it to the
# 6s ceiling, decay could not outrun the occasional 429, and a 52-invoice
# batch that should take ~2 minutes took over 7. Decaying 15% every 3 clean
# responses lets the batch settle at the endpoint's actual capacity instead
# of parking at the ceiling, while still yielding immediately when pushed back.
_PACE_BACKOFF_FACTOR = 1.8
_PACE_RECOVERY_FACTOR = 0.85
_consecutive_successes = 0
_SUCCESSES_BEFORE_SPEEDUP = 3


def _get_pacer_lock() -> asyncio.Lock:
    """Returns the pacer's lock, rebuilt if the running loop has changed.

    Same loop-affinity problem as the semaphore: an asyncio primitive binds to
    the first loop that awaits it.
    """
    global _pacer_lock, _pacer_lock_loop
    loop = asyncio.get_running_loop()
    if _pacer_lock_loop is not loop:
        _pacer_lock = asyncio.Lock()
        _pacer_lock_loop = loop
    return _pacer_lock


def _open_cooldown(seconds: float) -> None:
    """Extends the shared cooldown window to at least `seconds` from now."""
    global _cooldown_until
    _cooldown_until = max(_cooldown_until, time.monotonic() + seconds)


def note_rate_limited() -> None:
    """Widens the request interval after a 429. Called by every retry path."""
    global _request_interval, _consecutive_successes
    _consecutive_successes = 0
    _request_interval = min(
        _request_interval * _PACE_BACKOFF_FACTOR, MAX_REQUEST_INTERVAL_SECONDS
    )
    log.debug("pacer: widened request interval to %.2fs", _request_interval)


def note_request_succeeded() -> None:
    """Narrows the request interval after a run of clean responses."""
    global _request_interval, _consecutive_successes
    _consecutive_successes += 1
    if _consecutive_successes >= _SUCCESSES_BEFORE_SPEEDUP:
        _consecutive_successes = 0
        _request_interval = max(
            _request_interval * _PACE_RECOVERY_FACTOR, MIN_REQUEST_INTERVAL_SECONDS
        )


async def _acquire_pace_slot() -> None:
    """Blocks until this caller may start its request.

    Hands out start times at least `_request_interval` apart, globally across
    the batch. The lock is held only while claiming a slot, never while
    sleeping on it, so claiming is O(1) and concurrent callers queue up
    behind distinct future slots rather than all waking on the same one.
    """
    global _next_slot_at
    async with _get_pacer_lock():
        now = time.monotonic()
        slot = max(now, _next_slot_at)
        _next_slot_at = slot + _request_interval
    wait = slot - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)


async def _await_cooldown() -> None:
    """Waits out any open cooldown window, then claims a pacing slot.

    The cooldown is re-checked in a loop because the window can be extended by
    another task's 429 while this one is already waiting on it.
    """
    while True:
        remaining = _cooldown_until - time.monotonic()
        if remaining <= 0:
            break
        # A small extra jitter on top so tasks released by the same window do
        # not all fire on the same tick.
        await asyncio.sleep(remaining + random.uniform(0.0, 0.35))
    await _acquire_pace_slot()


def reset_rate_limit_state() -> None:
    """Resets the cooldown window and the pacer. For tests and between batches."""
    global _cooldown_until, _next_slot_at, _request_interval, _consecutive_successes
    _cooldown_until = 0.0
    _next_slot_at = 0.0
    _request_interval = MIN_REQUEST_INTERVAL_SECONDS
    _consecutive_successes = 0


# One AsyncOpenAI per (event loop, API key). The client owns an httpx
# connection pool belonging to the loop that created it, so it is cached per
# loop rather than globally — and reused across the batch's ~104 requests
# rather than rebuilt per invoice, which would mean a fresh TLS handshake for
# every call.
_async_clients = {}


def get_llm_semaphore() -> asyncio.Semaphore:
    """Returns the process-wide cap on in-flight NVIDIA NIM requests.

    Rebuilt if the running loop has changed since it was created: an asyncio
    primitive binds to the first loop that awaits it and raises on a second
    one, which would otherwise break any process that runs two batches.
    """
    global _llm_semaphore, _llm_semaphore_loop

    loop = asyncio.get_running_loop()
    if _llm_semaphore_loop is not loop:
        _llm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)
        _llm_semaphore_loop = loop
    return _llm_semaphore


def _get_async_client(api_key: str):
    """Returns the cached AsyncOpenAI client for this loop and key.

    max_retries=0 for the same reason as the synchronous client: the backoff
    loop in _call_nvidia_async() is the only retry policy, and the SDK's own
    would compound with it unpredictably.
    """
    key = (asyncio.get_running_loop(), api_key)
    client = _async_clients.get(key)
    if client is None:
        client = openai.AsyncOpenAI(
            base_url=NVIDIA_BASE_URL, api_key=api_key, max_retries=0
        )
        _async_clients[key] = client
    return client


async def close_async_clients() -> None:
    """Closes every cached AsyncOpenAI client. Call before the loop ends."""
    clients = list(_async_clients.values())
    _async_clients.clear()
    for client in clients:
        await client.close()


async def _call_nvidia_async(
    client, invoice_id: str, system_prompt: str, user_message: str, *,
    max_tokens: int, json_mode: bool, call_label: str, parse_fn=None
):
    """Async twin of _call_nvidia(). Same retry policy, same failure modes.

    Only the waiting differs: `await asyncio.sleep()` instead of
    `time.sleep()`, so an invoice that is backing off yields the event loop to
    the others instead of freezing the whole batch.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            kwargs = dict(
                model=NVIDIA_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.1,
                max_tokens=max_tokens,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            # Respect any cooldown another task's 429 opened before spending a
            # semaphore slot on a request the API has just said it won't take.
            await _await_cooldown()
            # The semaphore is held for the request only. Parsing, validation
            # and backoff all happen outside it, so a slot is never occupied
            # by an invoice that is not actually talking to the API.
            async with get_llm_semaphore():
                response = await client.chat.completions.create(**kwargs)
        except openai.RateLimitError as e:
            last_error = f"rate limited: {e}"
            retry_after = _retry_after_seconds(e)
            delay = _backoff_delay(attempt, retry_after)
            # Tell every other task about the shared window, not just this one,
            # and slow the whole batch's request rate rather than only this task's.
            note_rate_limited()
            _open_cooldown(retry_after if retry_after > 0 else min(delay, 5.0))
            log.warning("[%s] %s rate limited (attempt %d/%d) — retrying in %.1fs",
                        invoice_id, call_label, attempt, MAX_RETRIES, delay)
            await asyncio.sleep(delay)
            continue
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            last_error = f"network error: {e}"
            delay = _backoff_delay(attempt)
            log.warning("[%s] %s request failed (attempt %d/%d): %s — retrying in %.1fs",
                        invoice_id, call_label, attempt, MAX_RETRIES, last_error, delay)
            await asyncio.sleep(delay)
            continue
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                last_error = f"{e.status_code} server error: {e.message}"
                retry_after = _retry_after_seconds(e)
                delay = _backoff_delay(attempt, retry_after)
                # A 5xx from a shared endpoint is also shared information.
                note_rate_limited()
                _open_cooldown(retry_after if retry_after > 0 else min(delay, 5.0))
                log.warning("[%s] %s server error (attempt %d/%d): %s — retrying in %.1fs",
                            invoice_id, call_label, attempt, MAX_RETRIES, last_error, delay)
                await asyncio.sleep(delay)
                continue
            raise ValueError(
                f"NVIDIA NIM API error {e.status_code} for {invoice_id} ({call_label}): {e.message}"
            ) from e

        # The endpoint answered. Let the pacer creep back toward full speed —
        # a truncated or unparseable body below is a model problem, not a
        # capacity one, so the signal belongs here rather than after parsing.
        note_request_succeeded()

        choice = response.choices[0]
        content = choice.message.content

        if choice.finish_reason == "length":
            last_error = f"response truncated at max_tokens: {str(content)[:200]}"
            log.warning("[%s] %s truncated response (attempt %d/%d)",
                        invoice_id, call_label, attempt, MAX_RETRIES)
            continue

        if not content or not content.strip():
            last_error = "empty response"
            log.warning("[%s] %s empty response (attempt %d/%d)",
                        invoice_id, call_label, attempt, MAX_RETRIES)
            continue

        if parse_fn is None:
            return content

        try:
            return parse_fn(content)
        except (json.JSONDecodeError, ValueError) as e:
            last_error = f"unparseable response: {e} — raw: {str(content)[:300]}"
            log.warning("[%s] %s unparseable response (attempt %d/%d): %s",
                        invoice_id, call_label, attempt, MAX_RETRIES, e)
            continue

    raise ValueError(
        f"NVIDIA NIM {call_label} failed for {invoice_id} after {MAX_RETRIES} attempts: {last_error}"
    )


async def _extract_signals_async(invoice: dict, api_key: str) -> dict:
    """Async twin of _extract_signals(). Call 1 of the ReAct sequence."""
    if not api_key:
        raise ValueError(
            f"_extract_signals_async() called without an API key for "
            f"{invoice.get('invoice_id', '?')} — set NVIDIA_API_KEY in .env"
        )
    invoice_id = invoice.get("invoice_id", "?")
    user_message = SIGNAL_EXTRACTION_USER_TEMPLATE.format(**_invoice_context(invoice))
    client = _get_async_client(api_key)

    raw = await _call_nvidia_async(
        client, invoice_id,
        system_prompt=SIGNAL_EXTRACTION_SYSTEM_PROMPT,
        user_message=user_message,
        max_tokens=700,
        json_mode=True,
        call_label="signal extraction",
        parse_fn=_extract_json,
    )
    return normalise_signals(raw, invoice)


async def _reason_with_react_async(invoice: dict, signals: dict, api_key: str) -> str:
    """Async twin of _reason_with_react(). Call 2 of the ReAct sequence."""
    if not api_key:
        raise ValueError(
            f"_reason_with_react_async() called without an API key for "
            f"{invoice.get('invoice_id', '?')} — set NVIDIA_API_KEY in .env"
        )
    invoice_id = invoice.get("invoice_id", "?")
    user_message = REACT_USER_TEMPLATE.format(
        **_invoice_context(invoice),
        signals_json=json.dumps(signals, indent=2),
    )
    client = _get_async_client(api_key)

    # Not json_mode, for the same reason as the synchronous call: the expected
    # output is five Thought paragraphs followed by a JSON object.
    return await _call_nvidia_async(
        client, invoice_id,
        system_prompt=REACT_SYSTEM_PROMPT,
        user_message=user_message,
        max_tokens=1500,
        json_mode=False,
        call_label="ReAct reasoning",
    )


async def diagnose_async(invoice: dict, api_key: str = None) -> dict:
    """Async twin of diagnose(). Same two calls, same contract, same output.

    Returns the identical normalised dict, `_reasoning_trace` and `_signals`
    included, so the async pipeline in api/main.py strips exactly what the
    synchronous one strips.
    """
    invoice_id = invoice.get("invoice_id", "?")

    signals = await _extract_signals_async(invoice, api_key)

    # The same stagger the synchronous path applies, awaited rather than
    # slept: it spaces this invoice's two calls apart without holding the
    # event loop, which is the whole point of the async path.
    await asyncio.sleep(INTRA_DIAGNOSIS_DELAY_SECONDS)

    react_output = await _reason_with_react_async(invoice, signals, api_key)
    reasoning_trace, diagnosis = _parse_react_output(react_output, invoice_id)

    diagnosis["_reasoning_trace"] = reasoning_trace
    diagnosis["_signals"] = signals

    return diagnosis

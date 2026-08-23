import json
import logging
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

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# meta/llama-3.3-70b-instruct was consistently timing out on NVIDIA's free
# tier at the time of integration; meta/llama-3.1-8b-instruct responded
# reliably and fast in repeated live checks.
NVIDIA_MODEL = "meta/llama-3.1-8b-instruct"
MAX_RETRIES = 5

# Stagger between the two ReAct calls for a single invoice, so a burst of
# invoices being diagnosed concurrently doesn't double the request rate at
# the exact same instant. The per-invoice concurrency limit lives with the
# batch runner; this only spaces out the two calls within one diagnosis.
INTRA_DIAGNOSIS_DELAY_SECONDS = 0.5

VALID_CAUSES = {
    "dispute", "payment_link_failure", "chronic_late_payer", "forgotten_invoice",
    "wrong_contact", "cash_flow_delay", "promise_broken", "deliberate_stall",
    "legal_threat", "duplicate_invoice", "unknown",
}

# Valid values for the 8 signals extracted by Call 1. Kept in code as well as
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
    """Coerces Call 1's raw signal extraction into the 8-field contract, failing closed.

    Same philosophy as normalise_diagnosis(): every enum field is validated
    against its whitelist, and an unrecognised value degrades to a safe
    default rather than reaching Call 2 (or the audit trail) verbatim. Two
    fields have a data-derived fallback instead of a fixed one, because the
    invoice record already answers them unambiguously.
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

    signals = {
        "engagement_signal": engagement,
        "payment_history_pattern": history,
        "content_signal": content_signal,
        "content_detail": str(raw.get("content_detail") or "")[:500],
        "days_overdue_bucket": bucket,
        "account_sensitivity": sensitivity,
        "payment_link_status": link_status,
        "evidence_available": bool(raw.get("evidence_available")),
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
                timeout=30.0,
            )
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            response = client.chat.completions.create(**kwargs)
        except openai.RateLimitError as e:
            last_error = f"rate limited: {e}"
            log.warning("[%s] %s rate limited (attempt %d/%d)",
                        invoice_id, call_label, attempt, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            last_error = f"network error: {e}"
            log.warning("[%s] %s request failed (attempt %d/%d): %s",
                        invoice_id, call_label, attempt, MAX_RETRIES, last_error)
            time.sleep(2 ** attempt)
            continue
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                last_error = f"{e.status_code} server error: {e.message}"
                log.warning("[%s] %s server error (attempt %d/%d): %s",
                            invoice_id, call_label, attempt, MAX_RETRIES, last_error)
                time.sleep(2 ** attempt)
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
        max_tokens=500,
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
    logging — `_reasoning_trace` and `_signals` — which api.main.py must
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

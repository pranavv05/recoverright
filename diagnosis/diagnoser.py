import json
import logging
import time

import openai

from diagnosis.prompts import DIAGNOSIS_SYSTEM_PROMPT, DIAGNOSIS_USER_TEMPLATE
from policy.rules import KNOWN_ACTIONS, VALID_EVIDENCE_TIERS

log = logging.getLogger(__name__)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# meta/llama-3.3-70b-instruct was consistently timing out on NVIDIA's free
# tier at the time of integration; meta/llama-3.1-8b-instruct responded
# reliably and fast in repeated live checks.
NVIDIA_MODEL = "meta/llama-3.1-8b-instruct"
MAX_RETRIES = 5

VALID_CAUSES = {
    "dispute", "payment_link_failure", "chronic_late_payer", "forgotten_invoice",
    "wrong_contact", "cash_flow_delay", "promise_broken", "deliberate_stall",
    "legal_threat", "duplicate_invoice", "unknown",
}

# Marks the boundary of untrusted merchant/client text inside the prompt.
NOTES_FENCE = "<<<CLIENT_NOTES>>>"


def build_user_message(invoice: dict) -> str:
    """Formats one invoice into the NVIDIA NIM diagnosis user prompt.

    client_notes is untrusted third-party text. It is stripped of the fence
    marker and wrapped in it, so a note cannot close the field early and forge
    the surrounding structure.
    """
    outstanding = (
        invoice["amount_paise"] - invoice["partial_payment_received_paise"]
    ) // 100

    notes = (invoice.get("client_notes") or "").replace(NOTES_FENCE, "")

    return DIAGNOSIS_USER_TEMPLATE.format(
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

    def pick(field: str, valid: set, fallback: str) -> tuple:
        value = raw.get(field)
        cleaned = value.strip().lower() if isinstance(value, str) else None
        if cleaned in valid:
            return cleaned, None
        return fallback, value

    cause, cause_raw = pick("diagnosed_cause", VALID_CAUSES, "unknown")
    tier, tier_raw = pick("evidence_tier", VALID_EVIDENCE_TIERS, "unknown")
    action, action_raw = pick("recommended_action", KNOWN_ACTIONS, "gather_information")

    diagnosis = {
        "invoice_id": invoice_id,
        "diagnosed_cause": cause,
        "evidence_tier": tier,
        "recommended_action": action,
        "evidence_cited": str(raw.get("evidence_cited") or "")[:1000],
        "reasoning": str(raw.get("reasoning") or "")[:2000],
        "suspicious_content_detected": bool(raw.get("suspicious_content_detected")),
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


def diagnose(invoice: dict, api_key: str) -> dict:
    """Calls NVIDIA NIM to diagnose why an invoice is unpaid, then validates the result.

    Retries on rate limits, network errors, 5xx, truncation, and unparseable
    JSON — a malformed response is a transient model failure, not a permanent
    one, and burning the invoice on the first bad token was a real bug.

    Returns a normalised diagnosis dict. Performs no database writes.
    """
    if not api_key:
        raise ValueError(
            f"diagnose() called without an API key for {invoice.get('invoice_id', '?')} — "
            "set NVIDIA_API_KEY in .env"
        )

    invoice_id = invoice.get("invoice_id", "?")
    user_message = build_user_message(invoice)
    # max_retries=0: this function already implements its own controlled
    # backoff loop below — the SDK's own internal retries would compound
    # with it unpredictably.
    client = openai.OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key, max_retries=0)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=NVIDIA_MODEL,
                messages=[
                    {"role": "system", "content": DIAGNOSIS_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.1,
                max_tokens=1000,
                response_format={"type": "json_object"},
                timeout=30.0,
            )
        except openai.RateLimitError as e:
            last_error = f"rate limited: {e}"
            log.warning("[%s] rate limited (attempt %d/%d)", invoice_id, attempt, MAX_RETRIES)
            time.sleep(2 ** attempt)
            continue
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            last_error = f"network error: {e}"
            log.warning("[%s] request failed (attempt %d/%d): %s",
                        invoice_id, attempt, MAX_RETRIES, last_error)
            time.sleep(2 ** attempt)
            continue
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                last_error = f"{e.status_code} server error: {e.message}"
                log.warning("[%s] server error (attempt %d/%d): %s",
                            invoice_id, attempt, MAX_RETRIES, last_error)
                time.sleep(2 ** attempt)
                continue
            raise ValueError(
                f"NVIDIA NIM API error {e.status_code} for {invoice_id}: {e.message}"
            ) from e

        choice = response.choices[0]
        content = choice.message.content

        if choice.finish_reason == "length":
            last_error = f"response truncated at max_tokens: {str(content)[:200]}"
            log.warning("[%s] truncated response (attempt %d/%d)",
                        invoice_id, attempt, MAX_RETRIES)
            continue

        try:
            parsed = _extract_json(content)
        except json.JSONDecodeError as e:
            last_error = f"unparseable JSON: {e} — raw: {str(content)[:300]}"
            log.warning("[%s] unparseable JSON (attempt %d/%d): %s",
                        invoice_id, attempt, MAX_RETRIES, e)
            continue

        return normalise_diagnosis(parsed, invoice_id)

    raise ValueError(
        f"NVIDIA NIM diagnosis failed for {invoice_id} after {MAX_RETRIES} attempts: {last_error}"
    )

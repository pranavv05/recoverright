import json
import time

import openai
from diagnosis.prompts import DIAGNOSIS_SYSTEM_PROMPT, DIAGNOSIS_USER_TEMPLATE

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# meta/llama-3.3-70b-instruct was consistently timing out on NVIDIA's free
# tier at the time of integration; meta/llama-3.1-8b-instruct responded
# reliably and fast in repeated live checks.
NVIDIA_MODEL = "meta/llama-3.1-8b-instruct"
MAX_RETRIES = 5

def build_user_message(invoice: dict) -> str:
    """Formats one invoice into the NVIDIA NIM diagnosis user prompt."""
    outstanding = (
        invoice["amount_paise"] - invoice["partial_payment_received_paise"]
    ) // 100

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
        client_notes=invoice["client_notes"] or "(none)"
    )

def diagnose(invoice: dict, api_key: str) -> dict:
    """Calls NVIDIA NIM to diagnose why an invoice is unpaid; retries on rate limits and 5xx errors.

    Returns the parsed diagnosis JSON only — performs no database writes.
    """
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
                    {"role": "user", "content": user_message}
                ],
                temperature=0.1,
                max_tokens=1000,
                response_format={"type": "json_object"},
                timeout=30.0
            )
        except openai.RateLimitError as e:
            last_error = f"rate limited: {e}"
            wait = 2 ** attempt
            print(f"  [{invoice_id}] NVIDIA NIM rate limited (attempt {attempt}/{MAX_RETRIES}), retrying in {wait}s")
            time.sleep(wait)
            continue
        except openai.APIConnectionError as e:
            last_error = f"network error: {e}"
            print(f"  [{invoice_id}] NVIDIA NIM request failed (attempt {attempt}/{MAX_RETRIES}): {last_error}")
            time.sleep(2 ** attempt)
            continue
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                last_error = f"{e.status_code} server error: {e.message}"
                print(f"  [{invoice_id}] NVIDIA NIM server error (attempt {attempt}/{MAX_RETRIES}): {last_error}")
                time.sleep(2 ** attempt)
                continue
            raise ValueError(
                f"NVIDIA NIM API error {e.status_code} for {invoice_id}: {e.message}"
            ) from e

        choice = response.choices[0]
        raw = choice.message.content

        if choice.finish_reason == "length":
            raise ValueError(
                f"NVIDIA NIM response for {invoice_id} was truncated (hit max_tokens) — raw content: {raw[:500]}"
            )

        # Strip markdown fences if present
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]

        try:
            return json.loads(clean.strip())
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Failed to parse diagnosis JSON for {invoice_id}: {e} — raw content: {raw[:500]}"
            )

    raise ValueError(f"NVIDIA NIM API failed for {invoice_id} after {MAX_RETRIES} attempts: {last_error}")

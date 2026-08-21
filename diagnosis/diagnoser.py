import json
import time
import httpx
from diagnosis.prompts import DIAGNOSIS_SYSTEM_PROMPT, DIAGNOSIS_USER_TEMPLATE

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"
MAX_RETRIES = 5

def build_user_message(invoice: dict) -> str:
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
    invoice_id = invoice.get("invoice_id", "?")
    user_message = build_user_message(invoice)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = httpx.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": DIAGNOSIS_SYSTEM_PROMPT},
                        {"role": "user", "content": user_message}
                    ],
                    "temperature": 0.1,
                    "max_tokens": 1000
                },
                timeout=30.0
            )
        except httpx.RequestError as e:
            last_error = f"network error: {e}"
            print(f"  [{invoice_id}] Groq request failed (attempt {attempt}/{MAX_RETRIES}): {last_error}")
            time.sleep(2 ** attempt)
            continue

        # Rate limited — back off and retry, honoring Retry-After if present
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            wait = float(retry_after) if retry_after else 2 ** attempt
            last_error = f"429 rate limited: {response.text[:300]}"
            print(f"  [{invoice_id}] Groq rate limited (attempt {attempt}/{MAX_RETRIES}), retrying in {wait}s")
            time.sleep(wait)
            continue

        # Transient server errors — retry with exponential backoff
        if response.status_code >= 500:
            last_error = f"{response.status_code} server error: {response.text[:300]}"
            print(f"  [{invoice_id}] Groq server error (attempt {attempt}/{MAX_RETRIES}): {last_error}")
            time.sleep(2 ** attempt)
            continue

        if response.status_code != 200:
            raise ValueError(
                f"Groq API error {response.status_code} for {invoice_id}: {response.text[:500]}"
            )

        try:
            response_json = response.json()
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Groq response was not valid JSON for {invoice_id}: {e} — body: {response.text[:500]}"
            )

        if "choices" not in response_json:
            raise ValueError(f"Groq API error for {invoice_id}: {response_json}")

        choice = response_json["choices"][0]
        raw = choice["message"]["content"]

        if choice.get("finish_reason") == "length":
            raise ValueError(
                f"Groq response for {invoice_id} was truncated (hit max_tokens) — raw content: {raw[:500]}"
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

    raise ValueError(f"Groq API failed for {invoice_id} after {MAX_RETRIES} attempts: {last_error}")
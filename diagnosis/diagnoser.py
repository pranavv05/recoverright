import json
import httpx
from diagnosis.prompts import DIAGNOSIS_SYSTEM_PROMPT, DIAGNOSIS_USER_TEMPLATE

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

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
    user_message = build_user_message(invoice)

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
            "max_tokens": 500
        },
        timeout=30.0
    )

    raw = response.json()["choices"][0]["message"]["content"]

    # Strip markdown fences if present
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]

    return json.loads(clean.strip())
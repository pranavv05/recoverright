import base64
import json

import httpx


class RateLimitError(Exception):
    """Raised when Razorpay responds with 429 (rate limited)."""


class ServerError(Exception):
    """Raised when Razorpay responds with a 5xx server error."""


class RazorpayClient:
    """Thin HTTP wrapper around the Razorpay test-mode Payment Links API."""

    BASE_URL = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str):
        credentials = base64.b64encode(
            f"{key_id}:{key_secret}".encode()
        ).decode()
        self.headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json"
        }

    def create_payment_link(
        self,
        invoice_id: str,
        amount_paise: int,
        client_name: str,
        description: str
    ) -> dict:
        """Creates an idempotent Razorpay payment link for the outstanding balance."""
        idempotency_key = f"{invoice_id}-payment-link"

        try:
            response = httpx.post(
                f"{self.BASE_URL}/payment_links",
                headers={
                    **self.headers,
                    "X-Idempotency-Key": idempotency_key
                },
                json={
                    "amount": amount_paise,
                    "currency": "INR",
                    "description": description,
                    "reference_id": invoice_id,
                    "customer": {"name": client_name},
                    "notify": {"sms": False, "email": False}
                },
                timeout=15.0
            )
        except httpx.RequestError as e:
            raise ServerError(f"Razorpay request failed for {invoice_id}: {e}") from e

        if response.status_code == 429:
            raise RateLimitError("Razorpay rate limit hit")
        if response.status_code >= 500:
            raise ServerError(f"Razorpay server error: {response.status_code}")
        if response.status_code >= 400:
            raise ValueError(
                f"Razorpay API error {response.status_code} for {invoice_id}: {response.text[:500]}"
            )

        try:
            return response.json()
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Razorpay response was not valid JSON for {invoice_id}: {e}"
            ) from e
import base64
import json

import httpx


class RateLimitError(Exception):
    """Raised when Razorpay responds with 429 (rate limited)."""


class ServerError(Exception):
    """Raised when Razorpay responds with a 5xx server error."""


class DuplicateReferenceError(Exception):
    """Raised when a payment link already exists for this invoice's reference_id.

    Not an error condition in the usual sense — it is Razorpay's duplicate
    protection working. The caller should fetch and reuse the existing link.
    """


class RazorpayClient:
    """Thin HTTP wrapper around the Razorpay test-mode Payment Links API.

    Duplicate protection note: the Payment Links API has no idempotency-key
    header. An earlier revision sent `X-Idempotency-Key`, which Razorpay
    ignores as an unknown header — it looked like protection and provided
    none. The real mechanism is `reference_id`, which Razorpay enforces as
    unique per account: a second create with the same reference_id is
    rejected with a 400 rather than producing a second link. That is what
    `create_payment_link` relies on, and `DuplicateReferenceError` surfaces
    it as a distinct outcome instead of a generic client error.
    """

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
        """Creates a Razorpay payment link for the outstanding balance.

        Duplicate creation is prevented by `reference_id=invoice_id`, which
        Razorpay enforces as unique — see the class docstring. Raises
        DuplicateReferenceError if a link for this invoice already exists.
        """
        if not isinstance(amount_paise, int) or isinstance(amount_paise, bool):
            raise ValueError(
                f"amount_paise for {invoice_id} must be an int, got "
                f"{type(amount_paise).__name__} — money is never a float"
            )
        if amount_paise <= 0:
            raise ValueError(f"amount_paise for {invoice_id} must be positive, got {amount_paise}")

        try:
            response = httpx.post(
                f"{self.BASE_URL}/payment_links",
                headers=self.headers,
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
            body = response.text[:500]
            if "reference_id" in body and "exist" in body.lower():
                raise DuplicateReferenceError(
                    f"A payment link already exists for reference_id={invoice_id}"
                )
            raise ValueError(
                f"Razorpay API error {response.status_code} for {invoice_id}: {body}"
            )

        try:
            payload = response.json()
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Razorpay response was not valid JSON for {invoice_id}: {e}"
            ) from e

        # A 200 with a shape we do not recognise must not be treated as success:
        # without an id there is nothing to reconcile against later.
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError(
                f"Razorpay returned {response.status_code} for {invoice_id} without a "
                f"payment link id: {str(payload)[:300]}"
            )
        return payload

    def verify_payment(self, payment_link_id: str) -> dict:
        """Fetches a payment link's current status (and amount_paid) for reconciliation."""
        try:
            response = httpx.get(
                f"{self.BASE_URL}/payment_links/{payment_link_id}",
                headers=self.headers,
                timeout=15.0
            )
        except httpx.RequestError as e:
            raise ServerError(
                f"Razorpay verify_payment request failed for {payment_link_id}: {e}"
            ) from e

        if response.status_code == 429:
            raise RateLimitError("Razorpay rate limit hit")
        if response.status_code >= 500:
            raise ServerError(f"Razorpay server error: {response.status_code}")
        if response.status_code >= 400:
            raise ValueError(
                f"Razorpay API error {response.status_code} for {payment_link_id}: {response.text[:500]}"
            )

        try:
            payload = response.json()
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Razorpay response was not valid JSON for {payment_link_id}: {e}"
            ) from e

        if not isinstance(payload, dict) or "status" not in payload:
            raise ValueError(
                f"Razorpay payment link {payment_link_id} returned no status field: "
                f"{str(payload)[:300]}"
            )
        return payload
import httpx
import base64
import uuid

class RazorpayClient:
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
        idempotency_key = f"{invoice_id}-payment-link"
        
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

        if response.status_code == 429:
            raise RateLimitError("Razorpay rate limit hit")
        if response.status_code >= 500:
            raise ServerError(f"Razorpay server error: {response.status_code}")

        return response.json()
"""Unit tests for RazorpayClient.create_payment_link and .verify_payment.

`execution/test_razorpay_client.py` covers `fetch_overdue_invoices`; these two
methods — the ones that actually move money and then confirm it — had no test.
Both are pure HTTP-status-to-exception mapping plus response validation, which
is exactly the kind of code that looks obviously correct and silently isn't:
the difference between raising `RateLimitError` and `ValueError` on a 429
decides whether an invoice is retried or dropped.

`httpx.post` / `httpx.get` are monkeypatched, so no account and no network.
"""

import json

import pytest

from execution.razorpay_client import (
    DuplicateReferenceError,
    RateLimitError,
    RazorpayClient,
    ServerError,
)

import httpx

INVOICE_ID = "INV-LINK-001"
AMOUNT = 75_000


class FakeResponse:
    def __init__(self, status_code, payload=None, text=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._bad_json:
            raise json.JSONDecodeError("Expecting value", "<html>", 0)
        return self._payload


@pytest.fixture
def client():
    return RazorpayClient(key_id="rzp_test_id", key_secret="rzp_test_secret")


def link_payload(**overrides):
    payload = {
        "id": "plink_XYZ",
        "short_url": "https://rzp.io/i/XYZ",
        "reference_id": INVOICE_ID,
        "amount": AMOUNT,
        "status": "created",
    }
    payload.update(overrides)
    return payload


def create(client, amount_paise=AMOUNT):
    return client.create_payment_link(
        invoice_id=INVOICE_ID,
        amount_paise=amount_paise,
        client_name="Link Test Client",
        description="test link",
    )


# ── create_payment_link: input validation, before any HTTP happens ───────

@pytest.mark.parametrize(
    "amount", [75_000.0, "75000", None, True],
    ids=["float", "string", "none", "bool"],
)
def test_a_non_integer_amount_is_refused_without_calling_the_api(client, monkeypatch, amount):
    """Money is never a float. `True` is included because bool subclasses int."""
    def explode(*args, **kwargs):
        raise AssertionError("the API must not be called with an invalid amount")

    monkeypatch.setattr("httpx.post", explode)

    with pytest.raises(ValueError, match="must be an int"):
        create(client, amount_paise=amount)


@pytest.mark.parametrize("amount", [0, -1])
def test_a_non_positive_amount_is_refused(client, monkeypatch, amount):
    monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResponse(200, link_payload()))

    with pytest.raises(ValueError, match="must be positive"):
        create(client, amount_paise=amount)


# ── create_payment_link: the request it actually sends ───────────────────

def test_the_link_request_carries_the_invoice_as_its_reference_id(client, monkeypatch):
    """reference_id is the whole duplicate-protection mechanism — Razorpay
    enforces it as unique per account, and it is what reconciliation later
    matches on. If it stopped being the invoice id, both would break quietly.
    """
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update({"url": url, "body": json})
        return FakeResponse(200, link_payload())

    monkeypatch.setattr("httpx.post", fake_post)
    create(client)

    assert sent["body"]["reference_id"] == INVOICE_ID
    assert sent["body"]["amount"] == AMOUNT
    assert sent["body"]["currency"] == "INR"
    assert sent["url"].endswith("/payment_links")


def test_razorpay_is_told_not_to_notify_the_customer(client, monkeypatch):
    """The agent decides what reaches a client, not Razorpay's own mailer.

    A test-mode link that emails the customer would be a real message sent to
    a real address from a demo — so this is asserted rather than assumed.
    """
    sent = {}
    monkeypatch.setattr(
        "httpx.post",
        lambda url, headers, json, timeout: (sent.update(json), FakeResponse(200, link_payload()))[1],
    )
    create(client)

    assert sent["notify"] == {"sms": False, "email": False}


# ── create_payment_link: status mapping ──────────────────────────────────

def test_rate_limit_maps_to_rate_limit_error(client, monkeypatch):
    """Must be its own type: it is the only failure the caller retries."""
    monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResponse(429, text="rate limited"))

    with pytest.raises(RateLimitError):
        create(client)


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_map_to_server_error(client, monkeypatch, status):
    monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResponse(status, text="boom"))

    with pytest.raises(ServerError):
        create(client)


def test_a_duplicate_reference_id_is_recognised_from_the_error_body(client, monkeypatch):
    """Razorpay signals this as a plain 400; only the body distinguishes it."""
    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **k: FakeResponse(
            400, text="reference_id already exists for another payment link"
        ),
    )

    with pytest.raises(DuplicateReferenceError):
        create(client)


def test_an_ordinary_client_error_stays_a_value_error(client, monkeypatch):
    """A 400 that is not the duplicate case must not be mistaken for one."""
    monkeypatch.setattr(
        "httpx.post", lambda *a, **k: FakeResponse(400, text="amount is invalid")
    )

    with pytest.raises(ValueError) as excinfo:
        create(client)
    assert not isinstance(excinfo.value, DuplicateReferenceError)


def test_a_network_failure_maps_to_server_error(client, monkeypatch):
    def fake_post(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.post", fake_post)

    with pytest.raises(ServerError):
        create(client)


# ── create_payment_link: response validation ─────────────────────────────

def test_a_success_response_that_is_not_json_is_rejected(client, monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResponse(200, bad_json=True))

    with pytest.raises(ValueError, match="not valid JSON"):
        create(client)


def test_a_200_without_a_link_id_is_not_treated_as_success(client, monkeypatch):
    """Without an id there is nothing to reconcile against later, so a
    shape we do not recognise must fail rather than be recorded."""
    payload = link_payload()
    del payload["id"]
    monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResponse(200, payload))

    with pytest.raises(ValueError, match="without a"):
        create(client)


def test_a_valid_response_is_returned_intact(client, monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResponse(200, link_payload()))

    result = create(client)

    assert result["id"] == "plink_XYZ"
    assert result["short_url"] == "https://rzp.io/i/XYZ"


# ── verify_payment ───────────────────────────────────────────────────────

def test_verify_payment_requests_the_link_by_id(client, monkeypatch):
    seen = {}

    def fake_get(url, headers, timeout):
        seen["url"] = url
        return FakeResponse(200, link_payload(status="paid"))

    monkeypatch.setattr("httpx.get", fake_get)
    client.verify_payment("plink_XYZ")

    assert seen["url"].endswith("/payment_links/plink_XYZ")


def test_verify_payment_returns_the_payload(client, monkeypatch):
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: FakeResponse(200, link_payload(status="paid", amount_paid=AMOUNT))
    )

    result = client.verify_payment("plink_XYZ")

    assert result["status"] == "paid"
    assert result["amount_paid"] == AMOUNT


def test_verify_payment_rate_limit_maps_to_rate_limit_error(client, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(429, text="slow down"))

    with pytest.raises(RateLimitError):
        client.verify_payment("plink_XYZ")


def test_verify_payment_server_error_maps_to_server_error(client, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(503, text="boom"))

    with pytest.raises(ServerError):
        client.verify_payment("plink_XYZ")


def test_verify_payment_client_error_maps_to_value_error(client, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(404, text="no such link"))

    with pytest.raises(ValueError):
        client.verify_payment("plink_XYZ")


def test_verify_payment_network_failure_maps_to_server_error(client, monkeypatch):
    def fake_get(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("httpx.get", fake_get)

    with pytest.raises(ServerError):
        client.verify_payment("plink_XYZ")


def test_a_response_without_a_status_field_is_rejected(client, monkeypatch):
    """Reconciliation branches entirely on `status`. Absent is not 'unpaid' —
    it is an unrecognised response, and guessing would risk crediting money.
    """
    payload = link_payload()
    del payload["status"]
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(200, payload))

    with pytest.raises(ValueError, match="no status field"):
        client.verify_payment("plink_XYZ")


def test_verify_payment_non_json_response_is_rejected(client, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(200, bad_json=True))

    with pytest.raises(ValueError, match="not valid JSON"):
        client.verify_payment("plink_XYZ")

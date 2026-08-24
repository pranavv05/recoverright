"""Unit tests for RazorpayClient.fetch_overdue_invoices().

No live Razorpay account needed: httpx.get is monkeypatched to return
canned responses, so these exercise the pagination/filtering/error-mapping
logic in isolation. Manual verification against a real test-mode account is
covered separately (see README § Razorpay Native Sync).
"""

import time

import pytest

from execution.razorpay_client import (
    AuthenticationError,
    RateLimitError,
    RazorpayClient,
    ServerError,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        return self._payload


@pytest.fixture
def client():
    return RazorpayClient(key_id="rzp_test_id", key_secret="rzp_test_secret")


def _invoice(id_, status="issued", due_offset_days=-10, amount=100000):
    return {
        "id": id_,
        "status": status,
        "due_date": int(time.time()) + due_offset_days * 86400,
        "amount": amount,
        "amount_paid": 0,
        "customer": {"name": "Test Customer"},
        "notes": {},
        "short_url": f"https://rzp.io/i/{id_}",
    }


def test_filters_out_paid_and_not_yet_overdue(client, monkeypatch):
    items = [
        _invoice("inv_paid", status="paid", due_offset_days=-10),
        _invoice("inv_overdue", status="issued", due_offset_days=-10),
        _invoice("inv_future", status="issued", due_offset_days=10),
    ]

    def fake_get(url, headers, params, timeout):
        return FakeResponse(200, {"items": items})

    monkeypatch.setattr("httpx.get", fake_get)

    result = client.fetch_overdue_invoices(days_overdue_min=1)

    assert [inv["id"] for inv in result] == ["inv_overdue"]


def test_paginates_across_full_pages(client, monkeypatch):
    page_1 = [_invoice(f"inv_{i}") for i in range(RazorpayClient.INVOICE_PAGE_SIZE)]
    page_2 = [_invoice("inv_last")]
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append(params["skip"])
        page = page_1 if params["skip"] == 0 else page_2
        return FakeResponse(200, {"items": page})

    monkeypatch.setattr("httpx.get", fake_get)

    result = client.fetch_overdue_invoices()

    assert calls == [0, RazorpayClient.INVOICE_PAGE_SIZE]
    assert len(result) == RazorpayClient.INVOICE_PAGE_SIZE + 1


def test_raises_authentication_error_on_401(client, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(401, text="unauthorized"))

    with pytest.raises(AuthenticationError):
        client.fetch_overdue_invoices()


def test_raises_rate_limit_error_on_429(client, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(429))

    with pytest.raises(RateLimitError):
        client.fetch_overdue_invoices()


def test_raises_server_error_on_5xx(client, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(500))

    with pytest.raises(ServerError):
        client.fetch_overdue_invoices()


def test_raises_value_error_when_items_missing(client, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(200, {"unexpected": True}))

    with pytest.raises(ValueError):
        client.fetch_overdue_invoices()


def test_no_overdue_invoices_returns_empty_list(client, monkeypatch):
    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(200, {"items": []}))

    assert client.fetch_overdue_invoices() == []


def test_falls_back_to_expire_by_when_due_date_absent(client, monkeypatch):
    """Live Razorpay accounts return `expire_by`, not `due_date` — see
    execution.razorpay_client.invoice_due_date()."""
    inv = _invoice("inv_expire_by")
    del inv["due_date"]
    inv["expire_by"] = int(time.time()) - 10 * 86400

    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(200, {"items": [inv]}))

    result = client.fetch_overdue_invoices(days_overdue_min=1)

    assert [i["id"] for i in result] == ["inv_expire_by"]


def test_invoice_with_neither_due_date_nor_expire_by_is_excluded(client, monkeypatch):
    inv = _invoice("inv_no_date")
    del inv["due_date"]

    monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResponse(200, {"items": [inv]}))

    assert client.fetch_overdue_invoices() == []

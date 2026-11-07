"""PayPalPayments tests with PayPal's HTTP API mocked."""
import json

import httpx
import pytest

from app import payments, service, store
from app.models import Order
from app.payments import PaymentError, PayPalPayments

EMAILS = {"sup_a": "supplier-a@example.com"}


def _order(supplier_id: str = "sup_a") -> Order:
    """A small valid order."""
    return Order(
        sku="OAT-MILK", supplier_id=supplier_id, qty=30,
        unit_price=3.2, category="ingredients",
    )


def _paypal(handler) -> tuple[PayPalPayments, list]:
    """A PayPalPayments whose HTTP calls go to handler."""
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        """Keep each request, then answer it with the handler."""
        calls.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(record))
    return PayPalPayments("cid", "secret", EMAILS, http=http), calls


def _ok(request: httpx.Request) -> httpx.Response:
    """Answer token and payout calls like the sandbox does."""
    if request.url.path == "/v1/oauth2/token":
        return httpx.Response(
            200, json={"access_token": "tok", "expires_in": 3600}
        )
    return httpx.Response(201, json={"batch_header": {
        "payout_batch_id": "BATCH123", "batch_status": "PENDING",
    }})


def test_pay_supplier_sends_payout():
    """A payout goes to the supplier's email for the order total."""
    backend, calls = _paypal(_ok)
    result = backend.pay_supplier(_order())
    assert result == {"status": "PAID", "ref": "PAYPAL-BATCH123"}
    payout = calls[1]
    assert payout.url.path == "/v1/payments/payouts"
    assert payout.headers["Authorization"] == "Bearer tok"
    assert payout.headers["PayPal-Request-Id"]
    item = json.loads(payout.content)["items"][0]
    assert item["receiver"] == "supplier-a@example.com"
    assert item["amount"] == {"value": "96.00", "currency": "USD"}


def test_token_is_cached():
    """Two payments need only one OAuth call."""
    backend, calls = _paypal(_ok)
    backend.pay_supplier(_order())
    backend.pay_supplier(_order())
    paths = [c.url.path for c in calls]
    assert paths.count("/v1/oauth2/token") == 1


def test_bad_credentials_raise():
    """A 401 on the token call becomes a PaymentError."""
    backend, _ = _paypal(lambda r: httpx.Response(
        401, json={"error": "invalid_client"}
    ))
    with pytest.raises(PaymentError, match="auth failed"):
        backend.pay_supplier(_order())


def test_payout_error_raises():
    """A PayPal error on the payout call becomes a PaymentError."""
    def handler(request: httpx.Request) -> httpx.Response:
        """Grant a token but reject the payout."""
        if request.url.path == "/v1/oauth2/token":
            return _ok(request)
        return httpx.Response(422, json={
            "name": "INSUFFICIENT_FUNDS", "message": "Not enough money",
        })

    backend, _ = _paypal(handler)
    with pytest.raises(PaymentError, match="INSUFFICIENT_FUNDS"):
        backend.pay_supplier(_order())


def test_denied_batch_raises():
    """A DENIED batch status is treated as a failed payment."""
    def handler(request: httpx.Request) -> httpx.Response:
        """Accept the call but deny the batch."""
        if request.url.path == "/v1/oauth2/token":
            return _ok(request)
        return httpx.Response(201, json={"batch_header": {
            "payout_batch_id": "B1", "batch_status": "DENIED",
        }})

    backend, _ = _paypal(handler)
    with pytest.raises(PaymentError, match="DENIED"):
        backend.pay_supplier(_order())


def test_unknown_supplier_email_raises_without_calls():
    """No email for the supplier means no PayPal call is made."""
    backend, calls = _paypal(_ok)
    with pytest.raises(PaymentError, match="sup_b"):
        backend.pay_supplier(_order("sup_b"))
    assert calls == []


def test_network_error_raises():
    """A connection failure becomes a PaymentError."""
    def handler(request: httpx.Request) -> httpx.Response:
        """Simulate PayPal being unreachable."""
        raise httpx.ConnectError("down", request=request)

    backend, _ = _paypal(handler)
    with pytest.raises(PaymentError, match="request failed"):
        backend.pay_supplier(_order())


def test_parse_supplier_emails():
    """The env string becomes a supplier-to-email dict."""
    parsed = payments.parse_supplier_emails(
        " sup_a = a@x.com , sup_b=b@x.com, junk, "
    )
    assert parsed == {"sup_a": "a@x.com", "sup_b": "b@x.com"}


def test_failed_paypal_payment_marks_order_failed(monkeypatch):
    """When PayPal fails, the order is FAILED and nothing is spent."""
    monkeypatch.setenv("PAYMENTS_BACKEND", "paypal")
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    stock_before = {i["sku"]: i["stock"] for i in store.get_inventory()}
    result = service.process_order("OAT-MILK", "sup_a", 30, "test")
    assert result["status"] == "FAILED"
    assert store.get_mandate().spent_this_month == 0
    stock_after = {i["sku"]: i["stock"] for i in store.get_inventory()}
    assert stock_after == stock_before

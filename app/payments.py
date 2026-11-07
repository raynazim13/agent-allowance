"""Payment layer: a fake backend for tests and a PayPal sandbox backend.

PayPalPayments pays suppliers with the PayPal Payouts API
(POST /v1/payments/payouts), so the shop's agent can pay a supplier
without a buyer clicking "approve". Only the policy engine decides
whether pay_supplier is ever called.
"""
import os
import time
from functools import lru_cache
from typing import Optional, Protocol
from uuid import uuid4

import httpx

from app.models import Order

SANDBOX_URL = "https://api-m.sandbox.paypal.com"
TOKEN_MARGIN_S = 60
FAILED_BATCH_STATUSES = {"DENIED", "CANCELED"}


class PaymentError(Exception):
    """Raised when a supplier payment fails."""


class Payments(Protocol):
    """Interface every payment backend must follow."""

    def pay_supplier(self, order: Order) -> dict:
        """Pay the supplier and return {'status', 'ref'}."""


class FakePayments:
    """Pretends to pay. Used in tests and when no PayPal keys exist."""

    def pay_supplier(self, order: Order) -> dict:
        """Return a fake successful payment."""
        return {"status": "PAID", "ref": f"FAKE-{uuid4().hex[:8]}"}


def parse_supplier_emails(raw: str) -> dict[str, str]:
    """Parse 'sup_a=a@x.com,sup_b=b@x.com' into a dict."""
    emails: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        supplier_id, email = pair.split("=", 1)
        if supplier_id.strip() and email.strip():
            emails[supplier_id.strip()] = email.strip()
    return emails


class PayPalPayments:
    """Pays suppliers through PayPal Payouts (sandbox by default)."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        supplier_emails: dict[str, str],
        base_url: str = SANDBOX_URL,
        currency: str = "USD",
        http: Optional[httpx.Client] = None,
    ) -> None:
        """Store credentials and the supplier-to-PayPal-email map."""
        if not client_id or not client_secret:
            raise PaymentError("PayPal client ID or secret is missing")
        self.client_id = client_id
        self.client_secret = client_secret
        self.supplier_emails = supplier_emails
        self.base_url = base_url.rstrip("/")
        self.currency = currency
        self.http = http or httpx.Client(timeout=20)
        self._token = ""
        self._token_expires_at = 0.0

    def _get_token(self) -> str:
        """Return a cached OAuth token, fetching a new one if needed."""
        if self._token and time.time() < self._token_expires_at:
            return self._token
        try:
            resp = self.http.post(
                f"{self.base_url}/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
            )
        except httpx.HTTPError as exc:
            raise PaymentError(f"PayPal auth request failed: {exc}")
        if resp.status_code != 200:
            raise PaymentError(
                f"PayPal auth failed (HTTP {resp.status_code})"
            )
        body = resp.json()
        self._token = body["access_token"]
        expires_in = int(body.get("expires_in", 0))
        self._token_expires_at = time.time() + expires_in - TOKEN_MARGIN_S
        return self._token

    def _payout_body(self, order: Order, batch_id: str) -> dict:
        """Build the Payouts request body for one supplier order."""
        receiver = self.supplier_emails.get(order.supplier_id)
        if not receiver:
            raise PaymentError(
                f"No PayPal email configured for {order.supplier_id}"
            )
        return {
            "sender_batch_header": {
                "sender_batch_id": batch_id,
                "email_subject": "Payment for your order",
            },
            "items": [{
                "recipient_type": "EMAIL",
                "receiver": receiver,
                "amount": {
                    "value": f"{order.total:.2f}",
                    "currency": self.currency,
                },
                "note": f"{order.qty}x {order.sku}",
                "sender_item_id": f"{order.sku}-{batch_id}",
            }],
        }

    def pay_supplier(self, order: Order) -> dict:
        """Send a payout to the supplier and return its batch ID."""
        batch_id = f"aa-{uuid4().hex[:20]}"
        body = self._payout_body(order, batch_id)
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "PayPal-Request-Id": batch_id,
        }
        try:
            resp = self.http.post(
                f"{self.base_url}/v1/payments/payouts",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise PaymentError(f"PayPal payout request failed: {exc}")
        if resp.status_code not in (200, 201):
            raise PaymentError(_error_message(resp))
        header = resp.json().get("batch_header", {})
        status = header.get("batch_status", "")
        payout_id = header.get("payout_batch_id", "")
        if not payout_id or status in FAILED_BATCH_STATUSES:
            raise PaymentError(f"PayPal payout not accepted ({status})")
        return {"status": "PAID", "ref": f"PAYPAL-{payout_id}"}


def _error_message(resp: httpx.Response) -> str:
    """Turn a PayPal error response into a short message."""
    try:
        body = resp.json()
    except ValueError:
        body = {}
    name = body.get("name") or body.get("error") or "error"
    detail = body.get("message") or body.get("error_description") or ""
    return f"PayPal payout failed (HTTP {resp.status_code} {name}) {detail}"


@lru_cache(maxsize=4)
def _paypal_backend(
    client_id: str, client_secret: str, emails: str, base_url: str,
    currency: str,
) -> PayPalPayments:
    """Reuse one PayPal client per config so the token is cached."""
    return PayPalPayments(
        client_id, client_secret, parse_supplier_emails(emails),
        base_url, currency,
    )


def get_payments() -> Payments:
    """Pick the payment backend from the PAYMENTS_BACKEND env var."""
    backend = os.getenv("PAYMENTS_BACKEND", "fake")
    if backend == "fake":
        return FakePayments()
    if backend == "paypal":
        return _paypal_backend(
            os.getenv("PAYPAL_CLIENT_ID", ""),
            os.getenv("PAYPAL_CLIENT_SECRET", ""),
            os.getenv("PAYPAL_SUPPLIER_EMAILS", ""),
            os.getenv("PAYPAL_BASE_URL", SANDBOX_URL),
            os.getenv("PAYPAL_CURRENCY", "USD"),
        )
    raise PaymentError(f"Unknown PAYMENTS_BACKEND: {backend}")

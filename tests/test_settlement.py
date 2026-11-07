"""SENT -> PAID / FAILED: budget is reserved, stock waits for PAID."""
from pathlib import Path

import pytest

from app import agent, autopilot, service, store
from app.models import MandateUpdate, Order


class SlowPayments:
    """Accepts payouts as SENT; tests decide how each one ends."""

    def __init__(self) -> None:
        """Every payout starts in progress."""
        self.outcome = "SENT"
        self.sent = 0

    def pay_supplier(self, order: Order) -> dict:
        """Accept the payout without finishing it."""
        self.sent += 1
        return {"status": "SENT", "ref": f"PAYPAL-B{self.sent}"}

    def check_payment(self, ref: str) -> str:
        """Report whatever outcome the test set."""
        return self.outcome


@pytest.fixture
def slow(monkeypatch) -> SlowPayments:
    """Route all payments through a SlowPayments backend."""
    backend = SlowPayments()
    monkeypatch.setattr(service, "get_payments", lambda: backend)
    return backend


def _stock(sku: str) -> float:
    """Current stock of one item."""
    return (store.get_item(sku) or {})["stock"]


def _spent() -> float:
    """Money spent or reserved this month."""
    return store.get_mandate().spent_this_month


def test_sent_reserves_budget_but_adds_no_stock(slow):
    """An accepted payout counts as spent, stock waits."""
    before = _stock("OAT-MILK")
    result = service.process_order("OAT-MILK", "sup_a", 30)
    assert result["status"] == "SENT"
    assert _spent() == 96.0
    assert _stock("OAT-MILK") == before
    state = service.get_state()
    assert state["reserved"] == 96.0


def test_reconcile_paid_adds_stock_once(slow):
    """PAID delivers stock; a second reconcile changes nothing."""
    before = _stock("OAT-MILK")
    pid = service.process_order("OAT-MILK", "sup_a", 30)["id"]
    slow.outcome = "PAID"
    assert service.reconcile_payments() == [{"id": pid, "status": "PAID"}]
    assert service.reconcile_payments() == []
    assert _stock("OAT-MILK") == before + 30
    assert _spent() == 96.0
    assert store.get_purchase(pid)["status"] == "PAID"
    assert service.get_state()["reserved"] == 0


def test_reconcile_failed_releases_budget(slow):
    """FAILED gives the reserved money back and adds no stock."""
    before = _stock("OAT-MILK")
    pid = service.process_order("OAT-MILK", "sup_a", 30)["id"]
    slow.outcome = "FAILED"
    service.reconcile_payments()
    assert store.get_purchase(pid)["status"] == "FAILED"
    assert _spent() == 0
    assert _stock("OAT-MILK") == before
    actions = [e["action"] for e in store.list_log()]
    assert "payment_failed" in actions


def test_still_sent_changes_nothing(slow):
    """A payout still in progress stays SENT with budget reserved."""
    pid = service.process_order("OAT-MILK", "sup_a", 30)["id"]
    assert service.reconcile_payments() == []
    assert store.get_purchase(pid)["status"] == "SENT"
    assert _spent() == 96.0


def test_reserved_budget_counts_toward_limits(slow):
    """While a payout is in flight, the next order sees less budget."""
    service.update_mandate(MandateUpdate(monthly_budget=110))
    service.process_order("OAT-MILK", "sup_a", 30)
    second = service.process_order("CUPS-12OZ", "sup_a", 280)
    assert second["decision"] == "BLOCK"
    assert "budget" in second["reason"].lower()


def test_agent_does_not_reorder_while_paying(slow):
    """Items with a SENT order are not ordered again."""
    agent.run_agent()
    sent_before = slow.sent
    assert sent_before > 0
    agent.run_agent()
    assert slow.sent == sent_before
    skus = [p["sku"] for p in store.list_purchases("SENT")]
    assert len(skus) == len(set(skus))


def test_approved_held_order_goes_through_sent(slow):
    """Owner approval also reserves first and delivers on PAID."""
    held = service.process_order("WHOLE-MILK", "sup_a", 42)
    assert held["status"] == "PENDING"
    assert service.approve(held["id"])["status"] == "SENT"
    assert _spent() == 105.0
    slow.outcome = "PAID"
    service.reconcile_payments()
    assert store.get_purchase(held["id"])["status"] == "PAID"


def test_tick_reconciles_even_when_switches_off(slow):
    """The background loop settles payouts with play/autopilot off."""
    pid = service.process_order("OAT-MILK", "sup_a", 30)["id"]
    slow.outcome = "PAID"
    autopilot.tick()
    assert store.get_purchase(pid)["status"] == "PAID"


def test_fake_backend_pays_instantly():
    """With the fake backend an AUTO order is PAID at once."""
    before = _stock("OAT-MILK")
    result = service.process_order("OAT-MILK", "sup_a", 30)
    assert result["status"] == "PAID"
    assert _stock("OAT-MILK") == before + 30
    assert service.get_state()["reserved"] == 0


def test_ui_labels_sent_as_paying():
    """The dashboard shows SENT orders as 'Paying...'."""
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert 'SENT: "Paying..."' in html

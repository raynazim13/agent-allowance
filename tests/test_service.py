"""End-to-end tests of the order flow and the rule-based agent."""
from fastapi.testclient import TestClient

from app import agent, service, store
from app.main import app
from app.models import MandateUpdate


def by_sku(result: dict) -> dict:
    """Index agent orders by SKU."""
    return {order["sku"]: order for order in result["orders"]}


def test_agent_run_gives_auto_hold_and_block():
    """The seeded demo shows all three outcomes."""
    orders = by_sku(agent.run_agent())
    assert orders["OAT-MILK"]["status"] == "PAID"
    assert orders["CUPS-12OZ"]["status"] == "PAID"
    assert orders["WHOLE-MILK"]["status"] == "PENDING"
    assert orders["COFFEE-BEANS"]["status"] == "PENDING"
    assert orders["CLEANING-SPRAY"]["status"] == "BLOCKED"
    assert orders["TEA-BAGS"]["status"] == "BLOCKED"


def test_auto_payment_updates_budget_and_stock():
    """A paid order raises spend and stock."""
    before = store.get_item("OAT-MILK")["stock"]
    result = service.process_order("OAT-MILK", "sup_a", 30)
    assert result["status"] == "PAID"
    assert store.get_item("OAT-MILK")["stock"] == before + 30
    assert store.get_mandate().spent_this_month == 96.0


def test_approve_pays_held_order():
    """Owner approval pays a held order."""
    held = service.process_order("WHOLE-MILK", "sup_a", 42)
    assert held["status"] == "PENDING"
    assert service.approve(held["id"])["status"] == "PAID"


def test_deny_marks_denied():
    """Owner denial closes a held order without paying."""
    held = service.process_order("WHOLE-MILK", "sup_a", 42)
    assert service.deny(held["id"])["status"] == "DENIED"
    assert store.get_mandate().spent_this_month == 0


def test_kill_switch_blocks_approval():
    """Pausing the mandate stops even already-held orders."""
    held = service.process_order("WHOLE-MILK", "sup_a", 42)
    service.update_mandate(MandateUpdate(active=False))
    assert service.approve(held["id"])["status"] == "BLOCKED"


def test_unknown_supplier_is_error_not_saved():
    """Bad input never reaches the purchases table."""
    result = service.process_order("OAT-MILK", "nope", 5)
    assert result["status"] == "ERROR"
    assert store.list_purchases() == []


def test_advance_day_reduces_stock():
    """Fast-forward subtracts one day of sales."""
    before = store.get_item("OAT-MILK")["stock"]
    service.advance_day()
    assert store.get_item("OAT-MILK")["stock"] == before - 6


def test_api_smoke():
    """The HTTP layer returns state and runs the agent."""
    with TestClient(app) as client:
        assert client.get("/state").status_code == 200
        assert client.post("/agent/run").json()["orders"]


def test_repeat_agent_runs_do_not_pile_up():
    """Clicking Run agent again adds nothing new."""
    agent.run_agent()
    count = len(store.list_purchases())
    again = agent.run_agent()
    assert again["orders"] == []
    assert len(store.list_purchases()) == count


def test_rerun_after_mandate_change_retries_blocked():
    """A blocked order is retried once the rules allow it."""
    agent.run_agent()
    service.update_mandate(MandateUpdate(
        approved_suppliers=["sup_a", "sup_b", "sup_x"]))
    orders = by_sku(agent.run_agent())
    assert orders["TEA-BAGS"]["status"] == "PAID"


def test_ui_is_served_at_root():
    """The web UI loads at the site root."""
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Agent Allowance" in page.text

"""One test per policy rule."""
import pytest

from app.models import Decision, Mandate, Order
from app.policy import evaluate


@pytest.fixture
def mandate() -> Mandate:
    """A standard mandate."""
    return Mandate(
        monthly_budget=600, per_order_limit=200, approval_threshold=100,
        approved_suppliers=["sup_a"], allowed_categories=["ingredients"],
        max_price_spike_pct=20,
    )


def make_order(**overrides) -> Order:
    """Build an order that passes every rule unless overridden."""
    data = {"sku": "X", "supplier_id": "sup_a", "qty": 10,
            "unit_price": 5.0, "category": "ingredients"}
    data.update(overrides)
    return Order(**data)


def test_auto_happy_path(mandate):
    """Small, normal order is auto-approved."""
    assert evaluate(make_order(), mandate, 5.0)[0] == Decision.AUTO


def test_inactive_mandate_blocks(mandate):
    """Kill switch blocks everything."""
    mandate.active = False
    assert evaluate(make_order(), mandate, 5.0)[0] == Decision.BLOCK


def test_unapproved_supplier_blocks(mandate):
    """Suppliers outside the approved list are blocked."""
    order = make_order(supplier_id="sup_x")
    assert evaluate(order, mandate, 5.0)[0] == Decision.BLOCK


def test_bad_category_blocks(mandate):
    """Categories outside the allowed list are blocked."""
    order = make_order(category="cleaning")
    assert evaluate(order, mandate, 5.0)[0] == Decision.BLOCK


def test_over_per_order_limit_blocks(mandate):
    """Orders above the per-order limit are blocked."""
    order = make_order(qty=50)
    assert evaluate(order, mandate, 5.0)[0] == Decision.BLOCK


def test_budget_exceeded_blocks(mandate):
    """Orders that overshoot the monthly budget are blocked."""
    mandate.spent_this_month = 580
    assert evaluate(make_order(), mandate, 5.0)[0] == Decision.BLOCK


def test_price_spike_holds(mandate):
    """A price far above usual needs human approval."""
    order = make_order(unit_price=7.0, qty=10)
    assert evaluate(order, mandate, 5.0)[0] == Decision.HOLD


def test_above_threshold_holds(mandate):
    """Orders above the auto-approve threshold are held."""
    order = make_order(qty=30)
    assert evaluate(order, mandate, 5.0)[0] == Decision.HOLD


def test_block_wins_over_hold(mandate):
    """A hard limit beats a soft limit when both apply."""
    order = make_order(supplier_id="sup_x", unit_price=9.0)
    assert evaluate(order, mandate, 5.0)[0] == Decision.BLOCK

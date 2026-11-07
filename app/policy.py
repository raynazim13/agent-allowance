"""Spending policy engine. Plain code: no AI decides anything here."""
from app.models import Decision, Mandate, Order


def evaluate(
    order: Order, mandate: Mandate, usual_price: float
) -> tuple[Decision, str]:
    """Return (decision, reason) for a proposed order.

    Hard limits return BLOCK. Soft limits return HOLD so a human can
    approve. Every BLOCK rule is checked before any HOLD rule.
    """
    total = order.total
    if not mandate.active:
        return Decision.BLOCK, "Mandate is paused (kill switch)"
    if order.supplier_id not in mandate.approved_suppliers:
        return Decision.BLOCK, "Supplier is not approved"
    if order.category not in mandate.allowed_categories:
        return Decision.BLOCK, f"Category '{order.category}' not allowed"
    if total > mandate.per_order_limit:
        return Decision.BLOCK, (
            f"Order ${total:.2f} is over the per-order limit "
            f"(${mandate.per_order_limit:.2f})"
        )
    remaining = mandate.monthly_budget - mandate.spent_this_month
    if total > remaining:
        return Decision.BLOCK, (
            f"Order ${total:.2f} exceeds remaining monthly budget "
            f"(${remaining:.2f})"
        )
    spike = 0.0
    if usual_price > 0:
        spike = (order.unit_price / usual_price - 1) * 100
    if spike > mandate.max_price_spike_pct:
        return Decision.HOLD, f"Price is {spike:.0f}% above the usual price"
    if total > mandate.approval_threshold:
        return Decision.HOLD, (
            f"Order ${total:.2f} is above the auto-approve threshold "
            f"(${mandate.approval_threshold:.2f})"
        )
    return Decision.AUTO, "Within all limits"

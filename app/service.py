"""Business logic: orders, approvals, time simulation and state."""
import threading

from pydantic import ValidationError

from app import store
from app.models import Decision, Mandate, MandateUpdate, Order
from app.payments import PaymentError, get_payments
from app.policy import evaluate


def _error(actor: str, detail: str) -> dict:
    """Log and return a non-saved error result."""
    store.log(actor, "order_error", detail)
    return {"status": "ERROR", "reason": detail}


# Serialises every change to budget, stock and payment status, so the
# agent thread, the autopilot loop and HTTP requests cannot race.
_money_lock = threading.RLock()


def _change_spent(delta: float) -> None:
    """Add delta to spent_this_month (negative releases budget)."""
    mandate = store.get_mandate()
    mandate.spent_this_month = round(
        max(0.0, mandate.spent_this_month + delta), 2
    )
    store.save_mandate(mandate)


def _settle_paid(purchase: dict, actor: str) -> None:
    """Money arrived: deliver the stock and mark the order PAID."""
    if not store.transition_purchase(purchase["id"], "SENT", "PAID"):
        return
    store.add_stock(purchase["sku"], purchase["qty"])
    store.log(
        actor, "paid",
        f"#{purchase['id']} {purchase['qty']}x {purchase['sku']} from "
        f"{purchase['supplier_id']} = ${purchase['total']:.2f} "
        f"({purchase['payment_ref']})",
    )


def _settle_failed(purchase: dict, actor: str) -> None:
    """Payout failed: release the reserved budget, add no stock."""
    if not store.transition_purchase(purchase["id"], "SENT", "FAILED"):
        return
    _change_spent(-purchase["total"])
    store.log(
        actor, "payment_failed",
        f"#{purchase['id']}: PayPal payout failed, "
        f"${purchase['total']:.2f} released ({purchase['payment_ref']})",
    )


def _pay(purchase_id: int, order: Order, actor: str) -> dict:
    """Start paying the supplier and reserve the budget.

    The order becomes SENT and its total counts as spent straight away,
    so in-flight payouts still count toward every limit. Stock is only
    added once the payment is confirmed PAID.
    """
    with _money_lock:
        try:
            result = get_payments().pay_supplier(order)
        except (PaymentError, NotImplementedError) as exc:
            store.update_purchase(purchase_id, "FAILED")
            store.log(actor, "payment_failed", f"#{purchase_id}: {exc}")
            return {
                "id": purchase_id, "status": "FAILED", "reason": str(exc),
            }
        ref = result["ref"]
        _change_spent(order.total)
        store.update_purchase(purchase_id, "SENT", ref)
        store.log(
            actor, "payment_sent",
            f"#{purchase_id} ${order.total:.2f} to {order.supplier_id} "
            f"reserved, payout in progress ({ref})",
        )
        if result["status"] == "PAID":
            _settle_paid(store.get_purchase(purchase_id) or {}, actor)
        status = (store.get_purchase(purchase_id) or {}).get("status")
    return {"id": purchase_id, "status": status, "ref": ref}


def reconcile_payments() -> list[dict]:
    """Check every SENT payout and settle the ones that finished."""
    changed = []
    with _money_lock:
        sent = store.list_purchases("SENT")
        if not sent:
            return changed
        try:
            backend = get_payments()
        except PaymentError as exc:
            store.log("system", "reconcile_failed", str(exc))
            return changed
        for purchase in sent:
            try:
                status = backend.check_payment(purchase["payment_ref"])
            except PaymentError:
                continue
            if status == "PAID":
                _settle_paid(purchase, "system")
            elif status == "FAILED":
                _settle_failed(purchase, "system")
            else:
                continue
            changed.append({"id": purchase["id"], "status": status})
    return changed


def _build_order(
    sku: str, supplier_id: str, qty: int, reason: str
) -> Order:
    """Build an order using trusted catalogue data, not the agent's."""
    item = store.get_item(sku)
    if item is None:
        raise ValueError(f"Unknown item '{sku}'")
    supplier = next(
        (s for s in store.get_suppliers() if s["id"] == supplier_id), None
    )
    if supplier is None:
        raise ValueError(f"Unknown supplier '{supplier_id}'")
    price = supplier["prices"].get(sku)
    if price is None:
        raise ValueError(f"{supplier_id} does not carry {sku}")
    return Order(
        sku=sku, supplier_id=supplier_id, qty=qty, unit_price=price,
        category=item["category"], reason=reason,
    )


def process_order(
    sku: str, supplier_id: str, qty: int, reason: str = "",
    actor: str = "agent",
) -> dict:
    """Run a proposed order through the policy engine and act on it."""
    try:
        order = _build_order(sku, supplier_id, qty, reason)
    except (ValueError, ValidationError) as exc:
        return _error(actor, f"{sku}/{supplier_id}/{qty}: {exc}")
    with _money_lock:
        return _decide_and_act(order, actor)


def _decide_and_act(order: Order, actor: str) -> dict:
    """Run the policy, save the purchase and pay if AUTO (locked)."""
    sku, supplier_id, qty = order.sku, order.supplier_id, order.qty
    item = store.get_item(sku) or {}
    decision, why = evaluate(
        order, store.get_mandate(), item.get("usual_price", 0)
    )
    status = {
        Decision.AUTO: "APPROVED",
        Decision.HOLD: "PENDING",
        Decision.BLOCK: "BLOCKED",
    }[decision]
    purchase_id = store.save_purchase({
        **order.model_dump(), "total": order.total,
        "decision": decision.value, "decision_reason": why,
        "status": status,
    })
    store.log(
        actor, decision.value.lower(),
        f"#{purchase_id} {qty}x {sku} @ ${order.unit_price} "
        f"from {supplier_id}: {why}",
    )
    outcome = {
        "id": purchase_id, "sku": sku, "supplier_id": supplier_id,
        "qty": qty, "total": order.total,
        "decision": decision.value, "reason": why, "status": status,
    }
    if decision == Decision.AUTO:
        outcome.update(_pay(purchase_id, order, actor))
    return outcome


def preview_order(
    sku: str, supplier_id: str, qty: int
) -> tuple[Decision, str]:
    """Dry run: what would the policy say? Nothing is saved."""
    order = _build_order(sku, supplier_id, qty, "")
    item = store.get_item(sku) or {}
    return evaluate(
        order, store.get_mandate(), item.get("usual_price", 0)
    )


def _order_from_purchase(purchase: dict) -> Order:
    """Rebuild an Order from a stored purchase row."""
    return Order(
        sku=purchase["sku"], supplier_id=purchase["supplier_id"],
        qty=purchase["qty"], unit_price=purchase["unit_price"],
        category=purchase["category"], reason=purchase["reason"] or "",
    )


def approve(purchase_id: int) -> dict:
    """Owner approves a held order. Hard limits are re-checked."""
    purchase = store.get_purchase(purchase_id)
    if purchase is None or purchase["status"] != "PENDING":
        raise ValueError("Purchase is not waiting for approval")
    order = _order_from_purchase(purchase)
    with _money_lock:
        item = store.get_item(order.sku) or {}
        decision, why = evaluate(
            order, store.get_mandate(), item.get("usual_price", 0)
        )
        if decision == Decision.BLOCK:
            store.update_purchase(purchase_id, "BLOCKED")
            store.log(
                "owner", "approve_blocked", f"#{purchase_id}: {why}"
            )
            return {"id": purchase_id, "status": "BLOCKED", "reason": why}
        store.log("owner", "approved", f"#{purchase_id} approved by owner")
        return _pay(purchase_id, order, "owner")


def deny(purchase_id: int) -> dict:
    """Owner denies a held order."""
    purchase = store.get_purchase(purchase_id)
    if purchase is None or purchase["status"] != "PENDING":
        raise ValueError("Purchase is not waiting for approval")
    store.update_purchase(purchase_id, "DENIED")
    store.log("owner", "denied", f"#{purchase_id} denied by owner")
    return {"id": purchase_id, "status": "DENIED"}


def advance_day() -> list[dict]:
    """Simulate one day of sales (the demo's fast-forward button)."""
    for item in store.get_inventory():
        store.add_stock(item["sku"], -item["daily_sales"])
    store.log("system", "advance_day", "One day of sales applied")
    return store.get_inventory()


def update_mandate(update: MandateUpdate) -> Mandate:
    """Apply a partial mandate update and log what changed."""
    changes = update.model_dump(exclude_unset=True)
    mandate = store.get_mandate().model_copy(update=changes)
    store.save_mandate(mandate)
    store.log("owner", "mandate_updated", str(changes))
    return mandate


def get_state() -> dict:
    """Everything the dashboard needs in one call."""
    inventory = store.get_inventory()
    for item in inventory:
        sales = item["daily_sales"]
        item["days_left"] = round(item["stock"] / sales, 1) if sales else None
        item["low"] = item["stock"] <= item["reorder_at"]
    sending = store.list_purchases("SENT")
    return {
        "inventory": inventory,
        "mandate": store.get_mandate().model_dump(),
        "reserved": round(sum(p["total"] for p in sending), 2),
        "pending": store.list_purchases("PENDING"),
        "purchases": store.list_purchases(),
        "log": store.list_log(),
        "day": store.count_log("advance_day"),
    }


def get_quotes(sku: str) -> list[dict]:
    """Supplier prices for one item, cheapest first."""
    quotes = [
        {"supplier_id": s["id"], "supplier": s["name"],
         "unit_price": s["prices"][sku], "lead_days": s["lead_days"]}
        for s in store.get_suppliers() if sku in s["prices"]
    ]
    return sorted(quotes, key=lambda q: q["unit_price"])


def reset_demo() -> None:
    """Reload the demo data."""
    store.reset_db()

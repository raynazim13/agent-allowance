"""Restocking agent. It can only spend through process_order."""
import json
import math
import os
import threading
import time

from openai import OpenAI, OpenAIError
from pydantic import ValidationError

from app import service, store
from app.models import Decision

COVER_DAYS = 7
MAX_STEPS = 15
_run_lock = threading.Lock()
_llm_calls: list[float] = []
last_run: dict = {"mode": None, "at": None, "summary": "", "model": None}

SYSTEM_PROMPT = (
    "You restock inventory for a small business. Call check_inventory "
    "once: it lists every low-stock item, most urgent first, with all "
    "supplier quotes. Orders are funded most urgent first. For "
    "each item pick the best supplier by price and lead time and order "
    f"about {COVER_DAYS} days of sales cover (suggested_qty is a good "
    "default). Send all orders in ONE propose_orders call. A policy "
    "engine decides each order; only report what it returns. Each "
    "result has a status. PAID: the supplier has been paid. SENT: the "
    "payment is in progress. PENDING (decision HOLD): waiting for the "
    "owner's approval, no money has moved, never call it a payment. "
    "BLOCKED: not bought. Never claim money was spent unless the status "
    "is PAID. If one is BLOCKED because the supplier is "
    "not approved, you may retry that item once with another supplier. "
    "Never split an order to get around a limit. Finish with a short "
    "summary."
)
LLM_TIMEOUT_S = 30
MAX_ORDERS_PER_CALL = 20

TOOLS = [
    {"type": "function", "function": {
        "name": "check_inventory",
        "description": "Low-stock items with all supplier quotes.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "propose_orders",
        "description": "Propose orders. A policy engine decides each.",
        "parameters": {
            "type": "object",
            "properties": {"orders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sku": {"type": "string"},
                        "supplier_id": {"type": "string"},
                        "qty": {"type": "integer"},
                        "reason": {"type": "string"}},
                    "required": ["sku", "supplier_id", "qty"]}}},
            "required": ["orders"]}}},
]


OPEN_STATUSES = ("PENDING", "SENT")


def _pending_skus() -> set[str]:
    """SKUs with an open order (awaiting the owner or still paying)."""
    return {
        p["sku"] for status in OPEN_STATUSES
        for p in store.list_purchases(status)
    }


def _is_repeat_block(sku: str, supplier_id: str, qty: int) -> bool:
    """True if this exact order was already blocked for the same reason."""
    last = next(
        (p for p in store.list_purchases() if p["sku"] == sku), None
    )
    if not last or last["status"] != "BLOCKED":
        return False
    if last["supplier_id"] != supplier_id or last["qty"] != qty:
        return False
    try:
        decision, why = service.preview_order(sku, supplier_id, qty)
    except (ValueError, ValidationError):
        return False
    return decision == Decision.BLOCK and why == last["decision_reason"]


def _candidates() -> list[dict]:
    """Low items worth ordering now, most urgent (fewest days) first.

    Skips items that already have an open order and exact repeats of
    an order that is still blocked for the same reason.
    """
    waiting = _pending_skus()
    found = []
    for item in store.get_inventory():
        if item["sku"] in waiting or item["stock"] > item["reorder_at"]:
            continue
        quotes = service.get_quotes(item["sku"])
        if not quotes:
            continue
        qty = _target_qty(item)
        if _is_repeat_block(item["sku"], quotes[0]["supplier_id"], qty):
            continue
        left = service.days_left(item)
        found.append({
            **item, "suggested_qty": qty, "best_quote": quotes[0],
            "days_left": round(left, 1) if left != float("inf") else None,
            "_urgency": left,
        })
    return sorted(found, key=lambda c: c["_urgency"])


def needs_action() -> bool:
    """True if the agent has something worth ordering right now."""
    return bool(_candidates())


INVENTORY_FIELDS = (
    "sku", "category", "stock", "daily_sales", "usual_price",
    "suggested_qty", "days_left",
)
QUOTE_FIELDS = ("supplier_id", "unit_price", "lead_days")


def check_inventory() -> list[dict]:
    """Low-stock items with all supplier quotes (tool for the LLM).

    Quotes are included so the model does not need one extra call per
    item, which keeps a run inside a free tier's tokens-per-minute.
    """
    found = []
    for c in _candidates():
        entry = {k: c[k] for k in INVENTORY_FIELDS}
        entry["quotes"] = [
            {k: q[k] for k in QUOTE_FIELDS}
            for q in service.get_quotes(c["sku"])
        ]
        found.append(entry)
    return found


def _target_qty(item: dict) -> int:
    """Units needed to reach COVER_DAYS of cover."""
    return max(1, math.ceil(item["daily_sales"] * COVER_DAYS - item["stock"]))


def _propose_one(order: object, orders: list[dict]) -> dict:
    """Send one LLM-proposed order through the policy engine."""
    if not isinstance(order, dict):
        return {"status": "ERROR", "reason": "Order must be an object"}
    try:
        qty = int(order.get("qty", 0))
    except (TypeError, ValueError):
        return {"status": "ERROR", "reason": "qty must be a whole number"}
    result = service.process_order(
        str(order.get("sku", "")), str(order.get("supplier_id", "")),
        qty, str(order.get("reason", "")),
    )
    orders.append(result)
    return result


def _most_urgent_first(batch: list) -> list:
    """Sort proposed orders so the item running out soonest goes first.

    Enforced in code, so the model cannot spend the budget on a less
    urgent item first. Unknown items go last.
    """
    urgency = {i["sku"]: service.days_left(i) for i in store.get_inventory()}

    def key(order: object) -> float:
        """Days left for the order's item."""
        sku = order.get("sku") if isinstance(order, dict) else None
        return urgency.get(str(sku), float("inf"))

    return sorted(batch, key=key)


def summarize(orders: list[dict]) -> str:
    """Plain-language report of what happened to each order."""
    if not orders:
        return "Nothing new to order right now."
    words = {
        "PAID": "paid", "SENT": "payment in progress",
        "PENDING": "needs your approval", "BLOCKED": "blocked",
        "FAILED": "payment failed", "ERROR": "rejected",
    }
    lines = [f"Ordered {len(orders)} items, most urgent first:"]
    for o in orders:
        status = o.get("status", "")
        line = f"- {o.get('sku', '?')}: {words.get(status, status)}"
        if status in ("PENDING", "BLOCKED", "ERROR") and o.get("reason"):
            line += f" ({o['reason']})"
        lines.append(line)
    return "\n".join(lines)


def _dispatch(name: str, args: dict, orders: list[dict]) -> object:
    """Run one tool call requested by the LLM."""
    if name == "check_inventory":
        return check_inventory()
    if name == "propose_orders":
        batch = args.get("orders")
        if not isinstance(batch, list):
            return {"error": "orders must be a list"}
        return [
            _propose_one(order, orders)
            for order in _most_urgent_first(batch[:MAX_ORDERS_PER_CALL])
        ]
    return {"error": f"Unknown tool {name}"}


def run_rule_based(mode: str = "rule_based") -> dict:
    """Simple planner used when no LLM is available (always works)."""
    orders = [
        service.process_order(
            c["sku"], c["best_quote"]["supplier_id"], c["suggested_qty"],
            f"Cheapest quote, {COVER_DAYS}-day cover",
        )
        for c in _candidates()
    ]
    store.log("agent", "run", f"{mode} run, {len(orders)} orders")
    return {"mode": mode, "orders": orders, "summary": summarize(orders),
            "model": None}


def run_llm(api_key: str) -> dict:
    """Let an OpenAI-compatible LLM plan the restock with tool calls."""
    client = OpenAI(
        api_key=api_key, base_url=os.getenv("LLM_BASE_URL") or None,
        timeout=LLM_TIMEOUT_S, max_retries=1,
    )
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    messages: list = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Check stock and restock what's low."},
    ]
    effort = os.getenv("LLM_REASONING_EFFORT", "").strip()
    extra = {"reasoning_effort": effort} if effort else {}
    orders: list[dict] = []
    summary = ""
    for _ in range(MAX_STEPS):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS, **extra
        )
        message = response.choices[0].message
        messages.append(message)
        if not message.tool_calls:
            summary = message.content or ""
            break
        for call in message.tool_calls:
            args = json.loads(call.function.arguments or "{}")
            output = _dispatch(call.function.name, args, orders)
            messages.append({
                "role": "tool", "tool_call_id": call.id,
                "content": json.dumps(output),
            })
    store.log("agent", "run", f"LLM run, {len(orders)} orders")
    return {
        "mode": "llm", "orders": orders,
        "summary": summary or summarize(orders), "model": model,
    }


def _llm_allowed() -> bool:
    """Cap AI calls (per hour and minimum gap) to protect a free key."""
    now = time.time()
    _llm_calls[:] = [t for t in _llm_calls if now - t < 3600]
    if len(_llm_calls) >= int(os.getenv("LLM_MAX_PER_HOUR", "15")):
        return False
    gap = float(os.getenv("LLM_MIN_GAP_S", "60"))
    return not _llm_calls or now - _llm_calls[-1] >= gap


def reset_llm_budget() -> None:
    """Forget past AI calls (used by tests)."""
    _llm_calls.clear()


def _run_once() -> dict:
    """LLM if a key is set and the cap allows, else rule-based."""
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return run_rule_based("rule_based")
    if not _llm_allowed():
        store.log("agent", "llm_capped", "AI limit reached; rule-based")
        return run_rule_based("rule_based_capped")
    _llm_calls.append(time.time())
    try:
        return run_llm(api_key)
    except (OpenAIError, json.JSONDecodeError) as exc:
        store.log("agent", "llm_failed", f"{exc}; using rule-based")
        return run_rule_based("rule_based_fallback")


def clear_last_run() -> None:
    """Forget the last run (after a demo reset)."""
    last_run.update(mode=None, at=None, summary="", model=None)


def run_agent() -> dict:
    """Run the agent once. Runs never overlap."""
    with _run_lock:
        result = _run_once()
    last_run.update(
        mode=result["mode"], at=time.time(),
        summary=result.get("summary", ""), model=result.get("model"),
    )
    return result

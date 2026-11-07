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
last_run: dict = {"mode": None, "at": None}

SYSTEM_PROMPT = (
    "You restock inventory for a small business. For each low-stock "
    "item, compare supplier quotes, pick the best supplier by price and "
    f"lead time, and order about {COVER_DAYS} days of sales cover "
    "(daily_sales x days minus current stock). Only report what "
    "propose_order returns. Never claim money was spent unless the "
    "status is PAID. If an order is HOLD, say it needs owner approval. "
    "If it is BLOCKED because the supplier is not approved, you may try "
    "a different supplier once. Never split an order to get around a "
    "limit. Finish with a short summary."
)

TOOLS = [
    {"type": "function", "function": {
        "name": "check_inventory",
        "description": "List items at or below their reorder level.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_supplier_quotes",
        "description": "Get supplier prices and lead times for one item.",
        "parameters": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"]}}},
    {"type": "function", "function": {
        "name": "propose_order",
        "description": "Propose an order. A policy engine decides.",
        "parameters": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "supplier_id": {"type": "string"},
                "qty": {"type": "integer"},
                "reason": {"type": "string"}},
            "required": ["sku", "supplier_id", "qty"]}}},
]


def _pending_skus() -> set[str]:
    """SKUs that already have an order waiting for the owner."""
    return {p["sku"] for p in store.list_purchases("PENDING")}


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
    """Low items worth ordering now (skips waiting and repeat blocks)."""
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
        sales = item["daily_sales"]
        found.append({
            **item, "suggested_qty": qty, "best_quote": quotes[0],
            "days_left": round(item["stock"] / sales, 1) if sales else None,
        })
    return found


def needs_action() -> bool:
    """True if the agent has something worth ordering right now."""
    return bool(_candidates())


def check_inventory() -> list[dict]:
    """Low-stock items the agent should consider (tool for the LLM)."""
    return [
        {k: v for k, v in c.items() if k != "best_quote"}
        for c in _candidates()
    ]


def _target_qty(item: dict) -> int:
    """Units needed to reach COVER_DAYS of cover."""
    return max(1, math.ceil(item["daily_sales"] * COVER_DAYS - item["stock"]))


def _dispatch(name: str, args: dict, orders: list[dict]) -> object:
    """Run one tool call requested by the LLM."""
    if name == "check_inventory":
        return check_inventory()
    if name == "get_supplier_quotes":
        return service.get_quotes(args.get("sku", ""))
    if name == "propose_order":
        result = service.process_order(
            args.get("sku", ""), args.get("supplier_id", ""),
            int(args.get("qty", 0)), args.get("reason", ""),
        )
        orders.append(result)
        return result
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
    return {"mode": mode, "orders": orders,
            "summary": f"Proposed {len(orders)} new orders."}


def run_llm(api_key: str) -> dict:
    """Let an OpenAI-compatible LLM plan the restock with tool calls."""
    client = OpenAI(
        api_key=api_key, base_url=os.getenv("LLM_BASE_URL") or None
    )
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    messages: list = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Check stock and restock what's low."},
    ]
    orders: list[dict] = []
    summary = ""
    for _ in range(MAX_STEPS):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS
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
    return {"mode": "llm", "orders": orders, "summary": summary}


def _llm_allowed() -> bool:
    """Cap AI calls (per hour and minimum gap) to protect a free key."""
    now = time.time()
    _llm_calls[:] = [t for t in _llm_calls if now - t < 3600]
    if len(_llm_calls) >= int(os.getenv("LLM_MAX_PER_HOUR", "15")):
        return False
    gap = float(os.getenv("LLM_MIN_GAP_S", "20"))
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


def run_agent() -> dict:
    """Run the agent once. Runs never overlap."""
    with _run_lock:
        result = _run_once()
    last_run.update(mode=result["mode"], at=time.time())
    return result

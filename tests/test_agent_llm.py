"""LLM agent path with a scripted fake model (no network)."""
import json
from types import SimpleNamespace

import pytest
from openai import APIConnectionError

from app import agent, store


def _call(call_id: str, name: str, args: dict) -> SimpleNamespace:
    """One tool call as the OpenAI client returns it."""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _reply(tool_calls: list | None = None, content: str = "") -> object:
    """A chat completion with one message."""
    message = SimpleNamespace(
        role="assistant", content=content, tool_calls=tool_calls or None
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ScriptedLLM:
    """Fake OpenAI client that returns replies in order."""

    def __init__(self, replies: list) -> None:
        """Keep the script and record every request."""
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs) -> object:
        """Return the next scripted reply (or raise it)."""
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def llm(monkeypatch):
    """Enable the LLM path and return a function to script it."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MIN_GAP_S", "0")

    def script(replies: list) -> ScriptedLLM:
        """Install a ScriptedLLM as the OpenAI client."""
        fake = ScriptedLLM(replies)
        monkeypatch.setattr(agent, "OpenAI", lambda **_: fake)
        return fake

    return script


def test_check_inventory_includes_quotes():
    """Each low item comes with every supplier quote."""
    items = {i["sku"]: i for i in agent.check_inventory()}
    tea = items["TEA-BAGS"]
    assert {q["supplier_id"] for q in tea["quotes"]} == {"sup_b", "sup_x"}
    assert "best_quote" not in tea
    assert tea["suggested_qty"] == 140


def test_llm_batch_goes_through_policy(llm):
    """A propose_orders batch is decided order by order by the policy."""
    fake = llm([
        _reply([_call("c1", "check_inventory", {})]),
        _reply([_call("c2", "propose_orders", {"orders": [
            {"sku": "OAT-MILK", "supplier_id": "sup_a", "qty": 30},
            {"sku": "TEA-BAGS", "supplier_id": "sup_x", "qty": 140},
            {"sku": "COFFEE-BEANS", "supplier_id": "sup_a", "qty": 7},
        ]})]),
        _reply(content="Done."),
    ])
    result = agent.run_agent()
    assert result["mode"] == "llm"
    assert [o["status"] for o in result["orders"]] == [
        "PAID", "BLOCKED", "PENDING",
    ]
    assert result["summary"] == "Done."
    tool_msgs = [
        m for m in fake.requests[-1]["messages"]
        if isinstance(m, dict) and m["role"] == "tool"
    ]
    batch_reply = json.loads(tool_msgs[-1]["content"])
    assert batch_reply[1]["decision"] == "BLOCK"


def test_llm_cannot_set_price(llm):
    """A price sent by the model is ignored; the catalogue price is used."""
    llm([
        _reply([_call("c1", "propose_orders", {"orders": [
            {"sku": "COFFEE-BEANS", "supplier_id": "sup_a", "qty": 7,
             "unit_price": 1.0},
        ]})]),
        _reply(content="Done."),
    ])
    order = agent.run_agent()["orders"][0]
    assert order["total"] == 147.0
    assert order["status"] == "PENDING"


def test_bad_qty_returns_error_without_crashing(llm):
    """Bad arguments become tool errors; valid orders still run."""
    llm([
        _reply([_call("c1", "propose_orders", {"orders": [
            {"sku": "OAT-MILK", "supplier_id": "sup_a", "qty": "lots"},
            "not an object",
            {"sku": "CUPS-12OZ", "supplier_id": "sup_a", "qty": 280},
        ]})]),
        _reply([_call("c2", "propose_orders", {"orders": "nope"})]),
        _reply(content="Done."),
    ])
    result = agent.run_agent()
    assert result["mode"] == "llm"
    assert [o["sku"] for o in result["orders"]] == ["CUPS-12OZ"]


def test_batch_is_capped(llm):
    """At most MAX_ORDERS_PER_CALL orders run from one call."""
    many = [{"sku": "NOPE", "supplier_id": "sup_a", "qty": 1}] * 50
    llm([
        _reply([_call("c1", "propose_orders", {"orders": many})]),
        _reply(content="Done."),
    ])
    result = agent.run_agent()
    assert len(result["orders"]) == agent.MAX_ORDERS_PER_CALL
    assert store.list_purchases() == []


def test_api_error_falls_back_to_rule_based(llm):
    """If the AI call fails, the rule-based planner still restocks."""
    llm([APIConnectionError(request=None)])  # type: ignore[arg-type]
    result = agent.run_agent()
    assert result["mode"] == "rule_based_fallback"
    assert len(result["orders"]) == 6


def test_reasoning_effort_passed_only_when_set(llm, monkeypatch):
    """LLM_REASONING_EFFORT is sent to the API only if configured."""
    fake = llm([_reply(content="Nothing to do.")])
    agent.run_agent()
    assert "reasoning_effort" not in fake.requests[0]
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    agent.reset_llm_budget()
    fake = llm([_reply(content="Nothing to do.")])
    agent.run_agent()
    assert fake.requests[0]["reasoning_effort"] == "low"


def test_prompt_defines_every_status():
    """PENDING must read as 'waiting for approval', never a payment.

    Regression: the model once reported held (PENDING) orders as
    "payment is in progress" because the prompt only defined HOLD.
    """
    prompt = agent.SYSTEM_PROMPT
    for status in ("PAID", "SENT", "PENDING", "BLOCKED"):
        assert f"{status}" in prompt
    assert "PENDING (decision HOLD): waiting for the owner's approval" in (
        prompt
    )
    assert "no money has moved" in prompt

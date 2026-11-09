"""Urgency ordering, reason tags, agent report and UI hooks."""
from pathlib import Path

from fastapi.testclient import TestClient

from app import agent, service, store
from app.main import app
from app.models import MandateUpdate
from tests.test_agent_llm import ScriptedLLM, _call, _reply
from tests.test_settlement import SlowPayments

UI = Path("app/static/index.html").read_text(encoding="utf-8")


def _tags(sku: str) -> list[str]:
    """Reason tags shown for one inventory item."""
    items = {i["sku"]: i for i in service.get_state()["inventory"]}
    return items[sku]["tags"]


# ---- urgency -------------------------------------------------------------

def test_inventory_sorted_most_urgent_first():
    """Inventory comes back with the fewest days left first."""
    days = [i["days_left"] for i in service.get_state()["inventory"]]
    assert days == sorted(days)
    assert service.get_state()["inventory"][0]["sku"] == "WHOLE-MILK"


def test_candidates_sorted_by_days_left():
    """The agent looks at the most urgent items first."""
    skus = [c["sku"] for c in agent.check_inventory()]
    assert skus[:2] == ["WHOLE-MILK", "OAT-MILK"]
    days = [c["days_left"] for c in agent.check_inventory()]
    assert days == sorted(days)


def test_tight_budget_funds_urgent_item_first():
    """With $100 left, urgent oat milk is paid and cups are blocked.

    Alphabetically, cups (2.3 days) would be paid first and leave too
    little for oat milk (2.0 days).
    """
    service.update_mandate(MandateUpdate(monthly_budget=100))
    orders = {o["sku"]: o for o in agent.run_agent()["orders"]}
    assert orders["OAT-MILK"]["status"] == "PAID"
    assert orders["CUPS-12OZ"]["status"] == "BLOCKED"
    assert "budget" in orders["CUPS-12OZ"]["reason"].lower()


def test_llm_batch_is_reordered_by_urgency(monkeypatch):
    """Even if the model lists cups first, oat milk is funded first."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    service.update_mandate(MandateUpdate(monthly_budget=100))
    fake = ScriptedLLM([
        _reply([_call("c1", "propose_orders", {"orders": [
            {"sku": "CUPS-12OZ", "supplier_id": "sup_a", "qty": 280},
            {"sku": "OAT-MILK", "supplier_id": "sup_a", "qty": 30},
        ]})]),
        _reply(content="Done."),
    ])
    monkeypatch.setattr(agent, "OpenAI", lambda **_: fake)
    result = agent.run_agent()
    assert result["mode"] == "llm"
    assert [o["sku"] for o in result["orders"]] == ["OAT-MILK", "CUPS-12OZ"]
    assert [o["status"] for o in result["orders"]] == ["PAID", "BLOCKED"]


def test_approval_queue_sorted_with_days_left():
    """Held orders show days left and the most urgent comes first."""
    agent.run_agent()
    pending = service.get_state()["pending"]
    assert [p["sku"] for p in pending] == ["WHOLE-MILK", "COFFEE-BEANS"]
    assert pending[0]["days_left"] == 1.8


# ---- reason tags ---------------------------------------------------------

def test_tags_explain_why_items_stay_low():
    """Held and blocked items say why they are still low."""
    agent.run_agent()
    assert _tags("WHOLE-MILK") == ["Awaiting your approval"]
    assert _tags("CLEANING-SPRAY") == [
        "Blocked: Category 'cleaning' not allowed"
    ]
    assert _tags("TEA-BAGS")[0].startswith("Blocked: Supplier")
    assert _tags("OAT-MILK") == []


def test_denied_and_out_of_stock_tags():
    """Denied orders and empty shelves are tagged."""
    agent.run_agent()
    coffee = next(
        p for p in service.get_state()["pending"]
        if p["sku"] == "COFFEE-BEANS"
    )
    service.deny(coffee["id"])
    assert _tags("COFFEE-BEANS") == ["Denied by you"]
    for _ in range(5):
        service.advance_day()
    assert _tags("COFFEE-BEANS") == ["Out of stock", "Denied by you"]


def test_paying_tag(monkeypatch):
    """An item whose payout is in flight is tagged Paying..."""
    backend = SlowPayments()
    monkeypatch.setattr(service, "get_payments", lambda: backend)
    service.process_order("OAT-MILK", "sup_a", 30)
    assert _tags("OAT-MILK") == ["Paying..."]


def test_tags_clear_after_rerouted_order_is_paid():
    """A blocked item later bought elsewhere shows no blocked tag."""
    service.process_order("TEA-BAGS", "sup_x", 140)
    assert _tags("TEA-BAGS")[0].startswith("Blocked")
    service.process_order("TEA-BAGS", "sup_b", 140)
    assert _tags("TEA-BAGS") == []


# ---- agent report --------------------------------------------------------

def test_rule_based_summary_lists_each_order():
    """The rule-based planner writes a readable per-item report."""
    summary = agent.run_agent()["summary"]
    assert summary.startswith("Ordered 6 items, most urgent first:")
    assert "- WHOLE-MILK: needs your approval" in summary
    assert "- CLEANING-SPRAY: blocked (Category" in summary
    assert "- OAT-MILK: paid" in summary


def test_last_run_has_summary_and_model(monkeypatch):
    """The state endpoint carries the report and model for the UI."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")
    fake = ScriptedLLM([_reply(content="All **done**.")])
    monkeypatch.setattr(agent, "OpenAI", lambda **_: fake)
    agent.run_agent()
    with TestClient(app) as client:
        run = client.get("/state").json()["autopilot"]["last_run"]
    assert run["mode"] == "llm"
    assert run["model"] == "openai/gpt-oss-120b"
    assert run["summary"] == "All **done**."


def test_empty_llm_summary_falls_back_to_report(monkeypatch):
    """If the model writes nothing, a code-made report is shown."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    fake = ScriptedLLM([
        _reply([_call("c1", "propose_orders", {"orders": [
            {"sku": "OAT-MILK", "supplier_id": "sup_a", "qty": 30},
        ]})]),
        _reply(content=""),
    ])
    monkeypatch.setattr(agent, "OpenAI", lambda **_: fake)
    assert "- OAT-MILK: paid" in agent.run_agent()["summary"]


def test_reset_clears_last_run():
    """After Reset demo the old report is gone."""
    agent.run_agent()
    with TestClient(app) as client:
        client.post("/reset")
        run = client.get("/state").json()["autopilot"]["last_run"]
    assert run["mode"] is None and run["summary"] == ""


def test_llm_min_gap_defaults_to_60s(monkeypatch):
    """Without LLM_MIN_GAP_S, a second AI run within 60 s is capped."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.delenv("LLM_MIN_GAP_S", raising=False)
    fake = ScriptedLLM([_reply(content="Done.")])
    monkeypatch.setattr(agent, "OpenAI", lambda **_: fake)
    assert agent.run_agent()["mode"] == "llm"
    assert agent.run_agent()["mode"] == "rule_based_capped"


# ---- UI hooks (static checks; the page has no JS test runner) ------------

def test_ui_has_report_panel_and_mode_label():
    """The page has the Agent's report panel and shows the AI model."""
    assert 'id="report"' in UI and "Agent's report" in UI
    assert '"AI: " + (run.model' in UI


def test_ui_renders_tags_urgency_and_flash():
    """Reason tags, 'Runs out in' and the stock-change flash exist."""
    assert "reasonTag" in UI and "i.tags" in UI
    assert "Runs out in" in UI
    assert "tr.flash td{animation:flash" in UI
    assert "lastStock[i.sku] !== i.stock" in UI


def test_ui_polls_while_paying():
    """The page keeps refreshing while any order is Paying..."""
    assert "const paying = state && state.reserved > 0;" in UI
    assert "a.play || a.autopilot || paying" in UI


def test_report_summary_is_escaped():
    """The AI summary is HTML-escaped before bold is applied."""
    assert 'esc(run.summary || "No summary.")' in UI

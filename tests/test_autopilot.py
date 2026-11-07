"""Tests for automation and the AI usage cap."""
from fastapi.testclient import TestClient

from app import agent, autopilot, service, store
from app.main import app


def test_tick_does_nothing_when_off():
    """With both switches off, a tick changes nothing."""
    autopilot.tick()
    assert service.get_state()["day"] == 0
    assert store.list_purchases() == []


def test_tick_advances_day_and_orders():
    """Play + autopilot: time moves and the agent orders by itself."""
    autopilot.update(play=True, autopilot=True)
    autopilot.tick()
    assert service.get_state()["day"] == 1
    assert store.list_purchases()


def test_needs_action_false_once_everything_is_handled():
    """After a run, nothing is left to do until stock changes."""
    assert agent.needs_action()
    agent.run_agent()
    assert not agent.needs_action()


def test_llm_cap_falls_back_to_rule_based(monkeypatch):
    """When the AI call limit is hit, the rule-based planner runs."""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MAX_PER_HOUR", "0")
    assert agent.run_agent()["mode"] == "rule_based_capped"


def test_autopilot_endpoint_and_state():
    """The API toggles settings and reports them in /state."""
    with TestClient(app) as client:
        reply = client.post("/autopilot", json={"play": True})
        assert reply.json()["play"] is True
        assert client.get("/state").json()["autopilot"]["play"] is True

"""Shared fixtures: every test gets a fresh temporary database."""
import pytest

from app import agent, autopilot, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Point the app at a new database seeded with demo data."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PAYMENTS_BACKEND", "fake")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    autopilot.update(play=False, autopilot=False, interval_seconds=5)
    agent.reset_llm_budget()
    store.reset_db()

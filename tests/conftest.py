"""Shared fixtures: every test gets a fresh temporary database."""
import pytest

from app import agent, autopilot, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Point the app at a new database seeded with demo data."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PAYMENTS_BACKEND", "fake")
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
                 "LLM_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    autopilot.update(play=False, autopilot=False, interval_seconds=5)
    agent.reset_llm_budget()
    agent.clear_last_run()
    store.reset_db()

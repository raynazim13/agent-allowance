"""Background loop: advances time and runs the agent on its own."""
import asyncio
import logging
from typing import Optional

from app import agent, service

logger = logging.getLogger("autopilot")
settings = {"play": False, "autopilot": False, "interval_seconds": 5.0}


def get_settings() -> dict:
    """Current settings plus the agent's last run."""
    return {**settings, "last_run": dict(agent.last_run)}


def update(
    play: Optional[bool] = None,
    autopilot: Optional[bool] = None,
    interval_seconds: Optional[float] = None,
) -> dict:
    """Change settings. The interval is kept between 2 and 60 seconds."""
    if play is not None:
        settings["play"] = play
    if autopilot is not None:
        settings["autopilot"] = autopilot
    if interval_seconds is not None:
        settings["interval_seconds"] = min(60.0, max(2.0, interval_seconds))
    return get_settings()


def tick() -> None:
    """One step: maybe advance a day, then maybe run the agent."""
    if settings["play"]:
        service.advance_day()
    if settings["autopilot"] and agent.needs_action():
        agent.run_agent()


async def loop() -> None:
    """Run tick() forever at the chosen interval."""
    while True:
        await asyncio.sleep(settings["interval_seconds"])
        try:
            await asyncio.to_thread(tick)
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("autopilot tick failed")

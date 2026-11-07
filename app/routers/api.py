"""HTTP endpoints. Logic lives in app/service.py."""
from fastapi import APIRouter, HTTPException

from app import agent, autopilot, service
from app.models import AutopilotUpdate, MandateUpdate

router = APIRouter()


@router.get("/state")
def read_state() -> dict:
    """Inventory, mandate, approval queue, purchases and audit log."""
    return {**service.get_state(), "autopilot": autopilot.get_settings()}


@router.post("/mandate")
def change_mandate(update: MandateUpdate) -> dict:
    """Update spending rules or flip the kill switch."""
    return service.update_mandate(update).model_dump()


@router.post("/advance-day")
def advance_day() -> list[dict]:
    """Simulate one day of sales."""
    return service.advance_day()


@router.post("/agent/run")
def run_agent() -> dict:
    """Run the restocking agent once."""
    return agent.run_agent()


@router.post("/purchases/{purchase_id}/approve")
def approve_purchase(purchase_id: int) -> dict:
    """Approve a held order."""
    try:
        return service.approve(purchase_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/purchases/{purchase_id}/deny")
def deny_purchase(purchase_id: int) -> dict:
    """Deny a held order."""
    try:
        return service.deny(purchase_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reset")
def reset() -> dict:
    """Reload the demo data (handy for judges)."""
    service.reset_demo()
    return {"status": "reset"}


@router.post("/autopilot")
def change_autopilot(update: AutopilotUpdate) -> dict:
    """Turn play-time and autopilot on or off, or change the speed."""
    return autopilot.update(**update.model_dump(exclude_unset=True))

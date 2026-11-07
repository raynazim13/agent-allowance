"""Pydantic models shared across the app."""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Decision(str, Enum):
    """Outcome of the policy engine for a proposed order."""

    AUTO = "AUTO"
    HOLD = "HOLD"
    BLOCK = "BLOCK"


class Mandate(BaseModel):
    """Spending rules set by the business owner."""

    active: bool = True
    monthly_budget: float
    spent_this_month: float = 0.0
    per_order_limit: float
    approval_threshold: float
    approved_suppliers: list[str]
    allowed_categories: list[str]
    max_price_spike_pct: float


class MandateUpdate(BaseModel):
    """Partial update of the mandate (only sent fields change)."""

    active: Optional[bool] = None
    monthly_budget: Optional[float] = None
    per_order_limit: Optional[float] = None
    approval_threshold: Optional[float] = None
    approved_suppliers: Optional[list[str]] = None
    allowed_categories: Optional[list[str]] = None
    max_price_spike_pct: Optional[float] = None


class Order(BaseModel):
    """A proposed purchase from one supplier."""

    sku: str
    supplier_id: str
    qty: int = Field(gt=0)
    unit_price: float = Field(gt=0)
    category: str
    reason: str = ""

    @property
    def total(self) -> float:
        """Total cost of the order."""
        return round(self.qty * self.unit_price, 2)


class AutopilotUpdate(BaseModel):
    """Partial update of the automation settings."""

    play: Optional[bool] = None
    autopilot: Optional[bool] = None
    interval_seconds: Optional[float] = None

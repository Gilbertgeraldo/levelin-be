import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.common import QuotaOut


class PlanOption(BaseModel):
    code: str
    name: str
    price: int
    mission_quota: int
    days: int


class ActiveSubscription(BaseModel):
    plan: str
    status: str
    period_start: datetime
    period_end: datetime


class SubscriptionOut(BaseModel):
    quota: QuotaOut
    active: ActiveSubscription | None
    available_plans: list[PlanOption]


class CheckoutIn(BaseModel):
    plan: Literal["plus", "paket_jalur"]


class CheckoutOut(BaseModel):
    payment_id: uuid.UUID
    order_id: str
    plan: str
    amount: int
    currency: str
    snap_token: str | None
    redirect_url: str | None

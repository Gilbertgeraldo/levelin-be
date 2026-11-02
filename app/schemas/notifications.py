import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DeviceTokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=500)
    platform: Literal["android", "ios", "web"]


class DeviceTokenOut(BaseModel):
    id: uuid.UUID
    platform: str
    created_at: datetime


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str
    data: dict
    read_at: datetime | None
    created_at: datetime

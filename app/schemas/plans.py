import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Direction = Literal["upskill", "pivot"]


class PlanCreate(BaseModel):
    audit_id: uuid.UUID
    direction: Direction
    target_occupation_code: str | None = None


class PlanPatch(BaseModel):
    direction: Direction | None = None
    target_occupation_code: str | None = None


class PlanItem(BaseModel):
    type: Literal["skill", "mission", "action"]
    ref_id: str
    title: str
    done: bool


class PlanPhase(BaseModel):
    key: str
    title: str
    day_start: int
    day_end: int
    items: list[PlanItem]
    progress: float


class PlanTarget(BaseModel):
    code: str
    title: str


class PlanOut(BaseModel):
    id: uuid.UUID
    audit_id: uuid.UUID | None
    direction: str
    direction_label: str
    target_occupation: PlanTarget | None
    status: str
    skill_gap_ids: list[int]
    phases: list[PlanPhase]
    progress: float
    created_at: datetime
    updated_at: datetime

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Direction = Literal["perkuat", "naik", "pindah"]


class PlanCreate(BaseModel):
    audit_id: uuid.UUID
    direction: Direction


class PlanPatch(BaseModel):
    direction: Direction


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


class TargetTask(BaseModel):
    description: str
    ilo_score: float


class OccupationBrief(BaseModel):
    code: str
    name: str
    mean: float | None
    gradient_label: str
    tasks: list[TargetTask] | None = None


class NeededSkill(BaseModel):
    id: int
    code: str | None
    label: str
    proven: bool


class PlanOut(BaseModel):
    id: uuid.UUID
    audit_id: uuid.UUID | None
    direction: str
    direction_label: str
    current_occupation: OccupationBrief | None
    target_occupation: OccupationBrief | None
    needed_skills: list[NeededSkill]
    gap_skills: list[NeededSkill]
    skill_gap_ids: list[int]
    status: str
    phases: list[PlanPhase]
    progress: float
    readiness_score: int | None
    created_at: datetime
    updated_at: datetime

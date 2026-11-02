"""Kontrak audit, mengikuti payload v0.1 (API_ENDPOINTS.md)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Group = Literal["auto", "assist", "human", "none"]
ManualGroup = Literal["auto", "assist", "human"]


class TemplateTaskIn(BaseModel):
    ilo_task_id: str = Field(description="Id tugas ILO, mis. '4311-1' dari /v1/occupations/{code}/tasks")
    hours_per_week: float = Field(ge=0, le=80)


class AuditCreate(BaseModel):
    template_tasks: list[TemplateTaskIn] = Field(default_factory=list, max_length=30)
    story: str | None = Field(None, max_length=5000, description="Cerita kerja bebas dari layar L2")
    occupation_code: str | None = Field(None, description="Default: jabatan di profil")


class AuditTaskPatch(BaseModel):
    """Kirim hanya kolom yang diubah. ilo_task_id: null = lepas padanan ILO."""

    name: str | None = Field(None, min_length=1, max_length=300)
    hours_per_week: float | None = Field(None, ge=0, le=80)
    uses_ai: bool | None = None
    ilo_task_id: str | None = None
    group: ManualGroup | None = Field(None, description="Kelompok pilihan sendiri untuk tugas tanpa padanan ILO")


class AuditTaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    hours_per_week: float = Field(ge=0, le=80)
    ilo_task_id: str | None = None
    group: ManualGroup | None = None
    uses_ai: bool = False


class AuditTaskOut(BaseModel):
    id: uuid.UUID
    name: str
    hours_per_week: float
    ilo_task_id: str | None
    ilo_text: str | None
    ilo_score: float | None
    group: Group
    confidence: str | None
    source: str
    uses_ai: bool


class AuditSummary(BaseModel):
    readiness_score: int
    hours_by_group: dict[str, float]
    exposed_hours: float
    hours_saved: float
    save_factor: dict[str, float]


class OccupationName(BaseModel):
    code: str
    name: str


class AuditOut(BaseModel):
    id: uuid.UUID
    occupation: OccupationName
    story: str | None
    tasks: list[AuditTaskOut]
    summary: AuditSummary
    engine: str
    created_at: datetime
    updated_at: datetime


class AuditListItem(BaseModel):
    id: uuid.UUID
    occupation: OccupationName
    readiness_score: int
    hours_saved: float
    exposed_hours: float
    task_count: int
    engine: str
    created_at: datetime


class ShareCardOut(BaseModel):
    readiness_score: int
    occupation_title: str
    hours_saved: float
    share: dict[str, int]
    top_ai_task: str | None
    top_human_task: str | None
    headline: str
    share_text: str
    created_at: datetime

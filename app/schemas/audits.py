import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import OccupationRef


class AuditTaskIn(BaseModel):
    template_task_id: int | None = Field(None, description="Id dari /v1/occupations/{code}/tasks")
    description: str | None = Field(None, max_length=300)
    hours_per_week: float | None = Field(None, ge=0, le=80)
    uses_ai: bool = False

    @model_validator(mode="after")
    def _need_template_or_text(self):
        if self.template_task_id is None and not (self.description or "").strip():
            raise ValueError("Isi template_task_id atau description")
        return self


class AuditCreate(BaseModel):
    occupation_code: str
    story: str | None = Field(None, max_length=5000, description="Cerita kerja bebas dari layar L2")
    tasks: list[AuditTaskIn] = Field(default_factory=list, max_length=30)


class AuditTaskPatch(BaseModel):
    """Kirim hanya kolom yang diubah. template_task_id: null = lepas kecocokan ILO."""

    description: str | None = Field(None, min_length=2, max_length=300)
    hours_per_week: float | None = Field(None, ge=0, le=80)
    uses_ai: bool | None = None
    template_task_id: int | None = None


class AuditTaskCreate(BaseModel):
    description: str = Field(min_length=2, max_length=300)
    hours_per_week: float = Field(ge=0, le=80)
    uses_ai: bool = False
    template_task_id: int | None = None


class TemplateRef(BaseModel):
    id: int
    description: str


class AuditTaskOut(BaseModel):
    id: uuid.UUID
    position: int
    description: str
    hours_per_week: float
    uses_ai: bool
    ilo_score: float
    group: str
    group_label: str
    template_task: TemplateRef | None
    match_similarity: float | None
    match_overridden: bool
    source: str


class GroupStat(BaseModel):
    label: str
    task_count: int
    hours: float
    share: float


class AuditSummary(BaseModel):
    total_hours: float
    exposure_index: float
    groups: dict[str, GroupStat]
    ai_adoption: float
    hours_saved_week: float
    hours_saved_now: float
    readiness_score: int
    readiness_components: dict[str, float]
    readiness_weights: dict[str, float]
    thresholds: dict[str, float]


class AuditOut(BaseModel):
    id: uuid.UUID
    occupation: OccupationRef
    story: str | None
    engine: str
    engine_detail: dict
    summary: AuditSummary
    tasks: list[AuditTaskOut]
    created_at: datetime
    updated_at: datetime


class AuditListItem(BaseModel):
    id: uuid.UUID
    occupation: OccupationRef
    readiness_score: int
    exposure_index: float
    hours_saved_week: float
    total_hours: float
    task_count: int
    engine: str
    created_at: datetime


class ShareCardOut(BaseModel):
    readiness_score: int
    occupation_title: str
    hours_saved_week: float
    ai_share: float
    human_share: float
    top_ai_task: str | None
    top_human_task: str | None
    headline: str
    share_text: str
    created_at: datetime

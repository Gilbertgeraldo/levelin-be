import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import QuotaOut, SkillRef


class AnswerField(BaseModel):
    id: str
    label: str
    type: str = "text"  # text | long_text | number | choice
    options: list[str] | None = None
    required: bool = True
    placeholder: str | None = None
    rows: int | None = None


class RubricItemPublic(BaseModel):
    id: str
    criterion: str
    description: str | None = None


class BossPersona(BaseModel):
    name: str | None = None
    role: str | None = None


class MissionListItem(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    summary: str | None
    kind: str | None
    duration_label: str | None
    est_minutes: int
    skills: list[SkillRef]
    status: str
    is_recommended: bool
    recommend_reason: str | None
    is_custom: bool


class MissionDetail(MissionListItem):
    """Detail misi untuk Ruang Misi. Tidak pernah memuat kunci jawaban."""

    brief: str
    boss_persona: BossPersona
    simulation_data: dict = Field(description='{"table": {"columns", "rows"}} atau {"notes": "..."}')
    answer_fields: list[AnswerField]
    rubric: list[RubricItemPublic]
    checks: list[str] = Field(description="Hal yang dicek otomatis saat dikumpulkan (tanpa jawabannya)")


class AttemptOut(BaseModel):
    id: uuid.UUID
    mission_id: uuid.UUID
    status: str
    draft: dict
    created: bool
    created_at: datetime
    quota: QuotaOut


class ChatIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ChatMessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    flagged: bool
    created_at: datetime


class AnswersIn(BaseModel):
    answers: dict[str, Any]


class SubmitIn(AnswersIn):
    ai_usage: str | None = Field(None, max_length=2000, description="Cara memakai AI saat mengerjakan misi")


class DraftOut(BaseModel):
    attempt_id: uuid.UUID
    draft: dict
    updated_at: datetime


class CheckOut(BaseModel):
    label: str
    ok: bool
    hint: str | None


class RubricScoreOut(BaseModel):
    criterion: str
    score: int
    note: str


class MissionRef(BaseModel):
    id: uuid.UUID
    title: str


class ResultOut(BaseModel):
    """Nota penilaian sesuai payload v0.1 POST /v1/attempts/{id}/submit."""

    attempt_id: uuid.UUID
    mission: MissionRef
    status: str
    passed: bool | None
    checks: list[CheckOut]
    rubric: list[RubricScoreOut] | None
    rubric_avg: float | None
    feedback: str | None
    next_step: str | None
    passport_evidence_id: str | None
    readiness_score: int | None
    skills_earned: list[SkillRef]
    engine: str | None
    review_status: str
    submitted_at: datetime | None


class AppealIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


class AppealOut(BaseModel):
    review_id: uuid.UUID
    status: str

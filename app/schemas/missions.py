import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import QuotaOut, SkillRef


class AnswerField(BaseModel):
    id: str
    label: str
    type: str = "text"
    options: list[str] | None = None
    required: bool = True
    placeholder: str | None = None


class RubricItemPublic(BaseModel):
    id: str
    criterion: str
    description: str | None = None
    weight: float = 1


class BossPersona(BaseModel):
    name: str | None = None
    role: str | None = None


class MissionListItem(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    summary: str | None
    difficulty: int
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
    simulation_data: dict
    answer_fields: list[AnswerField]
    rubric: list[RubricItemPublic]
    pass_score: int


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


class DraftOut(BaseModel):
    attempt_id: uuid.UUID
    draft: dict
    updated_at: datetime


class ObjectiveCheckOut(BaseModel):
    id: str
    field_id: str
    label: str
    passed: bool
    feedback: str


class ObjectiveOut(BaseModel):
    score: float | None
    checks: list[ObjectiveCheckOut]


class RubricCriterionOut(BaseModel):
    id: str
    criterion: str
    weight: float
    level: int
    max_level: int
    feedback: str


class RubricOut(BaseModel):
    score: float
    criteria: list[RubricCriterionOut]
    overall_feedback: str


class MissionRef(BaseModel):
    id: uuid.UUID
    title: str


class ResultOut(BaseModel):
    attempt_id: uuid.UUID
    mission: MissionRef
    status: str
    score: float | None
    passed: bool | None
    pass_score: int
    objective: ObjectiveOut | None
    rubric: RubricOut | None
    skills_earned: list[SkillRef]
    engine: str | None
    review_status: str
    submitted_at: datetime | None


class AppealIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


class AppealOut(BaseModel):
    review_id: uuid.UUID
    status: str

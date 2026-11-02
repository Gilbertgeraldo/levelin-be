import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import SkillRef

CHECK_TYPES = ("exact", "number", "contains", "regex", "choice")
FIELD_TYPES = ("text", "long_text", "number", "choice")


class ReviewAttempt(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    score: float | None
    objective_score: float | None
    rubric_score: float | None
    passed: bool | None
    engine: str | None
    answers: dict | None
    result: dict | None
    submitted_at: datetime | None


class ReviewMission(BaseModel):
    id: uuid.UUID
    title: str
    pass_score: int
    rubric: list[dict]
    answer_key: list[dict]


class ReviewOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    reason: str | None
    decision: str | None
    original_score: float | None
    new_score: float | None
    note: str | None
    created_at: datetime
    resolved_at: datetime | None
    attempt: ReviewAttempt
    mission: ReviewMission


class ReviewDecisionIn(BaseModel):
    decision: Literal["approve", "override"]
    new_score: float | None = Field(None, ge=0, le=100)
    note: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _score_for_override(self):
        if self.decision == "override" and self.new_score is None:
            raise ValueError("new_score wajib diisi untuk keputusan override")
        return self


class AnswerFieldIn(BaseModel):
    id: str = Field(min_length=1, max_length=60)
    label: str
    type: Literal["text", "long_text", "number", "choice"] = "text"
    options: list[str] | None = None
    required: bool = True
    placeholder: str | None = None


class RubricItemIn(BaseModel):
    id: str = Field(min_length=1, max_length=60)
    criterion: str
    description: str | None = None
    weight: float = Field(1, gt=0)
    keywords: list[str] = Field(default_factory=list, description="Untuk penilaian cadangan tanpa LLM")


class AnswerKeyIn(BaseModel):
    id: str = Field(min_length=1, max_length=60)
    field_id: str
    type: Literal["exact", "number", "contains", "regex", "choice"]
    expected: Any
    tolerance: float | None = Field(None, ge=0)
    tolerance_pct: float | None = Field(None, ge=0)
    min_matches: int | None = Field(None, ge=1)
    weight: float = Field(1, gt=0)
    label: str | None = None
    hint: str | None = Field(None, description="Umpan balik saat salah, tanpa membocorkan jawaban")
    leak_guard: bool | None = Field(
        None, description="false bila jawaban memang tertulis di data simulasi (tidak disaring dari chat)"
    )


class MissionAdminIn(BaseModel):
    slug: str = Field(min_length=3, max_length=120, pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=3, max_length=255)
    summary: str | None = None
    brief: str = Field(min_length=10)
    boss_persona: dict = Field(default_factory=dict)
    simulation_data: dict = Field(default_factory=dict)
    answer_fields: list[AnswerFieldIn] = Field(min_length=1)
    rubric: list[RubricItemIn] = Field(min_length=1)
    answer_key: list[AnswerKeyIn] = Field(default_factory=list)
    occupation_isco_codes: list[str] = Field(default_factory=list)
    skill_ids: list[int] = Field(default_factory=list)
    difficulty: int = Field(1, ge=1, le=3)
    est_minutes: int = Field(30, ge=5, le=480)
    pass_score: int = Field(70, ge=0, le=100)
    objective_weight: float = Field(0.4, ge=0, le=1)
    is_published: bool = True
    org_id: uuid.UUID | None = None
    custom_request_id: uuid.UUID | None = Field(
        None, description="Tautkan ke pesanan misi kustom HR; org_id ikut organisasi pemesan"
    )

    @model_validator(mode="after")
    def _consistent(self):
        field_ids = [f.id for f in self.answer_fields]
        if len(set(field_ids)) != len(field_ids):
            raise ValueError("id answer_fields harus unik")
        if len({r.id for r in self.rubric}) != len(self.rubric):
            raise ValueError("id rubric harus unik")
        if len({k.id for k in self.answer_key}) != len(self.answer_key):
            raise ValueError("id answer_key harus unik")
        for key in self.answer_key:
            if key.field_id not in field_ids:
                raise ValueError(f"answer_key {key.id} merujuk kolom yang tidak ada: {key.field_id}")
        return self


class MissionAdminOut(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    summary: str | None
    brief: str
    boss_persona: dict
    simulation_data: dict
    answer_fields: list[dict]
    rubric: list[dict]
    answer_key: list[dict]
    occupation_isco_codes: list[str]
    skills: list[SkillRef]
    difficulty: int
    est_minutes: int
    pass_score: int
    objective_weight: float
    is_published: bool
    org_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

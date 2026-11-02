import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import SharingOut


class PassportSkillRef(BaseModel):
    id: int
    code: str | None = None
    label: str
    category: str


class Evidence(BaseModel):
    id: uuid.UUID
    mission_id: uuid.UUID
    mission_title: str
    attempt_id: uuid.UUID
    earned_at: datetime
    rubric: list[str]
    rubric_avg: float | None = None
    checks_passed: int | None = None
    checks_total: int | None = None
    excerpt: str | None = None
    score: float | None = None


class PassportSkill(BaseModel):
    skill: PassportSkillRef
    evidence_count: int
    best_score: float | None = None
    last_earned_at: datetime
    evidence: list[Evidence]


class PassportOut(BaseModel):
    name: str
    total_skills: int
    skills: list[PassportSkill]
    sharing: SharingOut


class SharingIn(BaseModel):
    share_with_org: bool | None = Field(None, description="Bagikan Paspor ke HR dan atasan tim di organisasimu")
    public_link: bool | None = None


class PublicPassportOut(BaseModel):
    name: str
    total_skills: int
    skills: list[PassportSkill]
    verified_by: str = "Levelin"
    generated_at: datetime

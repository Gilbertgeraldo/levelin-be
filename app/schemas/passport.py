import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import SharingOut


class PassportSkillRef(BaseModel):
    id: int
    label: str
    category: str


class Evidence(BaseModel):
    mission_id: uuid.UUID
    mission_title: str
    attempt_id: uuid.UUID
    earned_at: datetime
    rubric: list[str]
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
    share_with_org: bool | None = None
    public_link: bool | None = None


class PublicPassportOut(BaseModel):
    name: str
    total_skills: int
    skills: list[PassportSkill]
    verified_by: str = "Levelin"
    generated_at: datetime

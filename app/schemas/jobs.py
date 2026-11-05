import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import SkillRef


class JobOut(BaseModel):
    id: uuid.UUID
    title: str
    company: str
    location: str | None
    employment_type: str | None
    description: str | None
    url: str | None
    is_internal: bool
    source: str
    source_verified: bool
    warning: str | None
    occupation_code: str | None
    salary_min: int | None
    salary_max: int | None
    posted_at: datetime
    expires_at: datetime | None
    skills: list[SkillRef]
    applicant_count: int = 0


class JobMatchOut(JobOut):
    match_percent: int
    matched_skills: list[SkillRef]
    partial_skills: list[SkillRef]
    missing_skills: list[SkillRef]


class ApplyIn(BaseModel):
    cover_note: str | None = Field(None, max_length=3000)


class ApplicationOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    status: str
    passport_url: str | None
    created_at: datetime


class MyApplicationOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    job_title: str
    company: str
    location: str | None
    employment_type: str | None
    is_internal: bool
    status: str
    cover_note: str | None
    passport_url: str | None
    created_at: datetime

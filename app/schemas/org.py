import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import SkillRef


class GroupStats(BaseModel):
    people: int | None
    audited_people: int | None
    avg_readiness: float | None
    avg_exposure: float | None
    avg_hours_saved_week: float | None
    missions_passed: int | None
    suppressed: bool


class Overview(GroupStats):
    members: int
    audits_total: int
    mission_attempts: int
    candidates_sharing: int
    pending_invitations: int


class DepartmentStats(GroupStats):
    department: str


class TopSkill(BaseModel):
    skill_id: int
    label: str
    people: int


class DashboardOut(BaseModel):
    overview: Overview
    departments: list[DepartmentStats]
    top_proven_skills: list[TopSkill]
    k_anonymity_min: int
    generated_at: datetime


class CandidateOut(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str | None
    department: str | None
    proven_skills: list[SkillRef]
    skill_count: int
    passport_url: str | None
    last_active_at: datetime | None


class Invitee(BaseModel):
    email: str = Field(max_length=320)
    full_name: str | None = Field(None, max_length=200)
    department: str | None = Field(None, max_length=120)
    role: Literal["worker", "hr_admin"] = "worker"


class InvitationIn(BaseModel):
    """Isi salah satu: `emails` (dengan department/role bersama) atau `invitees` per orang."""

    emails: list[str] = Field(default_factory=list, max_length=500)
    invitees: list[Invitee] = Field(default_factory=list, max_length=500)
    department: str | None = Field(None, max_length=120)
    role: Literal["worker", "hr_admin"] = "worker"


class InvitationCreated(BaseModel):
    id: uuid.UUID
    email: str
    department: str | None
    role: str
    email_sent: bool


class InvitationSkipped(BaseModel):
    email: str
    reason: str


class InvitationResult(BaseModel):
    created: list[InvitationCreated]
    skipped: list[InvitationSkipped]


class InvitationOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    department: str | None
    role: str
    status: str
    created_at: datetime
    expires_at: datetime | None
    accepted_at: datetime | None


class CustomMissionOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    target_role: str | None
    file_name: str | None
    file_size: int | None
    status: str
    mission_id: uuid.UUID | None
    created_at: datetime


class OrgJobIn(BaseModel):
    title: str = Field(min_length=3, max_length=255)
    description: str | None = Field(None, max_length=10_000)
    location: str | None = Field(None, max_length=200)
    employment_type: str | None = Field(None, max_length=40)
    url: str | None = Field(None, max_length=500)
    occupation_code: str | None = None
    skill_ids: list[int] = Field(default_factory=list, max_length=50)
    salary_min: int | None = Field(None, ge=0)
    salary_max: int | None = Field(None, ge=0)
    expires_at: datetime | None = None


class ProgressRow(BaseModel):
    month: str
    active_people: int | None
    audited_people: int | None
    avg_readiness: float | None
    missions_passed: int | None
    suppressed: bool


class ProgressReportOut(BaseModel):
    organization: str
    k_anonymity_min: int
    rows: list[ProgressRow]
    generated_at: datetime

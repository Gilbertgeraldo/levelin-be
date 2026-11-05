import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import SkillRef

OrgRole = Literal["worker", "manager", "hr_admin"]


class GroupShare(BaseModel):
    auto: int
    assist: int
    human: int


class DashboardOrg(BaseModel):
    name: str
    invited: int
    audited: int


class DepartmentStats(BaseModel):
    name: str
    headcount: int
    share: GroupShare
    readiness_avg: int | None


class TrendPoint(BaseModel):
    month: str
    readiness_avg: int | None


class DashboardOut(BaseModel):
    """Payload v0.1 GET /v1/org/dashboard; juga dipakai GET /v1/team/dashboard (tanpa departemen).

    Angka bernilai null bila orang yang sudah audit kurang dari k_anonymity_min.
    """

    org: DashboardOrg
    headcount: int
    readiness_avg: int | None
    hours_saved_per_week: int | None
    share: GroupShare | None
    missions_passed_this_month: int | None
    internal_candidates: int
    departments: list[DepartmentStats]
    trend: list[TrendPoint]
    suppressed_departments: int
    suppressed: bool
    insight: str | None
    k_anonymity_min: int
    generated_at: datetime


class CandidateOut(BaseModel):
    user_id: uuid.UUID
    name: str
    initials: str
    email: str | None
    department: str | None
    current_occupation: str | None
    target: str
    evidence_count: int
    proven_skills: list[SkillRef]
    skill_count: int
    passport_url: str | None
    last_active_at: datetime | None


class MemberOut(BaseModel):
    """Data anggota tanpa hasil audit: hanya identitas, peran, dan departemen."""

    user_id: uuid.UUID
    name: str
    email: str | None
    department: str | None
    role: str
    manager_names: list[str]
    last_active_at: datetime | None
    is_online: bool = False


class MemberPatch(BaseModel):
    role: OrgRole | None = None
    department: str | None = Field(None, max_length=120)


class Invitee(BaseModel):
    email: str = Field(max_length=320)
    full_name: str | None = Field(None, max_length=200)
    department: str | None = Field(None, max_length=120)
    role: OrgRole = "worker"


class InvitationIn(BaseModel):
    """Isi salah satu: `emails` (dengan department/role bersama) atau `invitees` per orang."""

    emails: list[str] = Field(default_factory=list, max_length=500)
    invitees: list[Invitee] = Field(default_factory=list, max_length=500)
    department: str | None = Field(None, max_length=120)
    role: OrgRole = "worker"


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


class JobApplicantOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    email: str | None
    department: str | None
    cover_note: str | None
    passport_url: str | None
    status: str
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
    avg_readiness: int | None
    missions_passed: int | None
    suppressed: bool


class ProgressReportOut(BaseModel):
    organization: str
    k_anonymity_min: int
    rows: list[ProgressRow]
    generated_at: datetime

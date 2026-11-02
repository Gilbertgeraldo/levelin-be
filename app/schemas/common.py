import uuid
from datetime import datetime

from pydantic import BaseModel


class SkillRef(BaseModel):
    id: int
    label: str


class OccupationRef(BaseModel):
    code: str
    title: str
    isco_code: str | None = None


class QuotaOut(BaseModel):
    plan: str
    mission_quota: int
    used: int
    remaining: int
    period_start: datetime
    period_end: datetime


class SharingOut(BaseModel):
    share_with_org: bool
    public_link: bool
    public_slug: str | None = None
    public_url: str | None = None


class OrgRef(BaseModel):
    id: uuid.UUID
    name: str
    slug: str

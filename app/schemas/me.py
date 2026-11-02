import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import OrgRef, QuotaOut, SharingOut

Goal = Literal["perkuat", "naik", "pindah"]


class ProfileIn(BaseModel):
    occupation_code: str = Field(description="Kode jabatan dari /v1/occupations")
    full_name: str | None = Field(None, max_length=200)
    job_title: str | None = Field(None, max_length=200, description="Nama jabatan sesuai kantor")
    industry: str | None = Field(None, max_length=120)
    experience_range: str | None = Field(None, max_length=40, description='Lama kerja, mis. "3–5 tahun"')
    years_experience: int | None = Field(None, ge=0, le=60)
    goal: Goal | None = Field(None, description="perkuat posisi sekarang, naik peran, atau pindah karier")
    consent_data_processing: bool = Field(
        description="Persetujuan pemrosesan data (UU PDP). Wajib true untuk memakai fitur audit."
    )
    consent_version: str = Field("1.0", max_length=20)


class ProfileOccupation(BaseModel):
    code: str
    title: str
    isco_code: str | None = None


class ProfileOut(BaseModel):
    occupation: ProfileOccupation | None
    job_title: str | None
    industry: str | None
    experience_range: str | None
    years_experience: int | None
    goal: str | None
    consent_data_processing: bool
    consent_version: str | None
    consented_at: datetime | None


class TeamRef(BaseModel):
    manager_id: uuid.UUID
    manager_name: str


class MeOut(BaseModel):
    id: uuid.UUID
    email: str | None
    full_name: str | None
    role: str
    department: str | None
    organization: OrgRef | None
    teams: list[TeamRef]
    profile: ProfileOut | None
    subscription: QuotaOut
    passport_sharing: SharingOut

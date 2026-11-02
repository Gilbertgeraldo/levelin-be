import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import OccupationRef, OrgRef, QuotaOut, SharingOut

Goal = Literal["naik_jabatan", "pindah_karier", "kerja_lebih_efisien", "aman_dari_perubahan", "lainnya"]


class ProfileIn(BaseModel):
    occupation_code: str = Field(description="Kode jabatan KBJI dari /v1/occupations")
    full_name: str | None = Field(None, max_length=200)
    job_title: str | None = Field(None, max_length=200, description="Nama jabatan sesuai kantor")
    industry: str | None = Field(None, max_length=120)
    years_experience: int | None = Field(None, ge=0, le=60)
    goal: Goal | None = None
    consent_data_processing: bool = Field(
        description="Persetujuan pemrosesan data (UU PDP). Wajib true untuk memakai fitur audit."
    )
    consent_version: str = Field("1.0", max_length=20)


class ProfileOut(BaseModel):
    occupation: OccupationRef | None
    job_title: str | None
    industry: str | None
    years_experience: int | None
    goal: str | None
    consent_data_processing: bool
    consent_version: str | None
    consented_at: datetime | None


class MeOut(BaseModel):
    id: uuid.UUID
    email: str | None
    full_name: str | None
    role: str
    department: str | None
    organization: OrgRef | None
    profile: ProfileOut | None
    subscription: QuotaOut
    passport_sharing: SharingOut

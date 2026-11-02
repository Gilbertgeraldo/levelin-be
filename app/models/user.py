import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk
from app.db.types import UTCDateTime

ROLES = ("worker", "hr_admin", "reviewer")


class Organization(UUIDPk, Timestamps, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    plan: Mapped[str] = mapped_column(String(32), default="b2b_pilot")
    # Kuota misi per karyawan per bulan; None = pakai default org_member_mission_quota
    mission_quota_per_user: Mapped[int | None] = mapped_column(Integer)


class User(Timestamps, Base):
    __tablename__ = "users"

    # Sama dengan id pengguna di Supabase Auth (klaim "sub")
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="worker")
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    department: Mapped[str | None] = mapped_column(String(120))
    last_active_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Profile(Timestamps, Base):
    __tablename__ = "profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    occupation_code: Mapped[str | None] = mapped_column(
        ForeignKey("occupations.code", ondelete="SET NULL")
    )
    job_title: Mapped[str | None] = mapped_column(String(200))
    industry: Mapped[str | None] = mapped_column(String(120))
    years_experience: Mapped[int | None] = mapped_column(Integer)
    goal: Mapped[str | None] = mapped_column(String(40))
    consent_data_processing: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_version: Mapped[str | None] = mapped_column(String(20))
    consented_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

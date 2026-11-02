import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPk, utcnow
from app.db.types import JSONType, UTCDateTime
from app.models.reference import Skill

mission_skills = Table(
    "mission_skills",
    Base.metadata,
    Column("mission_id", ForeignKey("missions.id", ondelete="CASCADE"), primary_key=True),
    Column("skill_id", ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True),
)


class Mission(UUIDPk, Timestamps, Base):
    __tablename__ = "missions"

    slug: Mapped[str] = mapped_column(String(120), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(Text)
    brief: Mapped[str] = mapped_column(Text)
    # {name, role, style}
    boss_persona: Mapped[dict] = mapped_column(JSONType, default=dict)
    simulation_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    # [{id, label, type: text|long_text|number|choice, options?, required}]
    answer_fields: Mapped[list] = mapped_column(JSONType, default=list)
    # [{id, criterion, description, weight, keywords?}]
    rubric: Mapped[list] = mapped_column(JSONType, default=list)
    # RAHASIA - tidak pernah dikirim ke aplikasi.
    # [{id, field_id, type: exact|number|contains|regex|choice, expected, tolerance?, weight, hint?}]
    answer_key: Mapped[list] = mapped_column(JSONType, default=list)
    occupation_isco_codes: Mapped[list] = mapped_column(JSONType, default=list)
    # Jenis pekerjaan yang dilatih misi ini (lihat task_kinds)
    kind: Mapped[str | None] = mapped_column(String(16))
    difficulty: Mapped[int] = mapped_column(Integer, default=1)  # 1 mudah, 2 sedang, 3 sulit
    est_minutes: Mapped[int] = mapped_column(Integer, default=30)
    duration_label: Mapped[str | None] = mapped_column(String(40))  # mis. "30–40 menit"
    pass_score: Mapped[int] = mapped_column(Integer, default=70)
    objective_weight: Mapped[float] = mapped_column(Float, default=0.4)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    # Misi kustom milik organisasi tertentu; None = bank misi umum
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )

    skills: Mapped[list[Skill]] = relationship(secondary=mission_skills, lazy="selectin")


class Attempt(UUIDPk, Timestamps, Base):
    __tablename__ = "attempts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("missions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="in_progress")  # in_progress | graded
    draft: Mapped[dict] = mapped_column(JSONType, default=dict)
    answers: Mapped[dict | None] = mapped_column(JSONType)
    # Penjelasan pengguna tentang cara ia memakai AI saat mengerjakan misi
    ai_usage: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    objective_score: Mapped[float | None] = mapped_column(Float)
    rubric_score: Mapped[float | None] = mapped_column(Float)
    # Rata-rata rubrik skala 1-4; None bila rubrik tidak dinilai (mode cadangan)
    rubric_avg: Mapped[float | None] = mapped_column(Float)
    next_step: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    result: Mapped[dict | None] = mapped_column(JSONType)
    engine: Mapped[str | None] = mapped_column(String(16))
    # none | sampled | appealed | reviewed
    review_status: Mapped[str] = mapped_column(String(16), default="none")


class AttemptMessage(UUIDPk, Base):
    __tablename__ = "attempt_messages"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    engine: Mapped[str | None] = mapped_column(String(16))
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Review(UUIDPk, Timestamps, Base):
    """Antrean review manusia: sampel penilaian AI dan banding pengguna."""

    __tablename__ = "reviews"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # sample | appeal
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | resolved
    decision: Mapped[str | None] = mapped_column(String(16))  # approve | override
    original_score: Mapped[float | None] = mapped_column(Float)
    new_score: Mapped[float | None] = mapped_column(Float)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

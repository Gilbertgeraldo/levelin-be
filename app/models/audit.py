import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPk, utcnow
from app.db.types import JSONType, UTCDateTime
from app.models.reference import OccupationTask


class Audit(UUIDPk, Timestamps, Base):
    __tablename__ = "audits"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    occupation_code: Mapped[str] = mapped_column(ForeignKey("occupations.code"))
    isco_code: Mapped[str] = mapped_column(String(8))
    story: Mapped[str | None] = mapped_column(Text)
    engine: Mapped[str] = mapped_column(String(16), default="llm")  # llm | fallback
    engine_detail: Mapped[dict] = mapped_column(JSONType, default=dict)
    total_hours: Mapped[float] = mapped_column(Float, default=0)
    exposure_index: Mapped[float] = mapped_column(Float, default=0)
    hours_saved_week: Mapped[float] = mapped_column(Float, default=0)
    readiness_score: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict] = mapped_column(JSONType, default=dict)

    tasks: Mapped[list["AuditTask"]] = relationship(
        back_populates="audit",
        cascade="all, delete-orphan",
        order_by="AuditTask.position",
        lazy="selectin",
    )


class AuditTask(UUIDPk, Timestamps, Base):
    __tablename__ = "audit_tasks"

    audit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("audits.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text)
    hours_per_week: Mapped[float] = mapped_column(Float, default=0)
    uses_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    template_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("occupation_tasks.id", ondelete="SET NULL")
    )
    match_similarity: Mapped[float | None] = mapped_column(Float)
    # None bila tugas tidak cocok dengan tugas ILO mana pun
    ilo_score: Mapped[float | None] = mapped_column(Float)
    group: Mapped[str] = mapped_column(String(8), default="none")  # auto | assist | human | none
    # Kelompok pilihan pengguna untuk tugas yang tidak terpetakan
    manual_group: Mapped[str | None] = mapped_column(String(8))
    confidence: Mapped[str | None] = mapped_column(String(16))  # template | tinggi | sedang | rendah
    # template | story | template+story | manual
    source: Mapped[str] = mapped_column(String(16), default="story")
    match_overridden: Mapped[bool] = mapped_column(Boolean, default=False)

    audit: Mapped[Audit] = relationship(back_populates="tasks")
    template: Mapped[OccupationTask | None] = relationship(lazy="selectin")


class ReadinessSnapshot(Base):
    """Skor Kesiapan terakhir pengguna per bulan, untuk grafik tren dashboard."""

    __tablename__ = "readiness_snapshots"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM
    score: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

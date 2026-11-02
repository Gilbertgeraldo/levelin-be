import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPk
from app.db.types import JSONType


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
    ilo_score: Mapped[float] = mapped_column(Float, default=0)
    group: Mapped[str] = mapped_column(String(8), default="low")  # high | medium | low
    source: Mapped[str] = mapped_column(String(16), default="story")  # story | template | manual
    match_overridden: Mapped[bool] = mapped_column(Boolean, default=False)

    audit: Mapped[Audit] = relationship(back_populates="tasks")

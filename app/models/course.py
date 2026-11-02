import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPk, utcnow
from app.db.types import UTCDateTime
from app.models.reference import Skill

course_skills = Table(
    "course_skills",
    Base.metadata,
    Column("course_id", ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True),
    Column("skill_id", ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True),
)


class Course(UUIDPk, Timestamps, Base):
    __tablename__ = "courses"

    title: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int] = mapped_column(Integer, default=0)  # Rupiah
    duration_hours: Mapped[float | None] = mapped_column(Float)
    level: Mapped[str | None] = mapped_column(String(20))  # pemula | menengah | lanjut
    format: Mapped[str | None] = mapped_column(String(120))  # mis. "Bootcamp online live"
    language: Mapped[str] = mapped_column(String(10), default="id")
    rating: Mapped[float | None] = mapped_column(Float)
    is_partner: Mapped[bool] = mapped_column(Boolean, default=False)
    affiliate_code: Mapped[str | None] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    skills: Mapped[list[Skill]] = relationship(secondary=course_skills, lazy="selectin")


class CourseClick(UUIDPk, Base):
    __tablename__ = "course_clicks"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    is_partner: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

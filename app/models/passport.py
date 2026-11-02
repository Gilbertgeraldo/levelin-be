import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk, utcnow
from app.db.types import UTCDateTime


class PassportEntry(UUIDPk, Base):
    """Satu bukti skill: skill X terbukti lewat percobaan misi Y."""

    __tablename__ = "passport_entries"
    __table_args__ = (UniqueConstraint("attempt_id", "skill_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"))
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attempts.id", ondelete="CASCADE"))
    mission_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("missions.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float)
    earned_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class PassportSettings(Timestamps, Base):
    __tablename__ = "passport_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    share_with_org: Mapped[bool] = mapped_column(Boolean, default=False)
    public_link: Mapped[bool] = mapped_column(Boolean, default=False)
    public_slug: Mapped[str | None] = mapped_column(String(40), unique=True)

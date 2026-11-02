import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk
from app.db.types import JSONType


class Plan(UUIDPk, Timestamps, Base):
    __tablename__ = "plans"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    audit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("audits.id", ondelete="SET NULL")
    )
    direction: Mapped[str] = mapped_column(String(16))  # upskill | pivot
    target_occupation_code: Mapped[str | None] = mapped_column(
        ForeignKey("occupations.code", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | archived
    target_skill_ids: Mapped[list] = mapped_column(JSONType, default=list)
    # [{key, title, day_start, day_end, items: [{type: skill|mission, ref_id, title}]}]
    phases: Mapped[list] = mapped_column(JSONType, default=list)

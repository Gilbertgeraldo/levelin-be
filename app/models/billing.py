import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk
from app.db.types import JSONType, UTCDateTime


class Subscription(UUIDPk, Timestamps, Base):
    __tablename__ = "subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    plan: Mapped[str] = mapped_column(String(20))  # plus | paket_jalur
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | expired | cancelled
    mission_quota: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[datetime] = mapped_column(UTCDateTime)
    period_end: Mapped[datetime] = mapped_column(UTCDateTime)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL")
    )


class Payment(UUIDPk, Timestamps, Base):
    __tablename__ = "payments"

    order_id: Mapped[str] = mapped_column(String(64), unique=True)
    # SET NULL: catatan transaksi tetap ada walau akun dihapus (kewajiban pembukuan)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    plan: Mapped[str] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="IDR")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | paid | failed | expired
    provider: Mapped[str] = mapped_column(String(20), default="midtrans")
    provider_token: Mapped[str | None] = mapped_column(String(255))
    redirect_url: Mapped[str | None] = mapped_column(String(500))
    raw_notification: Mapped[dict | None] = mapped_column(JSONType)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

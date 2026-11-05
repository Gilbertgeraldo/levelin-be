"""Rate limit fitur AI per pengguna, dihitung dari tabel ai_usage agar konsisten antar-instance."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.base import utcnow
from app.models import AIUsage

HOUR = 3600
DAY = 86400


def _limits() -> dict[str, tuple[int, int]]:
    s = get_settings()
    return {
        "audit": (s.rate_limit_audit_per_day, DAY),
        "explain": (s.rate_limit_explain_per_hour, HOUR),
        "narrative": (s.rate_limit_narrative_per_hour, HOUR),
        "chat": (s.rate_limit_chat_per_hour, HOUR),
        "grade": (s.rate_limit_grade_per_day, DAY),
        "coach": (s.rate_limit_chat_per_hour, HOUR),
    }


def _human(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} detik"
    if seconds < 2 * HOUR:
        return f"{round(seconds / 60)} menit"
    return f"{round(seconds / HOUR)} jam"


async def enforce_ai_limit(db: AsyncSession, user_id: uuid.UUID, bucket: str) -> None:
    limit, window = _limits()[bucket]
    now = utcnow()
    since = now - timedelta(seconds=window)
    where = (AIUsage.user_id == user_id, AIUsage.bucket == bucket, AIUsage.created_at >= since)
    count = await db.scalar(select(func.count()).select_from(AIUsage).where(*where))
    if (count or 0) < limit:
        return
    oldest = await db.scalar(select(func.min(AIUsage.created_at)).where(*where))
    retry_after = max(1, int((oldest + timedelta(seconds=window) - now).total_seconds())) if oldest else window
    raise ApiError(
        429,
        "rate_limited",
        f"Batas pemakaian AI untuk fitur ini sudah tercapai. Coba lagi dalam {_human(retry_after)}.",
        details={"bucket": bucket, "limit": limit, "window_seconds": window},
        headers={"Retry-After": str(retry_after)},
    )


async def begin_ai_call(db: AsyncSession, user_id: uuid.UUID, bucket: str) -> AIUsage:
    """Cek limit lalu langsung catat & commit, supaya request paralel ikut terhitung."""
    await enforce_ai_limit(db, user_id, bucket)
    usage = AIUsage(user_id=user_id, bucket=bucket)
    db.add(usage)
    await db.commit()
    return usage


def finish_ai_call(usage: AIUsage, engine: str, info) -> None:
    """Lengkapi catatan pemakaian (engine, model, token). Pemanggil yang commit."""
    usage.engine = engine
    usage.model = getattr(info, "model", None)
    usage.input_tokens = getattr(info, "input_tokens", 0) or 0
    usage.output_tokens = getattr(info, "output_tokens", 0) or 0

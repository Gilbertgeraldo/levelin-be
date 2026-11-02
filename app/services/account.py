"""Hak subjek data (UU PDP): ekspor dan hapus semua data milik pengguna."""

import logging

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.base import utcnow
from app.models import (
    AIUsage,
    Attempt,
    AttemptMessage,
    Audit,
    CourseClick,
    DeviceToken,
    JobApplication,
    Notification,
    PassportEntry,
    PassportSettings,
    Payment,
    Plan,
    Profile,
    Subscription,
    User,
)
from app.services import supabase_admin

logger = logging.getLogger("levelin.account")


def _row(obj, exclude: tuple[str, ...] = ()) -> dict:
    return {c.key: getattr(obj, c.key) for c in obj.__table__.columns if c.key not in exclude}


async def export_user_data(db: AsyncSession, user: User) -> dict:
    uid = user.id

    async def rows(model, *where):
        return (await db.scalars(select(model).where(*where))).all()

    audits = await rows(Audit, Audit.user_id == uid)
    attempts = await rows(Attempt, Attempt.user_id == uid)
    attempt_ids = [a.id for a in attempts]
    messages = await rows(AttemptMessage, AttemptMessage.attempt_id.in_(attempt_ids)) if attempt_ids else []
    profile = await db.get(Profile, uid)
    sharing = await db.get(PassportSettings, uid)
    usage = (
        await db.execute(
            select(AIUsage.bucket, func.count()).where(AIUsage.user_id == uid).group_by(AIUsage.bucket)
        )
    ).all()

    data = {
        "exported_at": utcnow(),
        "user": _row(user),
        "profile": _row(profile) if profile else None,
        "audits": [
            {**_row(a), "tasks": [_row(t) for t in a.tasks]} for a in audits
        ],
        "plans": [_row(p) for p in await rows(Plan, Plan.user_id == uid)],
        "mission_attempts": [_row(a) for a in attempts],
        "mission_messages": [_row(m) for m in messages],
        "passport_entries": [_row(e) for e in await rows(PassportEntry, PassportEntry.user_id == uid)],
        "passport_sharing": _row(sharing) if sharing else None,
        "course_clicks": [_row(c) for c in await rows(CourseClick, CourseClick.user_id == uid)],
        "job_applications": [_row(j) for j in await rows(JobApplication, JobApplication.user_id == uid)],
        "notifications": [_row(n) for n in await rows(Notification, Notification.user_id == uid)],
        "devices": [
            {"platform": d.platform, "created_at": d.created_at}
            for d in await rows(DeviceToken, DeviceToken.user_id == uid)
        ],
        "subscriptions": [_row(s) for s in await rows(Subscription, Subscription.user_id == uid)],
        "payments": [_row(p, ("raw_notification",)) for p in await rows(Payment, Payment.user_id == uid)],
        "ai_usage_counts": {bucket: n for bucket, n in usage},
    }
    return jsonable_encoder(data)


async def delete_user(db: AsyncSession, user: User) -> None:
    """Hapus akun Supabase Auth lalu semua baris milik pengguna (FK cascade).

    Bila akun Auth gagal dihapus, data lokal dibiarkan supaya pengguna bisa mengulang.
    """
    if not supabase_admin.configured():
        logger.warning("SUPABASE_SERVICE_ROLE_KEY kosong: akun Auth %s tidak ikut terhapus", user.id)
    elif not await supabase_admin.delete_auth_user(user.id):
        raise ApiError(503, "auth_delete_failed", "Akun belum bisa dihapus. Coba lagi beberapa saat.")
    await db.delete(user)
    await db.commit()

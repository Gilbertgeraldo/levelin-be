"""Kirim pengingat (jalankan harian lewat cron / scheduler).

    uv run python -m scripts.send_reminders [--dry-run]

1. Cek ulang Peta Tugas: audit terakhir lebih tua dari REMINDER_DAYS (default 90 hari).
2. Lanjutkan misi: rencana aktif masih punya misi yang belum lulus dan tidak ada
   percobaan misi dalam 7 hari terakhir.
Setiap jenis pengingat paling sering sekali per jendela waktunya.
"""

import argparse
import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models import Attempt, Audit, Notification, Plan
from app.services.notifications import notify

MISSION_GAP_DAYS = 7


async def _recently_notified(db, user_id: uuid.UUID, type_: str, since) -> bool:
    return bool(
        await db.scalar(
            select(Notification.id).where(
                Notification.user_id == user_id, Notification.type == type_, Notification.created_at >= since
            )
        )
    )


async def reaudit_reminders(db, dry_run: bool) -> int:
    days = get_settings().reminder_days
    cutoff = utcnow() - timedelta(days=days)
    rows = (
        await db.execute(
            select(Audit.user_id, func.max(Audit.created_at)).group_by(Audit.user_id).having(func.max(Audit.created_at) < cutoff)
        )
    ).all()
    sent = 0
    for user_id, _ in rows:
        if await _recently_notified(db, user_id, "reaudit_reminder", cutoff):
            continue
        sent += 1
        if not dry_run:
            await notify(
                db,
                user_id,
                "reaudit_reminder",
                "Saatnya cek ulang Peta Tugas",
                f"Sudah {days} hari sejak audit terakhirmu. Perbarui peta untuk melihat kemajuan Skor Kesiapanmu.",
                {"screen": "audit"},
            )
    return sent


async def mission_reminders(db, dry_run: bool) -> int:
    since = utcnow() - timedelta(days=MISSION_GAP_DAYS)
    plans = (await db.scalars(select(Plan).where(Plan.status == "active"))).all()
    sent = 0
    for plan in plans:
        mission_ids = [
            uuid.UUID(i["ref_id"]) for p in plan.phases or [] for i in p["items"] if i["type"] == "mission"
        ]
        if not mission_ids:
            continue
        passed = set(
            (await db.scalars(
                select(Attempt.mission_id).where(Attempt.user_id == plan.user_id, Attempt.passed.is_(True))
            )).all()
        )
        pending = [m for m in mission_ids if m not in passed]
        recent = await db.scalar(
            select(Attempt.id).where(Attempt.user_id == plan.user_id, Attempt.created_at >= since)
        )
        if not pending or recent or await _recently_notified(db, plan.user_id, "mission_reminder", since):
            continue
        sent += 1
        if not dry_run:
            await notify(
                db,
                plan.user_id,
                "mission_reminder",
                "Lanjutkan Rencana Naik Level",
                f"Masih ada {len(pending)} misi di rencanamu. Satu misi cukup 20-30 menit.",
                {"screen": "missions", "mission_id": str(pending[0])},
            )
    return sent


async def main(dry_run: bool) -> None:
    async with SessionLocal() as db:
        a = await reaudit_reminders(db, dry_run)
        b = await mission_reminders(db, dry_run)
        if not dry_run:
            await db.commit()
    print(f"Pengingat cek ulang: {a}, pengingat misi: {b}{' (dry run)' if dry_run else ''}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    asyncio.run(main(parser.parse_args().dry_run))

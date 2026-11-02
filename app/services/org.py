"""Data untuk dashboard HR. Hanya agregat: tidak ada audit per orang yang keluar dari sini.

Setiap kelompok (organisasi, departemen, bulan) baru menampilkan angka bila jumlah orang
di dalamnya minimal k_anonymity_min; kalau kurang, nilainya null dan ditandai suppressed.
"""

import csv
import io
import re
import secrets
import uuid
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.base import utcnow
from app.models import Attempt, Audit, Invitation, PassportEntry, PassportSettings, Skill, User
from app.services import supabase_admin

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
INVITE_DAYS = 14


def _k() -> int:
    return get_settings().k_anonymity_min


async def _members(db: AsyncSession, org_id: uuid.UUID) -> list[User]:
    return list(
        (await db.scalars(select(User).where(User.org_id == org_id, User.role == "worker"))).all()
    )


async def _latest_audits(db: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, Audit]:
    if not user_ids:
        return {}
    audits = (
        await db.scalars(select(Audit).where(Audit.user_id.in_(user_ids)).order_by(Audit.created_at))
    ).all()
    latest: dict[uuid.UUID, Audit] = {}
    for a in audits:
        latest[a.user_id] = a  # urut naik, jadi yang terakhir menang
    return latest


def _group_stats(people: list[uuid.UUID], latest: dict, passed_by_user: dict) -> dict:
    audited = [latest[u] for u in people if u in latest]
    suppressed = len(audited) < _k()
    return {
        "people": len(people) if len(people) >= _k() else None,
        "audited_people": len(audited) if not suppressed else None,
        "avg_readiness": round(sum(a.readiness_score for a in audited) / len(audited), 1)
        if audited and not suppressed
        else None,
        "avg_exposure": round(sum(a.exposure_index for a in audited) / len(audited), 3)
        if audited and not suppressed
        else None,
        "avg_hours_saved_week": round(sum(a.hours_saved_week for a in audited) / len(audited), 1)
        if audited and not suppressed
        else None,
        "missions_passed": sum(passed_by_user.get(u, 0) for u in people) if not suppressed else None,
        "suppressed": suppressed,
    }


def _department_stats(by_dept: dict[str, list[uuid.UUID]], latest: dict, passed_by_user: dict) -> list[dict]:
    """Departemen kecil digabung jadi "Lainnya".

    Bila "Lainnya" masih di bawah ambang, angkanya bisa dihitung dari selisih total organisasi
    dengan departemen lain. Maka departemen terkecil ikut digabung sampai selisih itu aman.
    """
    k = _k()

    def audited(people: list[uuid.UUID]) -> int:
        return sum(1 for p in people if p in latest)

    def safe_remainder(people: list[uuid.UUID]) -> bool:
        return not people or (len(people) >= k and (audited(people) == 0 or audited(people) >= k))

    visible: list[tuple[str, list[uuid.UUID]]] = []
    remainder: list[uuid.UUID] = []
    for name, people in by_dept.items():
        if len(people) >= k and audited(people) >= k:
            visible.append((name, people))
        else:
            remainder += people

    visible.sort(key=lambda group: len(group[1]))
    while visible and not safe_remainder(remainder):
        remainder += visible.pop(0)[1]

    rows = [
        {"department": name, **_group_stats(people, latest, passed_by_user)}
        for name, people in sorted(visible, key=lambda group: group[0])
    ]
    if remainder:
        rows.append({"department": "Lainnya", **_group_stats(remainder, latest, passed_by_user)})
    return rows


async def dashboard(db: AsyncSession, org_id: uuid.UUID) -> dict:
    members = await _members(db, org_id)
    ids = [m.id for m in members]
    latest = await _latest_audits(db, ids)

    passed_rows = (
        await db.execute(
            select(Attempt.user_id, func.count())
            .where(Attempt.user_id.in_(ids), Attempt.passed.is_(True))
            .group_by(Attempt.user_id)
        )
    ).all() if ids else []
    passed_by_user = {u: c for u, c in passed_rows}
    audit_count = await db.scalar(
        select(func.count()).select_from(Audit).where(Audit.user_id.in_(ids))
    ) if ids else 0
    attempts_count = await db.scalar(
        select(func.count()).select_from(Attempt).where(Attempt.user_id.in_(ids))
    ) if ids else 0
    sharing = await db.scalar(
        select(func.count())
        .select_from(PassportSettings)
        .where(PassportSettings.user_id.in_(ids), PassportSettings.share_with_org.is_(True))
    ) if ids else 0
    pending_invites = await db.scalar(
        select(func.count())
        .select_from(Invitation)
        .where(Invitation.org_id == org_id, Invitation.status == "pending")
    )

    overall = _group_stats(ids, latest, passed_by_user)
    overall.update(
        {
            "members": len(ids),
            "audits_total": audit_count or 0,
            "mission_attempts": attempts_count or 0,
            "candidates_sharing": sharing or 0,
            "pending_invitations": pending_invites or 0,
        }
    )

    by_dept: dict[str, list[uuid.UUID]] = defaultdict(list)
    for m in members:
        by_dept[m.department or "Tanpa departemen"].append(m.id)
    departments = _department_stats(by_dept, latest, passed_by_user)

    top_skills = await _top_skills(db, ids)
    return {
        "overview": overall,
        "departments": departments,
        "top_proven_skills": top_skills,
        "k_anonymity_min": _k(),
        "generated_at": utcnow(),
    }


async def _top_skills(db: AsyncSession, ids: list[uuid.UUID], limit: int = 10) -> list[dict]:
    if not ids:
        return []
    rows = (
        await db.execute(
            select(Skill.id, Skill.label, func.count(func.distinct(PassportEntry.user_id)))
            .join(PassportEntry, PassportEntry.skill_id == Skill.id)
            .where(PassportEntry.user_id.in_(ids))
            .group_by(Skill.id, Skill.label)
        )
    ).all()
    visible = [
        {"skill_id": sid, "label": label, "people": n} for sid, label, n in rows if n >= _k()
    ]
    return sorted(visible, key=lambda r: -r["people"])[:limit]


async def progress_report(db: AsyncSession, org_id: uuid.UUID, months: int = 6) -> list[dict]:
    """Agregat bulanan: orang yang aktif audit/misi, rata-rata skor kesiapan, misi lulus."""
    members = await _members(db, org_id)
    ids = [m.id for m in members]
    since = (utcnow().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    audits = (
        await db.scalars(select(Audit).where(Audit.user_id.in_(ids), Audit.created_at >= since))
    ).all() if ids else []
    attempts = (
        await db.scalars(
            select(Attempt).where(Attempt.user_id.in_(ids), Attempt.created_at >= since)
        )
    ).all() if ids else []

    buckets: dict[str, dict] = defaultdict(lambda: {"audits": {}, "attempt_users": set(), "passed": 0})
    for a in audits:
        key = a.created_at.strftime("%Y-%m")
        buckets[key]["audits"][a.user_id] = a  # audit terakhir di bulan itu
    for t in attempts:
        key = t.created_at.strftime("%Y-%m")
        buckets[key]["attempt_users"].add(t.user_id)
        if t.passed:
            buckets[key]["passed"] += 1

    rows = []
    for month in sorted(buckets):
        b = buckets[month]
        audited = list(b["audits"].values())
        active = set(b["audits"]) | b["attempt_users"]
        ok = len(active) >= _k()
        rows.append(
            {
                "month": month,
                "active_people": len(active) if ok else None,
                "audited_people": len(audited) if ok else None,
                "avg_readiness": round(sum(a.readiness_score for a in audited) / len(audited), 1)
                if ok and len(audited) >= _k()
                else None,
                "missions_passed": b["passed"] if ok else None,
                "suppressed": not ok,
            }
        )
    return rows


def report_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    fields = ["month", "active_people", "audited_people", "avg_readiness", "missions_passed", "suppressed"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: "" if r[k] is None else r[k] for k in fields})
    return buf.getvalue()


# ---------- Undangan ----------


def parse_invite_csv(content: bytes) -> list[dict]:
    """CSV berkolom email (wajib), name, department, role."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "email" not in [f.strip().lower() for f in reader.fieldnames]:
        return []
    rows = []
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        rows.append(
            {
                "email": row.get("email", ""),
                "full_name": row.get("name") or row.get("nama") or None,
                "department": row.get("department") or row.get("departemen") or None,
                "role": row.get("role") or "worker",
            }
        )
    return rows


async def create_invitations(
    db: AsyncSession, org_id: uuid.UUID, inviter: User, entries: list[dict]
) -> dict:
    created, skipped = [], []
    seen: set[str] = set()
    now = utcnow()
    for entry in entries:
        email = (entry.get("email") or "").strip().lower()
        role = entry.get("role") or "worker"
        if not EMAIL_RE.match(email):
            skipped.append({"email": email, "reason": "invalid_email"})
            continue
        if role not in ("worker", "hr_admin"):
            skipped.append({"email": email, "reason": "invalid_role"})
            continue
        if email in seen:
            skipped.append({"email": email, "reason": "duplicate"})
            continue
        seen.add(email)
        member = await db.scalar(
            select(User.id).where(func.lower(User.email) == email, User.org_id == org_id)
        )
        if member:
            skipped.append({"email": email, "reason": "already_member"})
            continue
        pending = await db.scalar(
            select(Invitation.id).where(
                Invitation.org_id == org_id,
                func.lower(Invitation.email) == email,
                Invitation.status == "pending",
                Invitation.expires_at > now,
            )
        )
        if pending:
            skipped.append({"email": email, "reason": "already_invited"})
            continue
        inv = Invitation(
            org_id=org_id,
            email=email,
            full_name=entry.get("full_name"),
            department=entry.get("department"),
            role=role,
            token=secrets.token_urlsafe(24),
            status="pending",
            invited_by=inviter.id,
            expires_at=now + timedelta(days=INVITE_DAYS),
        )
        db.add(inv)
        await db.flush()
        email_sent = await supabase_admin.invite_user_by_email(
            email, data={"invitation_token": inv.token, "full_name": inv.full_name}
        )
        created.append({"id": inv.id, "email": email, "department": inv.department, "role": role, "email_sent": email_sent})
    await db.commit()
    return {"created": created, "skipped": skipped}


async def expire_old_invitations(db: AsyncSession, org_id: uuid.UUID) -> None:
    now = utcnow()
    rows = (
        await db.scalars(
            select(Invitation).where(
                Invitation.org_id == org_id, Invitation.status == "pending", Invitation.expires_at <= now
            )
        )
    ).all()
    for inv in rows:
        inv.status = "expired"
    if rows:
        await db.commit()

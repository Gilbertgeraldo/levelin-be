"""Data untuk dashboard HR dan atasan tim. Hanya agregat: tidak ada audit per orang yang keluar dari sini.

Setiap kelompok (organisasi, tim, departemen, bulan) baru menampilkan angka bila jumlah orang yang
sudah audit di dalamnya minimal k_anonymity_min; kalau kurang, nilainya null.
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
from app.models import (
    Attempt,
    Audit,
    Invitation,
    Occupation,
    Organization,
    PassportEntry,
    PassportSettings,
    Plan,
    Profile,
    ReadinessSnapshot,
    TeamMembership,
    User,
)
from app.services import passport, scoring, supabase_admin

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
INVITE_DAYS = 14
EMPLOYEE_ROLES = ("worker", "manager")
TREND_MONTHS = 3
ONLINE_WINDOW = timedelta(seconds=60)


def is_online(last_active_at) -> bool:
    return last_active_at is not None and (utcnow() - last_active_at) <= ONLINE_WINDOW


def _k() -> int:
    return get_settings().k_anonymity_min


# ---------- Anggota ----------


async def org_members(db: AsyncSession, org_id: uuid.UUID) -> list[User]:
    """Karyawan organisasi (pekerja dan atasan tim), tanpa akun HR."""
    rows = await db.scalars(select(User).where(User.org_id == org_id, User.role.in_(EMPLOYEE_ROLES)))
    return list(rows.all())


async def team_members(db: AsyncSession, manager_id: uuid.UUID) -> list[User]:
    rows = await db.scalars(
        select(User)
        .join(TeamMembership, TeamMembership.member_id == User.id)
        .where(TeamMembership.manager_id == manager_id)
    )
    return list(rows.all())


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


def _avg(values: list[float]) -> int | None:
    return scoring.js_round(sum(values) / len(values)) if values else None


def _share(audits: list[Audit]) -> dict[str, int]:
    totals = {g: 0.0 for g in scoring.GROUPS}
    for audit in audits:
        for g, h in ((audit.summary or {}).get("hours_by_group") or {}).items():
            totals[g] = totals.get(g, 0) + h
    return scoring.share_by_group(totals)


def _visible_departments(by_dept: dict[str, list[uuid.UUID]], latest: dict) -> list[tuple[str, list[uuid.UUID]]]:
    """Departemen yang boleh tampil.

    Departemen di bawah ambang disembunyikan. Bila sisa orang yang disembunyikan masih di bawah ambang,
    angkanya bisa dihitung dari selisih total dengan departemen lain, jadi departemen terkecil ikut
    disembunyikan sampai selisih itu aman.
    """
    k = _k()

    def audited(people: list[uuid.UUID]) -> int:
        return sum(1 for p in people if p in latest)

    def safe_hidden(people: list[uuid.UUID]) -> bool:
        n = audited(people)
        return not people or (len(people) >= k and (n == 0 or n >= k))

    visible, hidden_people = [], []
    for name, people in by_dept.items():
        if len(people) >= k and audited(people) >= k:
            visible.append((name, people))
        else:
            hidden_people += people
    visible.sort(key=lambda group: len(group[1]))
    while visible and not safe_hidden(hidden_people):
        hidden_people += visible.pop(0)[1]
    return sorted(visible, key=lambda group: group[0])


def _insight(departments: list[dict]) -> str | None:
    rated = [d for d in departments if d["readiness_avg"] is not None]
    if not rated:
        return None
    best = max(rated, key=lambda d: d["readiness_avg"])
    text = f"{best['name']} paling siap ({best['readiness_avg']})."
    worst = min(rated, key=lambda d: d["readiness_avg"])
    if worst is not best:
        if worst["share"]["human"] >= 50:
            text += (
                f" {worst['name']} perlu misi baru: skornya {worst['readiness_avg']} dan sebagian besar "
                "tugasnya tetap butuh manusia, jadi misi standar admin belum relevan."
            )
        else:
            text += f" {worst['name']} perlu perhatian: skornya {worst['readiness_avg']}."
    return text


async def _trend(db: AsyncSession, ids: list[uuid.UUID], months: int = TREND_MONTHS) -> list[dict]:
    if not ids:
        return []
    rows = (await db.scalars(select(ReadinessSnapshot).where(ReadinessSnapshot.user_id.in_(ids)))).all()
    by_month: dict[str, list[int]] = defaultdict(list)
    for snap in rows:
        by_month[snap.month].append(snap.score)
    recent = sorted(by_month)[-months:]
    return [
        {"month": m, "readiness_avg": _avg(by_month[m]) if len(by_month[m]) >= _k() else None}
        for m in recent
    ]


async def aggregate(
    db: AsyncSession,
    members: list[User],
    *,
    name: str,
    invited: int,
    by_department: bool,
) -> dict:
    """Ringkasan agregat sesuai payload v0.1 GET /v1/org/dashboard (juga dipakai dashboard tim)."""
    ids = [m.id for m in members]
    latest = await _latest_audits(db, ids)
    audited = list(latest.values())
    shown = len(audited) >= _k()
    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    passed_this_month = await db.scalar(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.user_id.in_(ids), Attempt.passed.is_(True), Attempt.submitted_at >= month_start)
    ) if ids else 0
    sharing = await db.scalar(
        select(func.count())
        .select_from(PassportSettings)
        .where(PassportSettings.user_id.in_(ids), PassportSettings.share_with_org.is_(True))
    ) if ids else 0

    departments: list[dict] = []
    suppressed = 0
    if by_department:
        by_dept: dict[str, list[uuid.UUID]] = defaultdict(list)
        for m in members:
            by_dept[m.department or "Tanpa departemen"].append(m.id)
        visible = _visible_departments(by_dept, latest)
        suppressed = len(by_dept) - len(visible)
        for dept, people in visible:
            dept_audits = [latest[p] for p in people if p in latest]
            departments.append(
                {
                    "name": dept,
                    "headcount": len(people),
                    "share": _share(dept_audits),
                    "readiness_avg": _avg([a.readiness_score for a in dept_audits]),
                }
            )

    return {
        "org": {"name": name, "invited": invited, "audited": len(audited)},
        "headcount": len(ids),
        "readiness_avg": _avg([a.readiness_score for a in audited]) if shown else None,
        "hours_saved_per_week": round(sum(a.hours_saved_week for a in audited)) if shown else None,
        "share": _share(audited) if shown else None,
        "missions_passed_this_month": (passed_this_month or 0) if shown else None,
        "internal_candidates": sharing or 0,
        "departments": departments,
        "trend": await _trend(db, ids) if shown else [],
        "suppressed_departments": suppressed,
        "suppressed": not shown,
        "insight": _insight(departments),
        "k_anonymity_min": _k(),
        "generated_at": utcnow(),
    }


async def dashboard(db: AsyncSession, org_id: uuid.UUID) -> dict:
    org = await db.get(Organization, org_id)
    pending = await db.scalar(
        select(func.count()).select_from(Invitation).where(Invitation.org_id == org_id, Invitation.status == "pending")
    )
    members = await org_members(db, org_id)
    # "Diundang" = yang sudah bergabung + undangan yang belum diterima
    return await aggregate(db, members, name=org.name, invited=len(members) + (pending or 0), by_department=True)


async def team_dashboard(db: AsyncSession, manager: User) -> dict:
    pending = await db.scalar(
        select(func.count())
        .select_from(Invitation)
        .where(Invitation.manager_id == manager.id, Invitation.status == "pending")
    )
    members = await team_members(db, manager.id)
    name = f"Tim {manager.full_name}" if manager.full_name else "Timmu"
    return await aggregate(db, members, name=name, invited=len(members) + (pending or 0), by_department=False)


# ---------- Kandidat (hanya yang berbagi Paspor) ----------


async def candidates(db: AsyncSession, members: list[User], *, skill_id: int | None = None) -> list[dict]:
    ids = [m.id for m in members]
    if not ids:
        return []
    sharing = {
        s.user_id: s
        for s in (
            await db.scalars(
                select(PassportSettings).where(
                    PassportSettings.user_id.in_(ids), PassportSettings.share_with_org.is_(True)
                )
            )
        ).all()
    }
    titles = dict((await db.execute(select(Occupation.code, Occupation.title))).all())
    out = []
    for user in members:
        settings = sharing.get(user.id)
        if settings is None:
            continue
        skills = await passport.passport_skills(db, user.id, include_scores=False)
        refs = [{"id": s["skill"]["id"], "code": s["skill"].get("code"), "label": s["skill"]["label"]} for s in skills]
        if skill_id is not None and skill_id not in {r["id"] for r in refs}:
            continue
        evidence = await db.scalar(
            select(func.count(func.distinct(PassportEntry.attempt_id))).where(PassportEntry.user_id == user.id)
        )
        latest = (await _latest_audits(db, [user.id])).get(user.id)
        profile = await db.get(Profile, user.id)
        current_code = latest.occupation_code if latest else (profile.occupation_code if profile else None)
        plan = await db.scalar(
            select(Plan).where(Plan.user_id == user.id, Plan.status == "active").order_by(Plan.created_at.desc()).limit(1)
        )
        name = passport.display_name(user, public=False)
        out.append(
            {
                "user_id": user.id,
                "name": name,
                "initials": "".join(p[0] for p in name.split()[:2]).upper(),
                "email": user.email,
                "department": user.department,
                "current_occupation": titles.get(current_code) if current_code else None,
                "target": titles.get(plan.target_occupation_code)
                if plan and plan.target_occupation_code
                else "Perkuat posisi sekarang",
                "evidence_count": evidence or 0,
                "proven_skills": refs,
                "skill_count": len(refs),
                "passport_url": passport.public_url(settings.public_slug) if settings.public_link else None,
                "last_active_at": user.last_active_at,
            }
        )
    out.sort(key=lambda c: (-c["evidence_count"], c["name"]))
    return out


# ---------- Laporan ----------


async def progress_report(db: AsyncSession, org_id: uuid.UUID, months: int = 6) -> list[dict]:
    """Agregat bulanan: orang yang aktif audit/misi, rata-rata skor kesiapan, misi lulus."""
    members = await org_members(db, org_id)
    ids = [m.id for m in members]
    since = (utcnow().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    audits = (
        await db.scalars(select(Audit).where(Audit.user_id.in_(ids), Audit.created_at >= since))
    ).all() if ids else []
    attempts = (
        await db.scalars(select(Attempt).where(Attempt.user_id.in_(ids), Attempt.created_at >= since))
    ).all() if ids else []
    snapshots = (
        await db.scalars(select(ReadinessSnapshot).where(ReadinessSnapshot.user_id.in_(ids)))
    ).all() if ids else []

    buckets: dict[str, dict] = defaultdict(lambda: {"audited": set(), "attempt_users": set(), "passed": 0, "scores": []})
    for a in audits:
        buckets[a.created_at.strftime("%Y-%m")]["audited"].add(a.user_id)
    for t in attempts:
        key = t.created_at.strftime("%Y-%m")
        buckets[key]["attempt_users"].add(t.user_id)
        if t.passed:
            buckets[key]["passed"] += 1
    for s in snapshots:
        if s.month >= since.strftime("%Y-%m"):
            buckets[s.month]["scores"].append(s.score)

    rows = []
    for month in sorted(buckets):
        b = buckets[month]
        active = b["audited"] | b["attempt_users"]
        ok = len(active) >= _k()
        rows.append(
            {
                "month": month,
                "active_people": len(active) if ok else None,
                "audited_people": len(b["audited"]) if ok else None,
                "avg_readiness": _avg(b["scores"]) if ok and len(b["scores"]) >= _k() else None,
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


def invite_redirect(role: str) -> str | None:
    """HR & atasan membuat kata sandi di web dashboard; pekerja diarahkan ke aplikasi (bila deep link diisi)."""
    settings = get_settings()
    if role in ("hr_admin", "manager"):
        return f"{settings.web_url.rstrip('/')}/masuk/kata-sandi"
    return settings.app_auth_redirect_url


async def create_invitations(
    db: AsyncSession,
    org_id: uuid.UUID,
    inviter: User,
    entries: list[dict],
    *,
    manager: User | None = None,
) -> dict:
    """Undangan HR (pekerja, atasan, HR) atau undangan atasan untuk timnya sendiri (hanya pekerja)."""
    allowed = ("worker",) if manager else ("worker", "manager", "hr_admin")
    created, skipped = [], []
    seen: set[str] = set()
    now = utcnow()
    for entry in entries:
        email = (entry.get("email") or "").strip().lower()
        role = "worker" if manager else (entry.get("role") or "worker")
        if not EMAIL_RE.match(email):
            skipped.append({"email": email, "reason": "invalid_email"})
            continue
        if role not in allowed:
            skipped.append({"email": email, "reason": "invalid_role"})
            continue
        if email in seen:
            skipped.append({"email": email, "reason": "duplicate"})
            continue
        seen.add(email)
        existing = await db.scalar(select(User).where(func.lower(User.email) == email))
        if manager is None and existing is not None and existing.org_id == org_id:
            skipped.append({"email": email, "reason": "already_member"})
            continue
        if manager is not None and existing is not None:
            if existing.org_id not in (None, org_id):
                skipped.append({"email": email, "reason": "other_organization"})
                continue
            if existing.id == manager.id:
                skipped.append({"email": email, "reason": "self"})
                continue
            in_team = await db.scalar(
                select(TeamMembership.member_id).where(
                    TeamMembership.manager_id == manager.id, TeamMembership.member_id == existing.id
                )
            )
            if in_team:
                skipped.append({"email": email, "reason": "already_in_team"})
                continue
        pending = await db.scalar(
            select(Invitation.id).where(
                Invitation.org_id == org_id,
                func.lower(Invitation.email) == email,
                Invitation.status == "pending",
                Invitation.expires_at > now,
                Invitation.manager_id.is_(None) if manager is None else Invitation.manager_id == manager.id,
            )
        )
        if pending:
            skipped.append({"email": email, "reason": "already_invited"})
            continue
        inv = Invitation(
            org_id=org_id,
            email=email,
            full_name=entry.get("full_name"),
            department=entry.get("department") or (manager.department if manager else None),
            role=role,
            token=secrets.token_urlsafe(24),
            status="pending",
            invited_by=inviter.id,
            manager_id=manager.id if manager else None,
            expires_at=now + timedelta(days=INVITE_DAYS),
        )
        db.add(inv)
        await db.flush()
        # Pengguna yang sudah punya akun cukup menerima undangan saat membuka aplikasi
        email_sent = existing is None and await supabase_admin.invite_user_by_email(
            email, data={"invitation_token": inv.token, "full_name": inv.full_name}, redirect_to=invite_redirect(role)
        )
        created.append(
            {"id": inv.id, "email": email, "department": inv.department, "role": role, "email_sent": bool(email_sent)}
        )
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

"""Lowongan cocok, sama dengan prototipe: per skill yang dibutuhkan lowongan, skill terbukti (misi lulus)
bernilai 1, skill dasar bernilai 0,5. Skill dasar = skill bawaan jabatan + skill jenis tugas di Peta Tugas
(+ "ai" bila ada tugas yang sudah dikerjakan dengan AI).
"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import Job, OccupationSkill, Skill, TaskKind, User
from app.services.audits import latest_audit
from app.services.passport import proven_skill_ids
from app.services.scoring import js_round

UNVERIFIED_WARNING = (
    "Sumber lowongan ini belum terverifikasi. Rekrutmen resmi tidak pernah memungut biaya."
)


async def base_skill_ids(db: AsyncSession, user: User) -> set[int]:
    audit = await latest_audit(db, user.id)
    if audit is None:
        return set()
    by_code = {s.code: s.id for s in (await db.scalars(select(Skill).where(Skill.code.is_not(None)))).all()}
    ids = set(
        (
            await db.scalars(
                select(OccupationSkill.skill_id).where(
                    OccupationSkill.isco_code == audit.isco_code, OccupationSkill.relation == "base"
                )
            )
        ).all()
    )
    kinds = {k.code: k for k in (await db.scalars(select(TaskKind))).all()}
    for task in audit.tasks:
        kind = kinds.get(task.template.kind) if task.template and task.template.kind else None
        for code in kind.skill_codes if kind else []:
            if code != "ai" and code in by_code:
                ids.add(by_code[code])
    if any(t.uses_ai for t in audit.tasks) and "ai" in by_code:
        ids.add(by_code["ai"])
    return ids


async def matches(db: AsyncSession, user: User, *, internal_first: bool = True) -> list[dict]:
    now = utcnow()
    jobs = (
        await db.scalars(
            select(Job).where(
                Job.is_active.is_(True),
                or_(Job.expires_at.is_(None), Job.expires_at > now),
                or_(Job.org_id.is_(None), Job.org_id == user.org_id),
            )
        )
    ).all()
    earned = await proven_skill_ids(db, user.id)
    base = await base_skill_ids(db, user)

    results = []
    for job in jobs:
        points, matched, partial, missing = 0.0, [], [], []
        for skill in job.skills:
            if skill.id in earned:
                points += 1
                matched.append(skill)
            elif skill.id in base:
                points += 0.5
                partial.append(skill)
            else:
                missing.append(skill)
        pct = js_round(100 * points / len(job.skills)) if job.skills else 0
        results.append((job, pct, matched, partial, missing))

    def sort_key(row):
        job, pct, *_ = row
        internal = job.org_id is not None
        return (not internal if internal_first else 0, -pct, -job.posted_at.timestamp())

    results.sort(key=sort_key)
    return [job_view(job, pct, matched, partial, missing) for job, pct, matched, partial, missing in results]


def _refs(skills) -> list[dict]:
    return [{"id": s.id, "code": s.code, "label": s.label} for s in skills]


def job_view(job: Job, match: int | None = None, matched=None, partial=None, missing=None) -> dict:
    view = {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "employment_type": job.employment_type,
        "description": job.description,
        "url": job.url,
        "is_internal": job.org_id is not None,
        "source": job.source,
        "source_verified": job.source_verified,
        "warning": None if job.source_verified else UNVERIFIED_WARNING,
        "occupation_code": job.occupation_code,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "posted_at": job.posted_at,
        "expires_at": job.expires_at,
        "skills": _refs(job.skills),
    }
    if match is not None:
        view["match_percent"] = match
        view["matched_skills"] = _refs(matched or [])
        view["partial_skills"] = _refs(partial or [])
        view["missing_skills"] = _refs(missing or [])
    return view

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import Job, Occupation, User
from app.services.audits import latest_audit
from app.services.passport import proven_skill_ids
from app.services.plans import active_plan

SKILL_WEIGHT = 0.7
ROLE_WEIGHT = 0.3


def _role_affinity(job_isco: str | None, user_iscos: set[str]) -> float:
    if not job_isco or not user_iscos:
        return 0.0
    if job_isco in user_iscos:
        return 1.0
    if any(job_isco[:2] == i[:2] for i in user_iscos):
        return 0.5
    return 0.0


async def user_iscos(db: AsyncSession, user: User) -> set[str]:
    codes: set[str] = set()
    audit = await latest_audit(db, user.id)
    if audit:
        codes.add(audit.isco_code)
    plan = await active_plan(db, user.id)
    if plan and plan.target_occupation_code:
        target = await db.get(Occupation, plan.target_occupation_code)
        if target:
            codes.add(target.isco_code)
    return codes


async def matches(db: AsyncSession, user: User, *, internal_first: bool = True) -> list[dict]:
    now = utcnow()
    stmt = select(Job).where(
        Job.is_active.is_(True),
        or_(Job.expires_at.is_(None), Job.expires_at > now),
        or_(Job.org_id.is_(None), Job.org_id == user.org_id),
    )
    jobs = (await db.scalars(stmt)).all()
    proven = await proven_skill_ids(db, user.id)
    iscos = await user_iscos(db, user)

    results = []
    for job in jobs:
        required = {s.id: s for s in job.skills}
        matched = [s for sid, s in required.items() if sid in proven]
        missing = [s for sid, s in required.items() if sid not in proven]
        skill_part = len(matched) / len(required) if required else 0.0
        match = round(100 * (SKILL_WEIGHT * skill_part + ROLE_WEIGHT * _role_affinity(job.isco_code, iscos)))
        results.append((job, match, matched, missing))

    def sort_key(row):
        job, match, _, _ = row
        internal = job.org_id is not None
        posted: datetime = job.posted_at
        return (not internal if internal_first else 0, -match, -posted.timestamp())

    results.sort(key=sort_key)
    return [job_view(job, match, matched, missing) for job, match, matched, missing in results]


def job_view(job: Job, match: int | None = None, matched=None, missing=None) -> dict:
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
        "occupation_code": job.occupation_code,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "posted_at": job.posted_at,
        "expires_at": job.expires_at,
        "skills": [{"id": s.id, "label": s.label} for s in job.skills],
    }
    if match is not None:
        view["match_percent"] = match
        view["matched_skills"] = [{"id": s.id, "label": s.label} for s in matched or []]
        view["missing_skills"] = [{"id": s.id, "label": s.label} for s in missing or []]
    return view

"""Rekomendasi kelas, sama dengan prototipe: urut skill gap yang ditutup (terbanyak dulu), lalu harga.

Label is_partner (Mitra) hanya informasi dan tidak memengaruhi urutan.
"""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Course, Plan
from app.services.passport import proven_skill_ids

Budget = Literal["all", "free", "under_500k", "bootcamp"]
UNDER_500K = 500_000


def _in_budget(course: Course, budget: Budget) -> bool:
    if budget == "free":
        return course.price == 0
    if budget == "under_500k":
        return course.price < UNDER_500K
    if budget == "bootcamp":
        return "bootcamp" in (course.format or "").lower()
    return True


async def recommend(db: AsyncSession, plan: Plan | None, *, budget: Budget = "all", max_price: int | None = None) -> list[dict]:
    gap: set[int] = set()
    if plan is not None:
        gap = set(plan.target_skill_ids or []) - await proven_skill_ids(db, plan.user_id)

    stmt = select(Course).where(Course.is_active.is_(True))
    if max_price is not None:
        stmt = stmt.where(Course.price <= max_price)
    courses = [c for c in (await db.scalars(stmt)).all() if _in_budget(c, budget)]

    ranked = []
    for course in courses:
        covered = [s for s in course.skills if s.id in gap]
        if gap and not covered:
            continue
        ranked.append((course, covered))
    ranked.sort(key=lambda pair: (-len(pair[1]), pair[0].price, pair[0].title))
    return [course_view(course, covered, len(gap)) for course, covered in ranked]


def course_view(course: Course, covered: list | None = None, gap_size: int = 0) -> dict:
    covered = covered or []
    return {
        "id": course.id,
        "title": course.title,
        "provider": course.provider,
        "url": course.url,
        "description": course.description,
        "price": course.price,
        "is_free": course.price == 0,
        "format": course.format,
        "duration_hours": course.duration_hours,
        "level": course.level,
        "language": course.language,
        "rating": course.rating,
        "is_partner": course.is_partner,
        "skills": [{"id": s.id, "code": s.code, "label": s.label} for s in course.skills],
        "gap_skills_covered": [{"id": s.id, "code": s.code, "label": s.label} for s in covered],
        "gap_covered_count": len(covered),
        "reason": (
            f"Kelas ini menutup {len(covered)} dari {gap_size} skill yang kamu butuhkan" if covered else None
        ),
    }


def outbound_url(course: Course) -> str:
    """URL keluar dengan penanda UTM (dan kode afiliasi untuk mitra) untuk pelacakan komisi."""
    sep = "&" if "?" in course.url else "?"
    url = f"{course.url}{sep}utm_source=levelin&utm_medium=app&utm_campaign=kelas"
    if course.is_partner and course.affiliate_code:
        url += f"&ref={course.affiliate_code}"
    return url

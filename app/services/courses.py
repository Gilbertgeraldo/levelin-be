from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Course, Plan
from app.services.passport import proven_skill_ids


async def recommend(
    db: AsyncSession,
    plan: Plan | None,
    *,
    budget: str = "all",
    max_price: int | None = None,
) -> list[dict]:
    """Urutkan kelas: skill gap yang terbayar (paling banyak dulu), lalu harga termurah.

    Label is_partner hanya informasi; tidak memengaruhi urutan.
    """
    gap: set[int] = set()
    if plan is not None:
        gap = set(plan.target_skill_ids or []) - await proven_skill_ids(db, plan.user_id)

    stmt = select(Course).where(Course.is_active.is_(True))
    if budget == "free":
        stmt = stmt.where(Course.price == 0)
    elif budget == "paid":
        stmt = stmt.where(Course.price > 0)
    if max_price is not None:
        stmt = stmt.where(Course.price <= max_price)
    courses = (await db.scalars(stmt)).all()

    ranked = []
    for course in courses:
        covered = [s for s in course.skills if s.id in gap]
        if gap and not covered:
            continue
        ranked.append((course, covered))
    ranked.sort(key=lambda pair: (-len(pair[1]), pair[0].price, -(pair[0].rating or 0), pair[0].title))
    return [course_view(course, covered) for course, covered in ranked]


def course_view(course: Course, covered: list | None = None) -> dict:
    covered = covered or []
    return {
        "id": course.id,
        "title": course.title,
        "provider": course.provider,
        "url": course.url,
        "description": course.description,
        "price": course.price,
        "is_free": course.price == 0,
        "duration_hours": course.duration_hours,
        "level": course.level,
        "language": course.language,
        "rating": course.rating,
        "is_partner": course.is_partner,
        "skills": [{"id": s.id, "label": s.label} for s in course.skills],
        "gap_skills_covered": [{"id": s.id, "label": s.label} for s in covered],
        "gap_covered_count": len(covered),
    }


def outbound_url(course: Course) -> str:
    """URL keluar dengan penanda UTM (dan kode afiliasi untuk mitra) untuk pelacakan komisi."""
    sep = "&" if "?" in course.url else "?"
    url = f"{course.url}{sep}utm_source=levelin&utm_medium=app&utm_campaign=kelas"
    if course.is_partner and course.affiliate_code:
        url += f"&ref={course.affiliate_code}"
    return url

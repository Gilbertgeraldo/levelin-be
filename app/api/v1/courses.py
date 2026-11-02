import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import DB, CurrentUser, Pagination
from app.core.errors import not_found
from app.core.pagination import Page, paginate_list
from app.models import Course, CourseClick
from app.schemas.courses import CourseClickOut, CourseOut
from app.services import courses
from app.services.passport import proven_skill_ids
from app.services.plans import active_plan, get_owned_plan

router = APIRouter(prefix="/courses", tags=["Kelas"])


@router.get(
    "/recommendations",
    response_model=Page[CourseOut],
    summary="Daftar kelas: urut skill gap terbayar lalu harga; label is_partner",
)
async def recommendations(
    db: DB,
    user: CurrentUser,
    page: Pagination,
    plan_id: uuid.UUID | None = None,
    budget: Literal["all", "free", "paid"] = "all",
    max_price: Annotated[int | None, Query(ge=0)] = None,
):
    plan = await get_owned_plan(db, user, plan_id) if plan_id else await active_plan(db, user.id)
    ranked = await courses.recommend(db, plan, budget=budget, max_price=max_price)
    items, meta = paginate_list(ranked, page)
    return {"data": items, "meta": meta}


async def _course(db, course_id: uuid.UUID) -> Course:
    course = await db.get(Course, course_id)
    if course is None or not course.is_active:
        raise not_found("Kelas")
    return course


@router.get("/{course_id}", response_model=CourseOut, summary="Detail kelas")
async def get_course(course_id: uuid.UUID, db: DB, user: CurrentUser):
    course = await _course(db, course_id)
    plan = await active_plan(db, user.id)
    gap = set(plan.target_skill_ids or []) - await proven_skill_ids(db, user.id) if plan else set()
    return courses.course_view(course, [s for s in course.skills if s.id in gap])


@router.post("/{course_id}/click", response_model=CourseClickOut, summary="Catat klik keluar (untuk komisi mitra)")
async def click_course(course_id: uuid.UUID, db: DB, user: CurrentUser, plan_id: uuid.UUID | None = None):
    course = await _course(db, course_id)
    if plan_id is not None:
        await get_owned_plan(db, user, plan_id)
    db.add(CourseClick(user_id=user.id, course_id=course.id, plan_id=plan_id, is_partner=course.is_partner))
    await db.commit()
    return {"redirect_url": courses.outbound_url(course), "is_partner": course.is_partner}

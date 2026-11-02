from typing import Annotated

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, or_, select

from app.api.deps import DB, Embeddings, Pagination, cache_public, is_postgres
from app.core.errors import not_found
from app.core.pagination import Page, paginate_scalars
from app.models import Occupation, OccupationTask, Skill
from app.schemas.reference import NextRoleOut, OccupationDetail, OccupationOut, OccupationTasksOut, SkillOut
from app.services import reference, scoring

router = APIRouter(tags=["Data referensi"])

CACHE_SECONDS = 3600
GRADIENT_LABELS = {
    0: "Tidak terpapar",
    1: "Gradien 1 (paparan rendah)",
    2: "Gradien 2 (paparan sedang)",
    3: "Gradien 3 (paparan tinggi)",
    4: "Gradien 4 (paparan tertinggi)",
}


@router.get("/occupations", response_model=list[OccupationOut], summary="Autocomplete jabatan KBJI ke ISCO-08")
async def search_occupations(
    response: Response,
    db: DB,
    embeddings: Embeddings,
    q: Annotated[str, Query(max_length=100, description="Teks pencarian, mis. 'admin'")] = "",
    limit: Annotated[int, Query(ge=1, le=30)] = 10,
):
    cache_public(response, CACHE_SECONDS)
    return await reference.search_occupations(db, embeddings, q, limit, postgres=is_postgres(db))


async def _occupation(db, code: str) -> Occupation:
    occupation = await db.get(Occupation, code)
    if occupation is None:
        raise not_found("Jabatan")
    return occupation


async def _tasks(db, isco_code: str) -> list[OccupationTask]:
    rows = await db.scalars(
        select(OccupationTask).where(OccupationTask.isco_code == isco_code).order_by(OccupationTask.ilo_score.desc())
    )
    return list(rows.all())


@router.get("/occupations/{code}", response_model=OccupationDetail, summary="Detail jabatan: rata-rata paparan, gradien ILO")
async def get_occupation(code: str, response: Response, db: DB):
    occupation = await _occupation(db, code)
    tasks = await _tasks(db, occupation.isco_code)
    groups = {"high": 0, "medium": 0, "low": 0}
    for t in tasks:
        groups[scoring.group_for(t.ilo_score)] += 1
    cache_public(response, CACHE_SECONDS)
    return {
        **reference.occupation_view(occupation),
        "description": occupation.description,
        "aliases": occupation.aliases or [],
        "gradient_label": GRADIENT_LABELS.get(occupation.exposure_gradient, "Belum ada data"),
        "task_count": len(tasks),
        "task_groups": groups,
    }


@router.get("/occupations/{code}/tasks", response_model=OccupationTasksOut, summary="Template tugas jabatan beserta skor ILO")
async def get_occupation_tasks(code: str, response: Response, db: DB):
    occupation = await _occupation(db, code)
    tasks = await _tasks(db, occupation.isco_code)
    cache_public(response, CACHE_SECONDS)
    return {
        "occupation": reference.occupation_view(occupation),
        "tasks": [
            {
                "id": t.id,
                "description": t.description,
                "description_en": t.description_en,
                "ilo_score": t.ilo_score,
                "group": scoring.group_for(t.ilo_score),
                "group_label": scoring.GROUP_LABELS[scoring.group_for(t.ilo_score)],
            }
            for t in tasks
        ],
    }


@router.get(
    "/occupations/{code}/next-roles",
    response_model=list[NextRoleOut],
    summary="Kandidat jabatan tujuan (kemiripan skill ESCO)",
)
async def get_next_roles(
    code: str, response: Response, db: DB, limit: Annotated[int, Query(ge=1, le=20)] = 5
):
    occupation = await _occupation(db, code)
    cache_public(response, CACHE_SECONDS)
    return await reference.next_roles(db, occupation, limit)


@router.get("/skills", response_model=Page[SkillOut], summary="Daftar skill (label Indonesia)")
async def list_skills(
    response: Response,
    db: DB,
    page: Pagination,
    q: Annotated[str | None, Query(max_length=100)] = None,
    category: Annotated[str | None, Query(max_length=40)] = None,
):
    stmt = select(Skill).order_by(Skill.label)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(Skill.label).like(like), func.lower(Skill.label_en).like(like)))
    if category:
        stmt = stmt.where(Skill.category == category)
    rows, meta = await paginate_scalars(db, stmt, page)
    cache_public(response, CACHE_SECONDS)
    return {"data": rows, "meta": meta}

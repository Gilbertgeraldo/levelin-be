from typing import Annotated

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, or_, select

from app.api.deps import DB, LLM, CurrentUser, Embeddings, Pagination, cache_public, is_postgres
from app.core.errors import not_found
from app.core.pagination import Page, paginate_scalars
from app.core.ratelimit import begin_ai_call, finish_ai_call
from app.models import Occupation, OccupationTask, Skill
from app.schemas.reference import (
    NextRoleOut,
    OccupationDetail,
    OccupationOut,
    OccupationTasksOut,
    SkillCoachOut,
    SkillDrillCheckIn,
    SkillDrillCheckOut,
    SkillOut,
)
from app.services import coaching, reference, scoring
from app.services.llm import LLMCallInfo

router = APIRouter(tags=["Data referensi"])

CACHE_SECONDS = 3600
# Label kategori indeks ILO, sama dengan prototipe
GRADIENT_LABELS = {
    "Exposed: Gradient 4": "Gradien 4 (paparan tertinggi)",
    "Exposed: Gradient 3": "Gradien 3",
    "Exposed: Gradient 2": "Gradien 2",
    "Exposed: Gradient 1": "Gradien 1",
    "Minimal Exposure": "Paparan minimal",
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
        select(OccupationTask).where(OccupationTask.isco_code == isco_code).order_by(OccupationTask.id)
    )
    return list(rows.all())


@router.get("/occupations/{code}", response_model=OccupationDetail, summary="Detail jabatan: rata-rata paparan, gradien ILO")
async def get_occupation(code: str, response: Response, db: DB):
    occupation = await _occupation(db, code)
    tasks = await _tasks(db, occupation.isco_code)
    groups = {"auto": 0, "assist": 0, "human": 0}
    for t in tasks:
        groups[scoring.group_for(t.ilo_score)] += 1
    cache_public(response, CACHE_SECONDS)
    return {
        **reference.occupation_view(occupation),
        "description": occupation.description,
        "aliases": occupation.aliases or [],
        "gradient_label": GRADIENT_LABELS.get(occupation.gradient_cat or "", "Belum ada data"),
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
                "id": t.code or str(t.id),
                "description": t.description,
                "description_en": t.description_en,
                "ilo_score": t.ilo_score,
                "kind": t.kind,
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


@router.get("/skills", response_model=Page[SkillOut], summary="Daftar skill (label Indonesia bila tersedia)")
async def list_skills(
    response: Response,
    db: DB,
    page: Pagination,
    q: Annotated[str | None, Query(max_length=100)] = None,
    category: Annotated[str | None, Query(max_length=40)] = None,
):
    stmt = select(Skill).order_by(func.coalesce(Skill.label_id, Skill.label), Skill.id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Skill.label.ilike(like),
                Skill.label_id.ilike(like),
                Skill.label_en.ilike(like),
            )
        )
    if category:
        stmt = stmt.where(Skill.category == category)
    rows, meta = await paginate_scalars(db, stmt, page)
    cache_public(response, CACHE_SECONDS)
    return {
        "data": [
            {
                "id": s.id,
                "label": s.label_id or s.label,
                "label_en": s.label_en,
                "category": s.category,
                "esco_uri": s.esco_uri,
            }
            for s in rows
        ],
        "meta": meta,
    }


async def _skill(db, skill_id: int) -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise not_found("Skill")
    return skill


@router.post(
    "/skills/{skill_id}/coach",
    response_model=SkillCoachOut,
    summary="AI Skill Coach: panduan kerja 3 menit, rumus prompt AI, dan latihan mikro tanpa potong kuota misi",
)
async def skill_coach(skill_id: int, db: DB, user: CurrentUser, llm: LLM):
    skill = await _skill(db, skill_id)
    usage = await begin_ai_call(db, user.id, "coach")
    info = LLMCallInfo()
    result = await coaching.generate_skill_coach(db, user, skill, llm, info)
    finish_ai_call(usage, result["engine"], info)
    await db.commit()
    return result


@router.post(
    "/skills/{skill_id}/drill-check",
    response_model=SkillDrillCheckOut,
    summary="Evaluasi instan jawaban latihan mikro (Micro-Drill) oleh AI Coach",
)
async def skill_drill_check(skill_id: int, body: SkillDrillCheckIn, db: DB, user: CurrentUser, llm: LLM):
    skill = await _skill(db, skill_id)
    usage = await begin_ai_call(db, user.id, "coach")
    info = LLMCallInfo()
    result = await coaching.evaluate_skill_drill(db, user, skill, body, llm, info)
    finish_ai_call(usage, result["engine"], info)
    await db.commit()
    return result

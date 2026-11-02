import uuid
from typing import Literal

from fastapi import APIRouter, Response
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, Pagination
from app.core.pagination import Page, paginate_list
from app.models import Mission
from app.schemas.missions import AttemptOut, MissionDetail, MissionListItem
from app.services import missions

router = APIRouter(prefix="/missions", tags=["Misi & atasan AI"])

MissionStatus = Literal["not_started", "in_progress", "passed", "failed", "under_review"]


def mission_item(mission: Mission, status: str, reason: str | None) -> dict:
    return {
        "id": mission.id,
        "slug": mission.slug,
        "title": mission.title,
        "summary": mission.summary,
        "difficulty": mission.difficulty,
        "est_minutes": mission.est_minutes,
        "skills": [{"id": s.id, "label": s.label} for s in mission.skills],
        "status": status,
        "is_recommended": reason is not None,
        "recommend_reason": reason,
        "is_custom": mission.org_id is not None,
    }


@router.get("", response_model=Page[MissionListItem], summary="Daftar misi dengan status dan rekomendasi")
async def list_missions(
    db: DB,
    user: CurrentUser,
    page: Pagination,
    recommended: bool = False,
    status: MissionStatus | None = None,
):
    all_missions = list((await db.scalars(select(Mission).where(*missions.visible_filter(user)))).all())
    statuses = await missions.mission_statuses(db, user.id, [m.id for m in all_missions])
    gap, isco = await missions.recommendation_context(db, user)

    rows = []
    for m in all_missions:
        st = statuses.get(m.id, "not_started")
        score, reason = missions.relevance(m, gap, isco)
        if status and st != status:
            continue
        if recommended and (score <= 0 or st == "passed"):
            continue
        rows.append((m, st, score, reason))
    if recommended:
        rows.sort(key=lambda r: (-r[2], r[0].difficulty, r[0].title))
    else:
        rows.sort(key=lambda r: (r[0].org_id is None, r[0].difficulty, r[0].title))
    items, meta = paginate_list([mission_item(m, st, reason) for m, st, _, reason in rows], page)
    return {"data": items, "meta": meta}


@router.get("/{mission_id}", response_model=MissionDetail, summary="Brief, data simulasi, kolom jawaban, rubrik (tanpa kunci jawaban)")
async def get_mission(mission_id: uuid.UUID, db: DB, user: CurrentUser):
    mission = await missions.get_visible_mission(db, user, mission_id)
    status = (await missions.mission_statuses(db, user.id, [mission.id])).get(mission.id, "not_started")
    gap, isco = await missions.recommendation_context(db, user)
    _, reason = missions.relevance(mission, gap, isco)
    persona = mission.boss_persona or {}
    return {
        **mission_item(mission, status, reason),
        "brief": mission.brief,
        "boss_persona": {"name": persona.get("name"), "role": persona.get("role")},
        "simulation_data": mission.simulation_data or {},
        "answer_fields": mission.answer_fields or [],
        "rubric": [
            {
                "id": r["id"],
                "criterion": r.get("criterion", r["id"]),
                "description": r.get("description"),
                "weight": r.get("weight", 1),
            }
            for r in mission.rubric or []
        ],
        "pass_score": mission.pass_score,
    }


@router.post(
    "/{mission_id}/attempts",
    status_code=201,
    response_model=AttemptOut,
    summary="Mulai percobaan (cek kuota). Percobaan yang masih berjalan dikembalikan ulang (200).",
)
async def start_attempt(mission_id: uuid.UUID, response: Response, db: DB, user: CurrentUser):
    mission = await missions.get_visible_mission(db, user, mission_id)
    attempt, created, quota = await missions.start_attempt(db, user, mission)
    await db.commit()
    if not created:
        response.status_code = 200
    return {
        "id": attempt.id,
        "mission_id": attempt.mission_id,
        "status": attempt.status,
        "draft": attempt.draft or {},
        "created": created,
        "created_at": attempt.created_at,
        "quota": quota,
    }

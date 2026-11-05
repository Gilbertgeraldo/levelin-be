import uuid
from typing import Literal

from fastapi import APIRouter, Response
from sqlalchemy import select

from app.api.deps import DB, Pagination, Reviewer
from app.core.errors import ApiError, not_found
from app.core.pagination import Page, paginate_scalars
from app.db.base import utcnow
from app.ingest import apply_mission_payload
from app.models import Attempt, CustomMissionRequest, Mission, Organization, Review, Skill
from app.schemas.admin import (
    CustomMissionAdminOut,
    MissionAdminIn,
    MissionAdminOut,
    ReviewDecisionIn,
    ReviewOut,
)
from app.services import missions as mission_service
from app.services.audits import refresh_readiness
from app.services.notifications import notify

router = APIRouter(prefix="/admin", tags=["Internal tim (reviewer)"])


async def review_view(db, review: Review) -> dict:
    attempt = await db.get(Attempt, review.attempt_id)
    mission = await db.get(Mission, attempt.mission_id)
    return {
        "id": review.id,
        "kind": review.kind,
        "status": review.status,
        "reason": review.reason,
        "decision": review.decision,
        "original_score": review.original_score,
        "new_score": review.new_score,
        "note": review.note,
        "created_at": review.created_at,
        "resolved_at": review.resolved_at,
        "attempt": {
            "id": attempt.id,
            "user_id": attempt.user_id,
            "score": attempt.score,
            "objective_score": attempt.objective_score,
            "rubric_score": attempt.rubric_score,
            "rubric_avg": attempt.rubric_avg,
            "ai_usage": attempt.ai_usage,
            "passed": attempt.passed,
            "engine": attempt.engine,
            "answers": attempt.answers,
            "result": attempt.result,
            "submitted_at": attempt.submitted_at,
        },
        "mission": {
            "id": mission.id,
            "title": mission.title,
            "pass_score": mission.pass_score,
            "rubric": mission.rubric or [],
            "answer_key": mission.answer_key or [],
        },
    }


@router.get("/reviews", response_model=Page[ReviewOut], summary="Antrean sampel penilaian AI dan banding")
async def list_reviews(
    db: DB,
    reviewer: Reviewer,
    page: Pagination,
    status: Literal["pending", "resolved"] = "pending",
    kind: Literal["sample", "appeal"] | None = None,
):
    # Banding didahulukan, lalu yang paling lama menunggu
    stmt = select(Review).where(Review.status == status).order_by((Review.kind == "appeal").desc(), Review.created_at)
    if kind:
        stmt = stmt.where(Review.kind == kind)
    rows, meta = await paginate_scalars(db, stmt, page)
    return {"data": [await review_view(db, r) for r in rows], "meta": meta}


@router.post("/reviews/{review_id}", response_model=ReviewOut, summary="Putusan peninjau (setuju atau ubah hasil lulus)")
async def decide_review(review_id: uuid.UUID, body: ReviewDecisionIn, db: DB, reviewer: Reviewer):
    review = await db.get(Review, review_id)
    if review is None:
        raise not_found("Review")
    if review.status == "resolved":
        raise ApiError(409, "already_resolved", "Review ini sudah diputus.")
    attempt = await db.get(Attempt, review.attempt_id)
    mission = await db.get(Mission, attempt.mission_id)

    review.status = "resolved"
    review.decision = body.decision
    review.note = body.note
    review.reviewer_id = reviewer.id
    review.resolved_at = utcnow()

    if body.decision == "override":
        was_passed = bool(attempt.passed)
        attempt.passed = body.passed
        if body.new_score is not None:
            attempt.score = body.new_score
        review.new_score = attempt.score
        result = dict(attempt.result or {})
        result["reviewer_override"] = {
            "passed": body.passed,
            "score": body.new_score,
            "note": body.note,
            "at": utcnow().isoformat(),
        }
        if attempt.passed and not was_passed:
            entries = await mission_service.award_passport(db, attempt, mission)
            result["skills_earned"] = [{"id": s.id, "code": s.code, "label": s.label} for s in mission.skills]
            result["passport_evidence_id"] = str(entries[0].id) if entries else None
        elif was_passed and not attempt.passed:
            await mission_service.revoke_passport(db, attempt)
            result["skills_earned"] = []
            result["passport_evidence_id"] = None
        elif attempt.passed:
            await mission_service.update_passport_score(db, attempt)
        attempt.result = result
        await db.flush()
        await refresh_readiness(db, attempt.user_id)
    else:
        review.new_score = attempt.score

    # Status review percobaan selesai bila tidak ada review lain yang masih menunggu
    others = await db.scalar(
        select(Review.id).where(
            Review.attempt_id == attempt.id, Review.status == "pending", Review.id != review.id
        )
    )
    if not others:
        attempt.review_status = "reviewed"

    if review.kind == "appeal" or body.decision == "override":
        verdict = "lulus" if attempt.passed else "belum lulus"
        await notify(
            db,
            attempt.user_id,
            "review_result",
            "Hasil tinjauan misi",
            f"Tim peninjau sudah memeriksa misi \"{mission.title}\": hasilnya {verdict}.",
            {"attempt_id": str(attempt.id)},
        )
    await db.commit()
    return await review_view(db, review)


def mission_admin_view(m: Mission) -> dict:
    return {
        "id": m.id,
        "slug": m.slug,
        "title": m.title,
        "summary": m.summary,
        "brief": m.brief,
        "boss_persona": m.boss_persona or {},
        "simulation_data": m.simulation_data or {},
        "answer_fields": m.answer_fields or [],
        "rubric": m.rubric or [],
        "answer_key": m.answer_key or [],
        "occupation_isco_codes": m.occupation_isco_codes or [],
        "skills": [{"id": s.id, "code": s.code, "label": s.label} for s in m.skills],
        "kind": m.kind,
        "difficulty": m.difficulty,
        "est_minutes": m.est_minutes,
        "duration_label": m.duration_label,
        "pass_score": m.pass_score,
        "objective_weight": m.objective_weight,
        "is_published": m.is_published,
        "org_id": m.org_id,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
    }


async def apply_mission(db, mission: Mission, body: MissionAdminIn) -> None:
    skills = list((await db.scalars(select(Skill).where(Skill.id.in_(body.skill_ids)))).all()) if body.skill_ids else []
    missing = sorted(set(body.skill_ids) - {s.id for s in skills})
    if missing:
        raise ApiError(400, "unknown_skills", "Ada skill yang tidak dikenal.", details=missing)
    apply_mission_payload(mission, body)
    mission.skills = skills


async def link_custom_request(db, mission: Mission, request_id: uuid.UUID | None) -> None:
    if request_id is None:
        return
    request = await db.get(CustomMissionRequest, request_id)
    if request is None:
        raise ApiError(400, "unknown_custom_request", "Pesanan misi kustom tidak ditemukan.")
    was_published = request.status == "published"
    mission.org_id = request.org_id
    request.mission_id = mission.id
    request.status = "published" if mission.is_published else "in_review"
    if mission.is_published and not was_published and request.requested_by is not None:
        await notify(
            db,
            request.requested_by,
            "custom_mission",
            "Misi kustom telah diterbitkan",
            f'Pesanan misi "{request.title}" telah diterbitkan menjadi misi "{mission.title}".',
            {
                "custom_mission_id": str(request.id),
                "mission_id": str(mission.id),
                "mission_slug": mission.slug,
            },
        )


@router.get(
    "/custom-missions",
    response_model=Page[CustomMissionAdminOut],
    summary="Daftar pesanan misi kustom dari seluruh organisasi untuk dikerjakan peninjau",
)
async def list_custom_missions_admin(
    db: DB,
    reviewer: Reviewer,
    page: Pagination,
    status: Literal["requested", "in_review", "published", "rejected"] | None = None,
):
    stmt = select(CustomMissionRequest).order_by(CustomMissionRequest.created_at.desc())
    if status:
        stmt = stmt.where(CustomMissionRequest.status == status)
    rows, meta = await paginate_scalars(db, stmt, page)
    org_ids = {r.org_id for r in rows if r.org_id}
    org_names = (
        dict((await db.execute(select(Organization.id, Organization.name).where(Organization.id.in_(org_ids)))).all())
        if org_ids
        else {}
    )
    data = [
        {
            "id": r.id,
            "org_id": r.org_id,
            "org_name": org_names.get(r.org_id),
            "requested_by": r.requested_by,
            "title": r.title,
            "description": r.description,
            "target_role": r.target_role,
            "file_name": r.file_name,
            "file_size": r.file_size,
            "status": r.status,
            "mission_id": r.mission_id,
            "created_at": r.created_at,
        }
        for r in rows
    ]
    return {"data": data, "meta": meta}


@router.get(
    "/missions",
    response_model=Page[MissionAdminOut],
    summary="Daftar seluruh misi di bank misi (termasuk draf dan misi kustom)",
)
async def list_missions_admin(
    db: DB,
    reviewer: Reviewer,
    page: Pagination,
    is_published: bool | None = None,
):
    stmt = select(Mission).order_by(Mission.updated_at.desc(), Mission.created_at.desc())
    if is_published is not None:
        stmt = stmt.where(Mission.is_published.is_(is_published))
    rows, meta = await paginate_scalars(db, stmt, page)
    return {"data": [mission_admin_view(m) for m in rows], "meta": meta}


@router.post(
    "/missions",
    status_code=201,
    response_model=MissionAdminOut,
    summary="Tambah misi ke bank misi (slug yang sudah ada = ubah)",
)
async def upsert_mission(body: MissionAdminIn, response: Response, db: DB, reviewer: Reviewer):
    mission = await db.scalar(select(Mission).where(Mission.slug == body.slug))
    if mission is None:
        mission = Mission()
        db.add(mission)
    else:
        response.status_code = 200
    await apply_mission(db, mission, body)
    await db.flush()
    await link_custom_request(db, mission, body.custom_request_id)
    await db.commit()
    return mission_admin_view(mission)


@router.get("/missions/{mission_id}", response_model=MissionAdminOut, summary="Detail misi lengkap dengan kunci jawaban")
async def get_mission_admin(mission_id: uuid.UUID, db: DB, reviewer: Reviewer):
    mission = await db.get(Mission, mission_id)
    if mission is None:
        raise not_found("Misi")
    return mission_admin_view(mission)


@router.put("/missions/{mission_id}", response_model=MissionAdminOut, summary="Ubah misi")
async def update_mission(mission_id: uuid.UUID, body: MissionAdminIn, db: DB, reviewer: Reviewer):
    mission = await db.get(Mission, mission_id)
    if mission is None:
        raise not_found("Misi")
    clash = await db.scalar(select(Mission.id).where(Mission.slug == body.slug, Mission.id != mission.id))
    if clash:
        raise ApiError(409, "slug_taken", "Slug sudah dipakai misi lain.")
    await apply_mission(db, mission, body)
    await link_custom_request(db, mission, body.custom_request_id)
    await db.commit()
    return mission_admin_view(mission)

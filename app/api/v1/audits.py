import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select

from app.api.deps import DB, LLM, CurrentUser, Embeddings, Pagination
from app.core.errors import ApiError, not_found
from app.core.pagination import Page, paginate_scalars
from app.core.ratelimit import begin_ai_call, finish_ai_call
from app.models import AIUsage, Audit, AuditTask, Occupation, OccupationTask, Profile
from app.schemas.audits import (
    AuditCreate,
    AuditListItem,
    AuditOut,
    AuditTaskCreate,
    AuditTaskPatch,
    ShareCardOut,
)
from app.services import audits, prompts, scoring
from app.services.guardrails import SentenceGuard
from app.services.llm import LLMCallInfo
from app.services.streaming import stream_with_fallback
from app.services.textutil import fmt_num

router = APIRouter(prefix="/audits", tags=["Audit & Peta Tugas"])


async def audit_out(db, audit: Audit) -> dict:
    occupation = await db.get(Occupation, audit.occupation_code)
    ids = {t.template_task_id for t in audit.tasks if t.template_task_id}
    templates = (
        {t.id: t for t in (await db.scalars(select(OccupationTask).where(OccupationTask.id.in_(ids)))).all()}
        if ids
        else {}
    )
    return {
        "id": audit.id,
        "occupation": {
            "code": audit.occupation_code,
            "title": occupation.title if occupation else audit.occupation_code,
            "isco_code": audit.isco_code,
        },
        "story": audit.story,
        "engine": audit.engine,
        "engine_detail": audit.engine_detail or {},
        "summary": audit.summary,
        "tasks": [
            {
                "id": t.id,
                "position": t.position,
                "description": t.description,
                "hours_per_week": t.hours_per_week,
                "uses_ai": t.uses_ai,
                "ilo_score": round(t.ilo_score, 3),
                "group": t.group,
                "group_label": scoring.GROUP_LABELS[t.group],
                "template_task": {"id": t.template_task_id, "description": templates[t.template_task_id].description}
                if t.template_task_id in templates
                else None,
                "match_similarity": t.match_similarity,
                "match_overridden": t.match_overridden,
                "source": t.source,
            }
            for t in audit.tasks
        ],
        "created_at": audit.created_at,
        "updated_at": audit.updated_at,
    }


async def _require_consent(db, user) -> None:
    profile = await db.get(Profile, user.id)
    if profile is None or not profile.consent_data_processing:
        raise ApiError(
            403,
            "consent_required",
            "Simpan profil dan setujui pemrosesan data (PUT /v1/me/profile) sebelum membuat audit.",
        )


@router.post("", status_code=201, response_model=AuditOut, summary="Buat audit: ekstraksi (LLM), pencocokan (embedding), skor")
async def create_audit(body: AuditCreate, db: DB, user: CurrentUser, llm: LLM, embeddings: Embeddings):
    await _require_consent(db, user)
    if not (body.story and body.story.strip()) and not body.tasks:
        raise ApiError(400, "no_tasks", "Ceritakan pekerjaanmu atau pilih minimal satu tugas dari daftar.")
    usage = await begin_ai_call(db, user.id, "audit")
    info = LLMCallInfo()
    audit = await audits.create_audit(
        db,
        user,
        occupation_code=body.occupation_code,
        story=body.story,
        selected=[t.model_dump() for t in body.tasks],
        llm=llm,
        embeddings=embeddings,
        info=info,
    )
    finish_ai_call(usage, audit.engine, info)
    await db.commit()
    return await audit_out(db, audit)


@router.get("/latest", response_model=AuditOut, summary="Audit terbaru lengkap dengan ringkasan")
async def get_latest(db: DB, user: CurrentUser):
    audit = await audits.latest_audit(db, user.id)
    if audit is None:
        raise ApiError(404, "no_audit", "Kamu belum punya audit. Buat audit pertama dulu.")
    return await audit_out(db, audit)


@router.get("", response_model=Page[AuditListItem], summary="Riwayat audit (paginasi)")
async def list_audits(db: DB, user: CurrentUser, page: Pagination):
    stmt = select(Audit).where(Audit.user_id == user.id).order_by(Audit.created_at.desc())
    rows, meta = await paginate_scalars(db, stmt, page)
    codes = {a.occupation_code for a in rows}
    titles = (
        dict((await db.execute(select(Occupation.code, Occupation.title).where(Occupation.code.in_(codes)))).all())
        if codes
        else {}
    )
    data = [
        {
            "id": a.id,
            "occupation": {"code": a.occupation_code, "title": titles.get(a.occupation_code, a.occupation_code), "isco_code": a.isco_code},
            "readiness_score": a.readiness_score,
            "exposure_index": a.exposure_index,
            "hours_saved_week": a.hours_saved_week,
            "total_hours": a.total_hours,
            "task_count": len(a.tasks),
            "engine": a.engine,
            "created_at": a.created_at,
        }
        for a in rows
    ]
    return {"data": data, "meta": meta}


@router.get("/{audit_id}", response_model=AuditOut, summary="Detail satu audit")
async def get_audit(audit_id: uuid.UUID, db: DB, user: CurrentUser):
    return await audit_out(db, await audits.get_owned_audit(db, user, audit_id))


def _find_task(audit: Audit, task_id: uuid.UUID) -> AuditTask:
    for task in audit.tasks:
        if task.id == task_id:
            return task
    raise not_found("Tugas")


async def _template_for(db, audit: Audit, template_id: int) -> OccupationTask:
    template = await db.get(OccupationTask, template_id)
    if template is None or template.isco_code != audit.isco_code:
        raise ApiError(400, "invalid_template_task", "Template tugas tidak cocok dengan jabatan audit ini.")
    return template


async def _default_score(db, audit: Audit) -> float:
    occupation = await db.get(Occupation, audit.occupation_code)
    templates = list((await db.scalars(select(OccupationTask).where(OccupationTask.isco_code == audit.isco_code))).all())
    return audits.fallback_score(occupation, templates)


@router.patch(
    "/{audit_id}/tasks/{task_id}",
    response_model=AuditOut,
    summary="Ubah jam/minggu, 'sudah pakai AI', atau ganti kecocokan ILO",
)
async def patch_task(audit_id: uuid.UUID, task_id: uuid.UUID, body: AuditTaskPatch, db: DB, user: CurrentUser):
    audit = await audits.get_owned_audit(db, user, audit_id)
    task = _find_task(audit, task_id)
    fields = body.model_fields_set
    if "description" in fields and body.description:
        task.description = body.description.strip()
    if "hours_per_week" in fields and body.hours_per_week is not None:
        task.hours_per_week = body.hours_per_week
    if "uses_ai" in fields and body.uses_ai is not None:
        task.uses_ai = body.uses_ai
    if "template_task_id" in fields:
        if body.template_task_id is None:
            task.template_task_id = None
            task.ilo_score = await _default_score(db, audit)
        else:
            template = await _template_for(db, audit, body.template_task_id)
            task.template_task_id = template.id
            task.ilo_score = template.ilo_score
        task.match_similarity = None
        task.match_overridden = True
    await audits.recompute(db, audit)
    await db.commit()
    return await audit_out(db, audit)


@router.post("/{audit_id}/tasks", status_code=201, response_model=AuditOut, summary="Tambah tugas manual")
async def add_task(audit_id: uuid.UUID, body: AuditTaskCreate, db: DB, user: CurrentUser, embeddings: Embeddings):
    audit = await audits.get_owned_audit(db, user, audit_id)
    if len(audit.tasks) >= 30:
        raise ApiError(400, "too_many_tasks", "Satu audit maksimal 30 tugas.")
    if body.template_task_id is not None:
        template, similarity, overridden = await _template_for(db, audit, body.template_task_id), None, True
    else:
        templates = list(
            (await db.scalars(select(OccupationTask).where(OccupationTask.isco_code == audit.isco_code))).all()
        )
        [(template, similarity)], _ = await audits.match_to_templates(embeddings, [body.description], templates)
        overridden = False
    score = template.ilo_score if template else await _default_score(db, audit)
    audit.tasks.append(
        AuditTask(
            position=max((t.position for t in audit.tasks), default=-1) + 1,
            description=body.description.strip(),
            hours_per_week=body.hours_per_week,
            uses_ai=body.uses_ai,
            template_task_id=template.id if template else None,
            match_similarity=similarity,
            ilo_score=score,
            source="manual",
            match_overridden=overridden,
        )
    )
    await audits.recompute(db, audit)
    await db.commit()
    return await audit_out(db, audit)


@router.delete("/{audit_id}/tasks/{task_id}", response_model=AuditOut, summary="Hapus tugas")
async def delete_task(audit_id: uuid.UUID, task_id: uuid.UUID, db: DB, user: CurrentUser):
    audit = await audits.get_owned_audit(db, user, audit_id)
    task = _find_task(audit, task_id)
    if len(audit.tasks) <= 1:
        raise ApiError(409, "last_task", "Audit harus punya minimal satu tugas.")
    audit.tasks.remove(task)
    await audits.recompute(db, audit)
    await db.commit()
    return await audit_out(db, audit)


async def _explain_ctx(audit_id: uuid.UUID, db: DB, user: CurrentUser) -> tuple[Audit, AIUsage]:
    audit = await audits.get_owned_audit(db, user, audit_id)
    usage = await begin_ai_call(db, user.id, "explain")
    return audit, usage


@router.post(
    "/{audit_id}/explain",
    response_class=EventSourceResponse,
    summary="Penjelasan peta oleh LLM ringan, hanya dari angka audit (SSE)",
    description="Event: meta {engine}, delta {text}, reset {reason}, done {engine}.",
)
async def explain(
    ctx: Annotated[tuple[Audit, AIUsage], Depends(_explain_ctx)], db: DB, llm: LLM
) -> AsyncIterator[ServerSentEvent]:
    audit, usage = ctx
    occupation = await db.get(Occupation, audit.occupation_code)
    prompt = audits.numbers_prompt(audits.audit_numbers(audit, occupation))
    await db.commit()  # lepas koneksi selama stream
    info = LLMCallInfo()

    def llm_stream():
        return llm.stream_text(
            tier="light",
            system=prompts.EXPLAIN_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            effort="low",
            info=info,
        )

    async def on_complete(text: str, engine: str, flagged: bool):
        finish_ai_call(usage, engine, info)
        await db.commit()

    async for event in stream_with_fallback(
        llm_stream=llm_stream if llm.available else None,
        fallback_text=lambda: audits.explain_fallback(audit),
        guard=SentenceGuard(),
        on_complete=on_complete,
    ):
        yield event


@router.get("/{audit_id}/share-card", response_model=ShareCardOut, summary="Data kartu bagikan Skor Kesiapan")
async def share_card(audit_id: uuid.UUID, db: DB, user: CurrentUser):
    audit = await audits.get_owned_audit(db, user, audit_id)
    occupation = await db.get(Occupation, audit.occupation_code)
    title = occupation.title if occupation else audit.occupation_code
    s = audit.summary or {}
    groups = s.get("groups") or {}
    ai_share = round((groups.get("high", {}).get("share", 0) + groups.get("medium", {}).get("share", 0)), 3)
    ordered = sorted(audit.tasks, key=lambda t: t.ilo_score, reverse=True)
    top_ai = next((t.description for t in ordered if t.group in ("high", "medium")), None)
    top_human = next((t.description for t in reversed(ordered) if t.group == "low"), None)
    readiness = s.get("readiness_score", audit.readiness_score)
    return {
        "readiness_score": readiness,
        "occupation_title": title,
        "hours_saved_week": s.get("hours_saved_week", 0),
        "ai_share": ai_share,
        "human_share": round(1 - ai_share, 3),
        "top_ai_task": top_ai,
        "top_human_task": top_human,
        "headline": f"Skor Kesiapan AI saya {readiness}/100",
        "share_text": (
            f"Saya baru memetakan pekerjaan saya sebagai {title} di Levelin. "
            f"{round(ai_share * 100)}% jam kerja saya bisa dibantu AI, berpotensi hemat "
            f"{fmt_num(s.get('hours_saved_week'))} jam per minggu. Skor Kesiapan saya {readiness}/100. #Levelin"
        ),
        "created_at": audit.created_at,
    }

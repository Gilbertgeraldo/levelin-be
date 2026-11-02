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
from app.schemas.audits import AuditCreate, AuditListItem, AuditOut, AuditTaskCreate, AuditTaskPatch, ShareCardOut
from app.services import audits, prompts, scoring
from app.services.guardrails import SentenceGuard
from app.services.llm import LLMCallInfo
from app.services.missions import first_recommended_title
from app.services.streaming import stream_with_fallback
from app.services.textutil import fmt_num

router = APIRouter(prefix="/audits", tags=["Audit & Peta Tugas"])

MAX_TASKS_PER_AUDIT = 30


async def audit_out(db, audit: Audit) -> dict:
    return audits.audit_view(audit, await db.get(Occupation, audit.occupation_code))


async def _profile_with_consent(db, user) -> Profile:
    profile = await db.get(Profile, user.id)
    if profile is None or not profile.consent_data_processing:
        raise ApiError(
            403,
            "consent_required",
            "Simpan profil dan setujui pemrosesan data (PUT /v1/me/profile) sebelum membuat audit.",
        )
    return profile


@router.post("", status_code=201, response_model=AuditOut, summary="Buat audit: tugas template + cerita -> Peta Tugas")
async def create_audit(body: AuditCreate, db: DB, user: CurrentUser, llm: LLM, embeddings: Embeddings):
    profile = await _profile_with_consent(db, user)
    occupation = await db.get(Occupation, body.occupation_code or profile.occupation_code)
    if occupation is None:
        raise not_found("Jabatan")
    if not (body.story and body.story.strip()) and not body.template_tasks:
        raise ApiError(400, "no_tasks", "Ceritakan pekerjaanmu atau pilih minimal satu tugas dari daftar.")
    usage = await begin_ai_call(db, user.id, "audit")
    info = LLMCallInfo()
    audit = await audits.create_audit(
        db,
        user,
        occupation=occupation,
        story=body.story,
        template_tasks=[t.model_dump() for t in body.template_tasks],
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


@router.get("", response_model=Page[AuditListItem], summary="Riwayat audit (cek ulang tiap 3 bulan)")
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
            "occupation": {"code": a.occupation_code, "name": titles.get(a.occupation_code, a.occupation_code)},
            "readiness_score": a.readiness_score,
            "hours_saved": a.hours_saved_week,
            "exposed_hours": (a.summary or {}).get("exposed_hours", 0),
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


async def _template_by_code(db, code: str) -> OccupationTask:
    template = await db.scalar(
        select(OccupationTask).where(OccupationTask.code == code, OccupationTask.is_candidate.is_(True))
    )
    if template is None:
        raise ApiError(400, "invalid_template_task", f"Tugas ILO {code} tidak dikenal.")
    return template


def _set_template(task: AuditTask, template: OccupationTask | None) -> None:
    task.template = template
    task.template_task_id = template.id if template else None
    task.ilo_score = template.ilo_score if template else None


@router.patch(
    "/{audit_id}/tasks/{task_id}",
    response_model=AuditOut,
    summary="Ubah jam/minggu, 'sudah pakai AI', padanan ILO, atau kelompok tugas tak terpetakan",
)
async def patch_task(audit_id: uuid.UUID, task_id: uuid.UUID, body: AuditTaskPatch, db: DB, user: CurrentUser):
    audit = await audits.get_owned_audit(db, user, audit_id)
    task = _find_task(audit, task_id)
    fields = body.model_fields_set
    if "name" in fields and body.name:
        task.description = body.name.strip()
    if "hours_per_week" in fields and body.hours_per_week is not None:
        task.hours_per_week = body.hours_per_week
    if "uses_ai" in fields and body.uses_ai is not None:
        task.uses_ai = body.uses_ai
    if "ilo_task_id" in fields:
        _set_template(task, await _template_by_code(db, body.ilo_task_id) if body.ilo_task_id else None)
        task.match_similarity = None
        task.match_overridden = True
        task.confidence = "manual" if body.ilo_task_id else None
    if "group" in fields:
        if task.ilo_score is not None and body.group is not None:
            raise ApiError(400, "group_from_ilo", "Kelompok tugas yang punya padanan ILO ditentukan dari skornya.")
        task.manual_group = body.group
    await audits.recompute(db, audit)
    await db.commit()
    return await audit_out(db, audit)


@router.post("/{audit_id}/tasks", status_code=201, response_model=AuditOut, summary="Tambah tugas manual")
async def add_task(audit_id: uuid.UUID, body: AuditTaskCreate, db: DB, user: CurrentUser):
    audit = await audits.get_owned_audit(db, user, audit_id)
    if len(audit.tasks) >= MAX_TASKS_PER_AUDIT:
        raise ApiError(400, "too_many_tasks", f"Satu audit maksimal {MAX_TASKS_PER_AUDIT} tugas.")
    template = await _template_by_code(db, body.ilo_task_id) if body.ilo_task_id else None
    task = AuditTask(
        position=max((t.position for t in audit.tasks), default=-1) + 1,
        description=body.name.strip(),
        hours_per_week=body.hours_per_week,
        uses_ai=body.uses_ai,
        manual_group=None if template else body.group,
        source="manual",
        confidence="manual" if template else None,
    )
    _set_template(task, template)
    audit.tasks.append(task)
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
    ctx: Annotated[tuple[Audit, AIUsage], Depends(_explain_ctx)], db: DB, user: CurrentUser, llm: LLM
) -> AsyncIterator[ServerSentEvent]:
    audit, usage = ctx
    occupation = await db.get(Occupation, audit.occupation_code)
    first_mission = await first_recommended_title(db, user)
    facts = audits.explain_facts(audit, occupation, user, first_mission)
    await db.commit()  # lepas koneksi selama stream
    info = LLMCallInfo()

    def llm_stream():
        return llm.stream_text(
            tier="light",
            system=prompts.EXPLAIN_SYSTEM,
            messages=[{"role": "user", "content": facts}],
            max_tokens=2000,
            effort="low",
            info=info,
        )

    async def on_complete(text: str, engine: str, flagged: bool):
        finish_ai_call(usage, engine, info)
        await db.commit()

    async for event in stream_with_fallback(
        llm_stream=llm_stream if llm.available else None,
        fallback_text=lambda: audits.explain_fallback(audit, user, first_mission),
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
    share = scoring.share_by_group(s.get("hours_by_group") or {})
    ordered = sorted(audit.tasks, key=lambda t: t.ilo_score or 0, reverse=True)
    top_ai = next((t.description for t in ordered if t.group in scoring.EXPOSED), None)
    top_human = next((t.description for t in reversed(ordered) if t.group == "human"), None)
    readiness = s.get("readiness_score", audit.readiness_score)
    exposed_share = share["auto"] + share["assist"]
    return {
        "readiness_score": readiness,
        "occupation_title": title,
        "hours_saved": s.get("hours_saved", 0),
        "share": share,
        "top_ai_task": top_ai,
        "top_human_task": top_human,
        "headline": f"Skor Kesiapan AI saya {readiness}/100",
        "share_text": (
            f"Saya baru memetakan pekerjaan saya sebagai {title} di Levelin. {exposed_share}% jam kerja saya "
            f"bergeser karena AI, dan saya bisa hemat sekitar {fmt_num(s.get('hours_saved'))} jam per minggu. "
            f"Skor Kesiapan saya {readiness}/100. #Levelin"
        ),
        "created_at": audit.created_at,
    }

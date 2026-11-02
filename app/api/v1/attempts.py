import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select

from app.api.deps import DB, LLM, CurrentUser
from app.core.errors import ApiError
from app.core.ratelimit import begin_ai_call, finish_ai_call
from app.db.base import utcnow
from app.models import AIUsage, Attempt, AttemptMessage, Mission, Review
from app.schemas.missions import AnswersIn, AppealIn, AppealOut, ChatIn, ChatMessageOut, DraftOut, ResultOut, SubmitIn
from app.services import grading, missions
from app.services.guardrails import SentenceGuard, answer_leak_check, layoff_check
from app.services.llm import LLMCallInfo
from app.services.streaming import stream_with_fallback

router = APIRouter(prefix="/attempts", tags=["Misi & atasan AI"])


async def _attempt_and_mission(db, user, attempt_id: uuid.UUID) -> tuple[Attempt, Mission]:
    attempt = await missions.get_owned_attempt(db, user, attempt_id)
    mission = await db.get(Mission, attempt.mission_id)
    return attempt, mission


async def _chat_ctx(
    attempt_id: uuid.UUID, body: ChatIn, db: DB, user: CurrentUser
) -> tuple[Attempt, Mission, AttemptMessage, AIUsage]:
    attempt, mission = await _attempt_and_mission(db, user, attempt_id)
    missions.require_in_progress(attempt)
    usage = await begin_ai_call(db, user.id, "chat")
    message = AttemptMessage(attempt_id=attempt.id, role="user", content=body.content.strip())
    db.add(message)
    await db.commit()
    return attempt, mission, message, usage


@router.post(
    "/{attempt_id}/messages",
    response_class=EventSourceResponse,
    summary="Kirim pesan ke atasan AI; balasan di-stream (SSE)",
    description="Event: meta {engine}, delta {text}, reset {reason}, done {engine, message_id, flagged}.",
)
async def send_message(
    ctx: Annotated[tuple[Attempt, Mission, AttemptMessage, AIUsage], Depends(_chat_ctx)], db: DB, llm: LLM
) -> AsyncIterator[ServerSentEvent]:
    attempt, mission, user_message, usage = ctx
    history = missions.to_llm_messages(await missions.chat_history(db, attempt.id))
    system = missions.boss_system(mission)
    await db.commit()  # lepas koneksi selama stream
    info = LLMCallInfo()

    def llm_stream():
        return llm.stream_text(
            tier="main", system=system, messages=history, max_tokens=4000, effort="low", info=info
        )

    async def on_complete(text: str, engine: str, flagged: bool):
        reply = AttemptMessage(
            attempt_id=attempt.id, role="assistant", content=text, engine=engine, flagged=flagged
        )
        db.add(reply)
        finish_ai_call(usage, engine, info)
        await db.commit()
        return {"message_id": str(reply.id), "flagged": flagged}

    async for event in stream_with_fallback(
        llm_stream=llm_stream if llm.available and history else None,
        fallback_text=lambda: missions.boss_fallback_reply(mission, user_message.content),
        guard=SentenceGuard([layoff_check, answer_leak_check(mission.answer_key)]),
        on_complete=on_complete,
    ):
        yield event


@router.get("/{attempt_id}/messages", response_model=list[ChatMessageOut], summary="Riwayat chat")
async def list_messages(attempt_id: uuid.UUID, db: DB, user: CurrentUser):
    attempt = await missions.get_owned_attempt(db, user, attempt_id)
    rows = await db.scalars(
        select(AttemptMessage)
        .where(AttemptMessage.attempt_id == attempt.id)
        .order_by(AttemptMessage.created_at)
        .limit(500)
    )
    return rows.all()


@router.put("/{attempt_id}/draft", response_model=DraftOut, summary="Simpan otomatis jawaban yang belum dikumpulkan")
async def save_draft(attempt_id: uuid.UUID, body: AnswersIn, db: DB, user: CurrentUser):
    attempt, mission = await _attempt_and_mission(db, user, attempt_id)
    missions.require_in_progress(attempt)
    known = {f["id"] for f in mission.answer_fields or []}
    unknown = sorted(set(body.answers) - known)
    if unknown:
        raise ApiError(400, "invalid_answers", "Ada kolom jawaban yang tidak dikenal.", details=unknown)
    attempt.draft = dict(body.answers)
    attempt.updated_at = utcnow()
    await db.commit()
    return {"attempt_id": attempt.id, "draft": attempt.draft, "updated_at": attempt.updated_at}


@router.post(
    "/{attempt_id}/submit",
    response_model=ResultOut,
    summary="Kumpulkan jawaban: cek objektif (skrip) lalu rubrik (LLM JSON)",
)
async def submit(attempt_id: uuid.UUID, body: SubmitIn, db: DB, user: CurrentUser, llm: LLM):
    attempt, mission = await _attempt_and_mission(db, user, attempt_id)
    missions.require_in_progress(attempt)
    grading.validate_answers(mission, body.answers)
    usage = await begin_ai_call(db, user.id, "grade")
    info = LLMCallInfo()
    await missions.submit_attempt(db, attempt, mission, body.answers, body.ai_usage, llm, info)
    finish_ai_call(usage, attempt.engine, info)
    await db.commit()
    return await missions.result_view(db, attempt, mission)


@router.get("/{attempt_id}/result", response_model=ResultOut, summary="Ambil ulang hasil (nota penilaian)")
async def get_result(attempt_id: uuid.UUID, db: DB, user: CurrentUser):
    attempt, mission = await _attempt_and_mission(db, user, attempt_id)
    if attempt.status != "graded":
        raise ApiError(409, "not_graded", "Percobaan ini belum dikumpulkan.")
    return await missions.result_view(db, attempt, mission)


@router.post(
    "/{attempt_id}/appeal",
    status_code=201,
    response_model=AppealOut,
    summary="Ajukan banding, masuk antrean review manusia",
)
async def appeal(attempt_id: uuid.UUID, body: AppealIn, db: DB, user: CurrentUser):
    attempt = await missions.get_owned_attempt(db, user, attempt_id)
    if attempt.status != "graded":
        raise ApiError(409, "not_graded", "Hanya percobaan yang sudah dinilai yang bisa dibanding.")
    existing = await db.scalar(
        select(Review.id).where(Review.attempt_id == attempt.id, Review.kind == "appeal")
    )
    if existing:
        raise ApiError(409, "already_appealed", "Banding untuk percobaan ini sudah diajukan.")
    review = Review(
        attempt_id=attempt.id,
        kind="appeal",
        reason=body.reason.strip(),
        status="pending",
        original_score=attempt.score,
    )
    db.add(review)
    attempt.review_status = "appealed"
    await db.commit()
    return {"review_id": review.id, "status": review.status}

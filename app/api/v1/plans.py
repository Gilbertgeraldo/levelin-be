import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.sse import EventSourceResponse, ServerSentEvent

from app.api.deps import DB, LLM, CurrentUser
from app.core.errors import ApiError
from app.core.ratelimit import begin_ai_call, finish_ai_call
from app.models import AIUsage, Plan
from app.schemas.plans import PlanCreate, PlanOut, PlanPatch
from app.services import plans, prompts
from app.services.audits import get_owned_audit
from app.services.guardrails import SentenceGuard
from app.services.llm import LLMCallInfo
from app.services.streaming import stream_with_fallback

router = APIRouter(prefix="/plans", tags=["Rencana Naik Level"])


@router.post("", status_code=201, response_model=PlanOut, summary="Buat rencana dari {audit_id, direction}")
async def create_plan(body: PlanCreate, db: DB, user: CurrentUser):
    audit = await get_owned_audit(db, user, body.audit_id)
    plan = await plans.create_plan(db, user, audit, body.direction)
    await db.commit()
    return await plans.plan_view(db, plan)


@router.get("/current", response_model=PlanOut, summary="Rencana aktif plus progres per fase")
async def current_plan(db: DB, user: CurrentUser):
    plan = await plans.active_plan(db, user.id)
    if plan is None:
        raise ApiError(404, "no_plan", "Belum ada rencana aktif. Buat rencana dari Peta Tugas.")
    return await plans.plan_view(db, plan)


@router.patch("/{plan_id}", response_model=PlanOut, summary="Ganti arah: perkuat, naik, atau pindah")
async def patch_plan(plan_id: uuid.UUID, body: PlanPatch, db: DB, user: CurrentUser):
    plan = await plans.get_owned_plan(db, user, plan_id)
    if plan.status != "active":
        raise ApiError(409, "plan_archived", "Rencana ini sudah diarsipkan.")
    await plans.regenerate(db, user, plan, body.direction)
    await db.commit()
    return await plans.plan_view(db, plan)


async def _narrative_ctx(plan_id: uuid.UUID, db: DB, user: CurrentUser) -> tuple[Plan, AIUsage]:
    plan = await plans.get_owned_plan(db, user, plan_id)
    usage = await begin_ai_call(db, user.id, "narrative")
    return plan, usage


@router.post(
    "/{plan_id}/narrative",
    response_class=EventSourceResponse,
    summary="Narasi rencana oleh LLM ringan (SSE)",
    description="Event: meta {engine}, delta {text}, reset {reason}, done {engine}.",
)
async def narrative(
    ctx: Annotated[tuple[Plan, AIUsage], Depends(_narrative_ctx)], db: DB, user: CurrentUser, llm: LLM
) -> AsyncIterator[ServerSentEvent]:
    plan, usage = ctx
    view = await plans.plan_view(db, plan)
    facts = plans.narrative_facts(view, user)
    await db.commit()  # lepas koneksi selama stream
    info = LLMCallInfo()

    def llm_stream():
        return llm.stream_text(
            tier="light",
            system=prompts.PLAN_NARRATIVE_SYSTEM,
            messages=[{"role": "user", "content": facts}],
            max_tokens=2500,
            effort="low",
            info=info,
        )

    async def on_complete(text: str, engine: str, flagged: bool):
        finish_ai_call(usage, engine, info)
        await db.commit()

    async for event in stream_with_fallback(
        llm_stream=llm_stream if llm.available else None,
        fallback_text=lambda: plans.narrative_fallback(view, user),
        guard=SentenceGuard(),
        on_complete=on_complete,
    ):
        yield event

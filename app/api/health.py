from fastapi import APIRouter, Response
from sqlalchemy import text

from app.api.deps import DB, LLM, Embeddings
from app.core.config import get_settings

router = APIRouter(tags=["Operasional"])


@router.get("/health", summary="Cek hidup server, database, dan penyedia LLM")
async def health(response: Response, db: DB, llm: LLM, embeddings: Embeddings):
    settings = get_settings()
    try:
        await db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:  # noqa: BLE001 - laporkan status apa pun penyebabnya
        database = "error"

    llm_status = {
        "provider": llm.provider,
        "configured": llm.available,
        "model_main": llm.model_for("main"),
        "model_light": llm.model_for("light"),
        "reachable": await llm.reachable() if llm.available else None,
    }
    status = "ok"
    if database != "ok":
        status = "error"
        response.status_code = 503
    elif not llm.available or llm_status["reachable"] is False:
        status = "degraded"
    return {
        "status": status,
        "version": settings.app_version,
        "database": database,
        "llm": llm_status,
        "embeddings": {"model": embeddings.primary_model},
    }

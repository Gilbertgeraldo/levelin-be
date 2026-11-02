import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.api.router import api_v1
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.db.session import engine

DESCRIPTION = """\
Backend Levelin untuk aplikasi pekerja (Flutter) dan dashboard HR (web).

- Auth: `Authorization: Bearer <JWT Supabase>`. Peran: `worker`, `hr_admin`, `reviewer`.
- Error: `{"error": {"code": "...", "message": "..."}}`.
- Skor dihitung di server; kunci jawaban misi tidak pernah dikirim ke aplikasi.
- Endpoint AI punya cadangan berbasis aturan; respons menandai `engine` (`llm` / `fallback`).
- Streaming memakai SSE dengan event `meta`, `delta`, `reset`, `done`.
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    origins = settings.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After", "X-Unread-Count", "Content-Disposition"],
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(api_v1)

    @app.get("/", include_in_schema=False)
    async def root():
        return {"name": settings.app_name, "version": settings.app_version, "docs": "/docs"}

    return app


app = create_app()

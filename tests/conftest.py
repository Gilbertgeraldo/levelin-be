import os
import tempfile

# Harus diset sebelum modul app di-import (get_settings di-cache)
os.environ.update(
    {
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "SUPABASE_JWT_SECRET": "test-secret-yang-cukup-panjang-untuk-hs256",
        "LLM_PROVIDER": "gemini",
        "GEMINI_API_KEY": "",
        "GROQ_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "VOYAGE_API_KEY": "",
        "MIDTRANS_SERVER_KEY": "",
        "SUPABASE_URL": "",
        "SUPABASE_SERVICE_ROLE_KEY": "",
        "FCM_SERVICE_ACCOUNT_FILE": "",
        "REVIEW_SAMPLE_RATE": "0",
        "CORS_ORIGINS": "*",
        "UPLOAD_DIR": tempfile.mkdtemp(prefix="levelin-uploads-"),
    }
)

import json  # noqa: E402
import re  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool, StaticPool  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.session import build_engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.embeddings import EmbeddingService, HashingEmbedder, get_embeddings  # noqa: E402
from app.services.llm import LLMError, get_llm  # noqa: E402
from scripts.dev_token import make_token  # noqa: E402
from scripts.seed_demo import seed_catalog, seed_reference  # noqa: E402

WORKER_EMAIL = "budi@contoh-sejahtera.test"
HR_EMAIL = "hr@contoh-sejahtera.test"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def sessionmaker():
    # Default SQLite in-memory; set TEST_DATABASE_URL untuk menjalankan tes di Postgres + pgvector
    pg_url = os.environ.get("TEST_DATABASE_URL")
    if pg_url:
        engine = build_engine(pg_url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
    else:
        engine = build_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as db:
        await seed_reference(db)
        await seed_catalog(db)
        await db.commit()
    yield maker
    await engine.dispose()


class NoLLM:
    """LLM tidak tersedia: semua endpoint AI harus jalan lewat cadangan."""

    available = False
    provider = "none"

    def model_for(self, tier):
        return "none"

    async def generate_json(self, **kwargs):
        raise LLMError("tidak dikonfigurasi")

    async def stream_text(self, **kwargs):
        raise LLMError("tidak dikonfigurasi")
        yield  # pragma: no cover


class FakeLLM:
    """LLM palsu yang bisa diatur untuk menguji jalur 'llm' dan validasinya."""

    available = True

    def __init__(self, json_by_model: dict | None = None, chunks: list[str] | None = None, fail_after: int | None = None):
        self.json_by_model = json_by_model or {}
        self.chunks = chunks or []
        self.fail_after = fail_after
        self.calls: list[dict] = []

    async def generate_json(self, *, model_cls, info=None, **kwargs):
        self.calls.append({"kind": "json", "model_cls": model_cls.__name__, **kwargs})
        if info is not None:
            info.model, info.input_tokens, info.output_tokens = "fake-model", 100, 50
        data = self.json_by_model.get(model_cls.__name__)
        if data is None:
            raise LLMError("tidak ada respons palsu")
        try:
            return model_cls.model_validate(data)
        except Exception as exc:  # meniru validasi skema yang gagal
            raise LLMError("skema") from exc

    async def stream_text(self, *, info=None, **kwargs) -> AsyncIterator[str]:
        self.calls.append({"kind": "stream", **kwargs})
        for i, chunk in enumerate(self.chunks):
            if self.fail_after is not None and i == self.fail_after:
                raise LLMError("putus di tengah")
            yield chunk
        if info is not None:
            info.model = "fake-model"


class FakePrimaryEmbedder(HashingEmbedder):
    """Penyedia embedding 'utama' palsu supaya audit bisa bertanda engine=llm."""

    name = "fake-embed"

    async def embed(self, texts, input_type):  # noqa: ARG002
        return HashingEmbedder.embed(self, texts)


@pytest.fixture
async def client(sessionmaker):
    async def _db():
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_llm] = lambda: NoLLM()
    app.dependency_overrides[get_embeddings] = lambda: EmbeddingService()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def use_llm(llm) -> None:
    app.dependency_overrides[get_llm] = lambda: llm


def use_fake_primary_embeddings() -> None:
    def _make():
        service = EmbeddingService()
        service.primary = FakePrimaryEmbedder(service.settings.embedding_dim)
        return service

    app.dependency_overrides[get_embeddings] = _make


def auth(email: str = WORKER_EMAIL, role: str | None = None, name: str | None = "Budi Santoso") -> dict:
    return {"Authorization": f"Bearer {make_token(email, name, role, 1)}"}


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in re.split(r"\r?\n\r?\n", text.strip()):
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if event:
            events.append((event, data))
    return events


def sse_text(events: list[tuple[str, dict]]) -> str:
    return "".join(d["text"] for e, d in events if e == "delta")

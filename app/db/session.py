import uuid
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


def build_engine(url: str | None = None, **kwargs) -> AsyncEngine:
    settings = get_settings()
    url = url or settings.async_database_url
    connect_args: dict = {}
    if url.startswith("postgresql+asyncpg") and settings.db_pgbouncer:
        # Pooler mode transaksi tidak mendukung prepared statement bernama tetap.
        connect_args = {
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
        }
        # Cache statement milik SQLAlchemy juga harus mati, kalau tidak query gagal
        # "prepared statement ... does not exist" saat koneksi berpindah di pooler.
        url = make_url(url).update_query_dict({"prepared_statement_cache_size": "0"}).render_as_string(hide_password=False)
    engine = create_async_engine(
        url,
        echo=settings.db_echo,
        pool_pre_ping=not url.startswith("sqlite"),
        connect_args=connect_args,
        **kwargs,
    )
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_fk_on(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


engine = build_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection

from alembic import context
from app import models  # noqa: F401 - mendaftarkan semua tabel ke metadata
from app.db.base import Base
from app.db.session import build_engine
from app.db.types import UTCDateTime, Vector

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Tampilkan tipe kustom sebagai app.db.types.* di berkas migrasi."""
    if type_ == "type" and isinstance(obj, Vector | UTCDateTime):
        autogen_context.imports.add("import app.db.types")
        return f"app.db.types.{obj!r}"
    return False


def include_object(obj, name, type_, reflected, compare_to):
    """Indeks HNSW/trigram dibuat lewat SQL mentah di migrasi, bukan dari model."""
    if type_ == "index" and reflected and compare_to is None and name and name.endswith(("_hnsw", "_trgm")):
        return False
    return True


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_item=render_item,
        include_object=include_object,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    from app.core.config import get_settings

    _configure(url=get_settings().async_database_url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    sqlite = connection.dialect.name == "sqlite"
    if sqlite:
        # Batch mode SQLite membuat ulang tabel; cek foreign key harus mati supaya tabel berisi data aman
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        # PRAGMA memulai transaksi implisit; tutup dulu agar migrasi berjalan di transaksinya sendiri
        connection.commit()
    _configure(connection=connection, render_as_batch=sqlite)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    from app.core.config import get_settings

    url = config.attributes.get("database_url") or get_settings().session_database_url
    engine = build_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

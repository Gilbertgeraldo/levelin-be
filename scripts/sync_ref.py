"""Sinkronkan data referensi dari schema `ref` (ESCO, KBJI 2014, Lightcast) ke tabel publik backend.

    uv run python -m scripts.sync_ref [--env-file .env.production]

Untuk database Supabase produksi, gunakan koneksi session pooler (port 5432) lewat
env `SYNC_DATABASE_URL` atau biarkan skrip mengonversi port 6543 -> 5432 otomatis.
"""

import argparse
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import ingest
from app.core.config import Settings


async def main(env_file: str) -> None:
    settings = Settings(_env_file=env_file)
    engine = create_async_engine(settings.session_database_url, echo=settings.db_echo)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with session_factory() as db:
            stats = await ingest.sync_ref(db)
            await db.commit()
        print(f"Sinkronisasi ref selesai: {stats}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=".env", help="Berkas env konfigurasi (default: .env)")
    args = parser.parse_args()
    asyncio.run(main(args.env_file))

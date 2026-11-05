"""Hitung embedding jabatan dan template tugas (yang belum ada atau beda model).

    uv run python -m scripts.embed_reference [--force] [--batch-size 64] [--env-file .env.production]

Diproses per batch dan di-commit setiap batch selesai, sehingga aman dilanjutkan bila terputus.
"""

import argparse
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import ingest
from app.core.config import Settings
from app.services.embeddings import EmbeddingService


async def main(force: bool, batch_size: int, env_file: str) -> None:
    settings = Settings(_env_file=env_file)
    engine = create_async_engine(settings.session_database_url, echo=settings.db_echo)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    embeddings = EmbeddingService(settings)
    try:
        async with session_factory() as db:
            counts = await ingest.embed_reference(
                db,
                embeddings,
                force=force,
                batch_size=batch_size,
                commit_per_batch=True,
            )
            await db.commit()
        print(f"Embedding diperbarui: {counts}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="Hitung ulang semua")
    parser.add_argument("--batch-size", type=int, default=64, help="Ukuran batch per commit (default: 64)")
    parser.add_argument("--env-file", default=".env", help="Berkas env konfigurasi (default: .env)")
    args = parser.parse_args()
    asyncio.run(main(args.force, args.batch_size, args.env_file))

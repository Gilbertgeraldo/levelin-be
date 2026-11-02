"""Hitung embedding jabatan dan template tugas (yang belum ada atau beda model).

    uv run python -m scripts.embed_reference [--force]
"""

import argparse
import asyncio

from app import ingest
from app.db.session import SessionLocal
from app.services.embeddings import get_embeddings


async def main(force: bool) -> None:
    async with SessionLocal() as db:
        counts = await ingest.embed_reference(db, get_embeddings(), force=force)
        await db.commit()
    print(f"Embedding diperbarui: {counts}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="Hitung ulang semua")
    asyncio.run(main(parser.parse_args().force))

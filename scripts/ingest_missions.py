"""Impor/ubah misi di bank misi dari berkas JSON ({"missions": [...]}, lihat data/demo/missions.json).

    uv run python -m scripts.ingest_missions data/missions.json
"""

import argparse
import asyncio

from app import ingest
from app.db.session import SessionLocal
from scripts._common import load_json


async def main(path: str) -> None:
    rows = load_json(path)["missions"]
    async with SessionLocal() as db:
        n = await ingest.upsert_missions(db, rows)
        await db.commit()
    print(f"Misi diimpor/diubah: {n}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path")
    asyncio.run(main(parser.parse_args().path))

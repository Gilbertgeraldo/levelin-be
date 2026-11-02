"""Impor jabatan KBJI 2026 (BPS) beserta pemetaan ke ISCO-08.

    uv run python -m scripts.ingest_kbji data/kbji.csv

Kolom CSV: kbji_code, title, isco_code, [title_en], [description], [aliases] (dipisah ";").
"""

import argparse
import asyncio

from app import ingest
from app.db.session import SessionLocal
from scripts._common import read_csv, split_list


async def main(path: str, delimiter: str) -> None:
    rows = read_csv(path, delimiter)
    occupations = []
    for row in rows:
        if not row.get("kbji_code") or not row.get("isco_code") or not row.get("title"):
            continue
        occupations.append(
            {
                "code": row["kbji_code"],
                "isco_code": row["isco_code"][:4],
                "title": row["title"],
                "title_en": row.get("title_en") or None,
                "description": row.get("description") or None,
                "aliases": split_list(row.get("aliases")),
            }
        )
    async with SessionLocal() as db:
        n = await ingest.upsert_occupations(db, occupations)
        await db.commit()
    print(f"Jabatan diimpor: {n} (dilewati: {len(rows) - n})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv")
    parser.add_argument("--delimiter", default=",")
    args = parser.parse_args()
    asyncio.run(main(args.csv, args.delimiter))

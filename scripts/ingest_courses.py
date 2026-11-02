"""Impor katalog kelas.

    uv run python -m scripts.ingest_courses data/courses.csv
    uv run python -m scripts.ingest_courses data/courses.json

CSV: title, provider, url, price, [duration_hours], [level], [language], [rating],
[is_partner] (true/false), [affiliate_code], [description], skills (label/URI ESCO dipisah ";").
JSON: {"courses": [...]} dengan kunci yang sama dan skills berupa list.
"""

import argparse
import asyncio

from app import ingest
from app.db.session import SessionLocal
from scripts._common import load_json, read_csv, split_list


def _num(value: str | None, cast=float):
    return cast(value) if value not in (None, "") else None


async def main(path: str) -> None:
    if path.endswith(".json"):
        rows = load_json(path)["courses"]
    else:
        rows = [
            {
                **r,
                "price": int(float(r.get("price") or 0)),
                "duration_hours": _num(r.get("duration_hours")),
                "rating": _num(r.get("rating")),
                "is_partner": (r.get("is_partner") or "").lower() in ("1", "true", "ya", "yes"),
                "skills": split_list(r.get("skills")),
            }
            for r in read_csv(path)
            if r.get("url") and r.get("title")
        ]
    async with SessionLocal() as db:
        n = await ingest.upsert_courses(db, rows)
        await db.commit()
    print(f"Kelas diimpor: {n}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path")
    asyncio.run(main(parser.parse_args().path))

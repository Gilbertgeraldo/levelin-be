"""Impor skor paparan AI generatif per tugas dari indeks ILO.

    uv run python -m scripts.ingest_ilo data/ilo_tasks.csv \
        --isco-col isco08_4digit --task-col task --score-col score \
        [--translation data/ilo_tasks_id.csv] [--occupations data/ilo_occupations.csv]

- CSV tugas: satu baris per tugas. Nama kolom bisa diatur lewat argumen.
- --translation (opsional): kolom task_en, task_id untuk label Bahasa Indonesia.
- --occupations (opsional): kolom isco_code, mean_score, gradient (0-4) per jabatan.
  Tanpa berkas ini avg_exposure dihitung dari rata-rata skor tugas.
- Kolom opsional `kind` (data|dok|hr|layan|koord|fisik) menghubungkan tugas ke misi latihan; tanpa itu
  Skor Kesiapan hanya naik lewat tugas yang ditandai "sudah pakai AI".
"""

import argparse
import asyncio
from collections import defaultdict

from sqlalchemy import select

from app import ingest
from app.db.session import SessionLocal
from app.models import Occupation
from scripts._common import read_csv


async def main(args) -> None:
    rows = read_csv(args.csv, args.delimiter)
    translations = {}
    if args.translation:
        translations = {r["task_en"]: r["task_id"] for r in read_csv(args.translation) if r.get("task_id")}

    tasks: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        isco = (row.get(args.isco_col) or "")[:4]
        task_en = row.get(args.task_col)
        try:
            score = float(row.get(args.score_col, ""))
        except ValueError:
            continue
        if not isco or not task_en:
            continue
        tasks[isco].append(
            {
                # Id tugas yang dipakai aplikasi, mis. "4311-1" (urutan dalam berkas)
                "code": f"{isco}-{len(tasks[isco]) + 1}",
                "description": translations.get(task_en, task_en),
                "description_en": task_en,
                "ilo_score": max(0.0, min(1.0, score)),
                "kind": row.get("kind") or None,
            }
        )

    async with SessionLocal() as db:
        total = 0
        for isco, items in tasks.items():
            total += await ingest.replace_tasks(db, isco, items, source=args.source)
        if args.occupations:
            for row in read_csv(args.occupations):
                occs = (await db.scalars(select(Occupation).where(Occupation.isco_code == row["isco_code"][:4]))).all()
                for occ in occs:
                    if row.get("mean_score"):
                        occ.avg_exposure = float(row["mean_score"])
                    if row.get("gradient"):
                        occ.exposure_gradient = int(row["gradient"])
        await ingest.recompute_avg_exposure(db, only_missing=True)
        await db.commit()
    untranslated = sum(1 for items in tasks.values() for t in items if t["description"] == t["description_en"])
    print(f"Tugas diimpor: {total} untuk {len(tasks)} kode ISCO; tanpa terjemahan: {untranslated}")
    print("Jalankan `python -m scripts.embed_reference` setelah impor.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv")
    parser.add_argument("--isco-col", default="isco_code")
    parser.add_argument("--task-col", default="task")
    parser.add_argument("--score-col", default="score")
    parser.add_argument("--translation")
    parser.add_argument("--occupations")
    parser.add_argument("--source", default="ILO 2025")
    parser.add_argument("--delimiter", default=",")
    asyncio.run(main(parser.parse_args()))

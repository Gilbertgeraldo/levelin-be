"""Impor skill ESCO dan relasinya ke kode ISCO.

    uv run python -m scripts.ingest_esco \
        --skills data/esco/skills_en.csv \
        --occupations data/esco/occupations_en.csv \
        --relations data/esco/occupationSkillRelations_en.csv \
        [--labels-id data/esco/skills_id.csv] [--top 25]

Berkas berasal dari unduhan CSV resmi ESCO. ESCO tidak menyediakan Bahasa Indonesia, jadi
--labels-id (kolom conceptUri, label_id) dipakai untuk label tampilan; tanpa itu label Inggris.
Per kode ISCO, skill diurutkan menurut berapa banyak okupasi ESCO di grup itu yang
menganggapnya essential; --top teratas jadi "essential", sisanya (sampai 2x top) "optional".
"""

import argparse
import asyncio
from collections import Counter, defaultdict

from app import ingest
from app.db.session import SessionLocal
from scripts._common import read_csv


async def main(args) -> None:
    labels_id = {}
    if args.labels_id:
        labels_id = {r["conceptUri"]: r["label_id"] for r in read_csv(args.labels_id) if r.get("label_id")}

    skills_rows = read_csv(args.skills)
    skill_info = {
        r["conceptUri"]: {
            "esco_uri": r["conceptUri"],
            "label": labels_id.get(r["conceptUri"], r.get("preferredLabel", "")),
            "label_en": r.get("preferredLabel"),
            "category": "digital" if "digital" in (r.get("skillType", "") + r.get("reuseLevel", "")).lower() else "teknis",
            "description": (r.get("description") or "")[:2000] or None,
        }
        for r in skills_rows
        if r.get("conceptUri") and r.get("preferredLabel")
    }

    occ_to_isco = {r["conceptUri"]: (r.get("iscoGroup") or "")[:4] for r in read_csv(args.occupations)}
    essential: dict[str, Counter] = defaultdict(Counter)
    optional: dict[str, Counter] = defaultdict(Counter)
    for rel in read_csv(args.relations):
        isco = occ_to_isco.get(rel.get("occupationUri"))
        uri = rel.get("skillUri")
        if not isco or uri not in skill_info:
            continue
        (essential if rel.get("relationType") == "essential" else optional)[isco][uri] += 1

    used = set()
    plan: dict[str, list[tuple[str, str]]] = {}
    for isco in set(essential) | set(optional):
        top_e = [u for u, _ in essential[isco].most_common(args.top)]
        rest = [u for u, _ in (essential[isco] + optional[isco]).most_common(args.top * 3) if u not in top_e]
        plan[isco] = [(u, "essential") for u in top_e] + [(u, "optional") for u in rest[: args.top]]
        used.update(u for u, _ in plan[isco])

    async with SessionLocal() as db:
        ids = await ingest.upsert_skills(db, [skill_info[u] for u in sorted(used)])
        for isco, links in plan.items():
            await ingest.set_occupation_skills(db, isco, [(ids[u], rel) for u, rel in links])
        await db.commit()
    print(f"Skill diimpor: {len(used)}; kode ISCO dengan relasi: {len(plan)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skills", required=True)
    parser.add_argument("--occupations", required=True)
    parser.add_argument("--relations", required=True)
    parser.add_argument("--labels-id")
    parser.add_argument("--top", type=int, default=25)
    asyncio.run(main(parser.parse_args()))

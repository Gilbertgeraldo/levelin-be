"""Isi database dengan data demo hacking day.

    uv run python -m scripts.seed_demo                    # data referensi, kelas, misi, lowongan
    uv run python -m scripts.seed_demo --sample-workers 15  # + pekerja contoh untuk dashboard HR

Skor ILO di data demo ilustratif; lihat data/demo/reference.json.
"""

import argparse
import asyncio
import random
import uuid
from datetime import timedelta

from sqlalchemy import select

from app import ingest
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models import Attempt, Invitation, Mission, Occupation, OccupationTask, PassportSettings, Profile, User
from app.services.audits import create_audit
from app.services.embeddings import get_embeddings
from app.services.llm import LLMCallInfo, LLMService
from app.services.missions import award_passport
from scripts._common import DEMO_DIR, load_json

DEPARTMENTS = {
    "Operasional": "4110",
    "Keuangan": "4311",
    "Layanan Pelanggan": "4222",
}


async def seed_reference(db) -> None:
    ref = load_json(DEMO_DIR / "reference.json")
    skill_ids = await ingest.upsert_skills(db, ref["skills"])
    await ingest.upsert_occupations(db, ref["occupations"])
    for occ in ref["occupations"]:
        await ingest.replace_tasks(db, occ["isco_code"], occ["tasks"], source="DEMO (ilustratif)")
        links = [(skill_ids[label], "essential") for label in occ["skills"].get("essential", [])]
        links += [(skill_ids[label], "optional") for label in occ["skills"].get("optional", [])]
        await ingest.set_occupation_skills(db, occ["isco_code"], links)
    await ingest.recompute_avg_exposure(db, only_missing=False)
    print(f"  jabatan: {len(ref['occupations'])}, skill: {len(ref['skills'])}")


async def seed_catalog(db) -> dict:
    n_courses = await ingest.upsert_courses(db, load_json(DEMO_DIR / "courses.json")["courses"])
    n_missions = await ingest.upsert_missions(db, load_json(DEMO_DIR / "missions.json")["missions"])
    jobs = load_json(DEMO_DIR / "jobs.json")
    orgs = {o["slug"]: await ingest.upsert_organization(db, o["name"], o["slug"]) for o in jobs["organizations"]}
    n_jobs = await ingest.upsert_jobs(db, jobs["jobs"], orgs)
    for inv in jobs["invitations"]:
        exists = await db.scalar(select(Invitation).where(Invitation.email == inv["email"]))
        if exists is None:
            db.add(
                Invitation(
                    org_id=orgs[inv["org_slug"]].id,
                    email=inv["email"],
                    full_name=inv.get("full_name"),
                    department=inv.get("department"),
                    role=inv.get("role", "worker"),
                    token=uuid.uuid4().hex,
                    status="pending",
                    expires_at=utcnow() + timedelta(days=365),
                )
            )
    print(f"  kelas: {n_courses}, misi: {n_missions}, lowongan: {n_jobs}, undangan demo: {len(jobs['invitations'])}")
    return orgs


async def seed_sample_workers(db, org, count: int) -> None:
    """Pekerja contoh berisi audit & misi supaya dashboard HR lolos ambang k-anonim."""
    rng = random.Random(42)
    llm = LLMService()
    embeddings = get_embeddings()
    missions = list((await db.scalars(select(Mission))).all())
    names = ["Ayu", "Bagus", "Cahya", "Dewi", "Eko", "Fajar", "Gita", "Hadi", "Indah", "Joko", "Kiki", "Lina",
             "Made", "Nur", "Oki", "Putri", "Rizky", "Sari", "Tono", "Wulan"]
    depts = list(DEPARTMENTS)
    for i in range(count):
        dept = depts[i % len(depts)]
        email = f"contoh{i + 1}@contoh-sejahtera.test"
        if await db.scalar(select(User.id).where(User.email == email)):
            continue
        user = User(
            id=uuid.uuid5(uuid.NAMESPACE_URL, email),
            email=email,
            full_name=f"{names[i % len(names)]} Contoh",
            role="worker",
            org_id=org.id,
            department=dept,
            last_active_at=utcnow() - timedelta(days=rng.randint(0, 20)),
        )
        db.add(user)
        await db.flush()  # baris users harus ada sebelum tabel anak (tanpa relationship ORM)
        occupation = await db.get(Occupation, DEPARTMENTS[dept])
        db.add(Profile(user_id=user.id, occupation_code=occupation.code, consent_data_processing=True,
                       consent_version="1.0", consented_at=utcnow()))
        db.add(PassportSettings(user_id=user.id, share_with_org=rng.random() < 0.6, public_link=False))
        await db.flush()

        templates = list((await db.scalars(
            select(OccupationTask).where(OccupationTask.isco_code == occupation.isco_code)
        )).all())
        chosen = rng.sample(templates, k=min(len(templates), rng.randint(4, 6)))
        selected = [
            {"template_task_id": t.id, "hours_per_week": float(rng.choice([3, 4, 5, 6, 8, 10])),
             "uses_ai": rng.random() < 0.35}
            for t in chosen
        ]
        audit = await create_audit(db, user, occupation_code=occupation.code, story=None, selected=selected,
                                   llm=llm, embeddings=embeddings, info=LLMCallInfo())
        audit.created_at = utcnow() - timedelta(days=rng.randint(0, 120))

        relevant = [m for m in missions if occupation.isco_code in (m.occupation_isco_codes or [])]
        for mission in relevant[: rng.randint(0, len(relevant))]:
            score = float(rng.randint(55, 95))
            attempt = Attempt(user_id=user.id, mission_id=mission.id, status="graded", score=score,
                              passed=score >= mission.pass_score, engine="llm", submitted_at=utcnow(),
                              answers={}, result={"objective": None, "rubric": None, "skills_earned": []})
            db.add(attempt)
            await db.flush()
            if attempt.passed:
                await award_passport(db, attempt, mission)
    print(f"  pekerja contoh: {count} di {org.name}")


async def main(sample_workers: int, embed: bool) -> None:
    async with SessionLocal() as db:
        print("Seed data demo Levelin")
        await seed_reference(db)
        orgs = await seed_catalog(db)
        if embed:
            counts = await ingest.embed_reference(db, get_embeddings())
            print(f"  embedding: {counts}")
        if sample_workers:
            await seed_sample_workers(db, orgs["contoh-sejahtera"], sample_workers)
        await db.commit()
    print("Selesai.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample-workers", type=int, default=0, help="Jumlah pekerja contoh di organisasi demo")
    parser.add_argument("--no-embed", action="store_true", help="Lewati perhitungan embedding")
    args = parser.parse_args()
    asyncio.run(main(args.sample_workers, not args.no_embed))

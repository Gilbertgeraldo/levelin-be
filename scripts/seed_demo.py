"""Isi database dengan data Levelin dari prototipe Tim Hearz.

    uv run python -m scripts.seed_demo                      # data referensi, kelas, misi, lowongan
    uv run python -m scripts.seed_demo --sample-workers     # + 48 karyawan contoh PT Sinar Distribusi

Data referensi (jabatan, tugas ILO-NASK, skill, misi, kelas) adalah data asli tim. Organisasi,
karyawan, dan lowongan contoh fiktif. Akun demo memakai id dari scripts.dev_token (uuid5 email),
jadi hanya berlaku dengan token dev.
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
from app.models import (
    Attempt,
    Invitation,
    Mission,
    Occupation,
    OccupationTask,
    PassportSettings,
    Profile,
    ReadinessSnapshot,
    TeamMembership,
    User,
)
from app.services import scoring
from app.services.audits import create_audit, recompute
from app.services.dev_auth import DEMO_EMAIL_DOMAIN
from app.services.embeddings import get_embeddings
from app.services.llm import LLMCallInfo, LLMService
from app.services.missions import award_passport
from scripts._common import ROOT, load_json

DATA_DIR = ROOT / "data" / "hearz"
ORG_SLUG = "sinar-distribusi"
EMAIL_DOMAIN = DEMO_EMAIL_DOMAIN

# Jabatan contoh per departemen (dari DEPTS prototipe)
DEPT_OCCUPATIONS = {
    "Keuangan & Akuntansi": ["4311", "4313"],
    "Operasional & Logistik": ["4323", "4110", "4132"],
    "Layanan Pelanggan": ["4222", "4226"],
    "HR & GA": ["4416", "4120"],
    "Gudang": ["4321"],
}
DEMO_ACCOUNTS = [
    {"email": f"hr@{EMAIL_DOMAIN}", "full_name": "Hana Pratiwi", "department": "HR & GA", "role": "hr_admin"},
    {"email": f"ratna@{EMAIL_DOMAIN}", "full_name": "Ratna Wijaya", "department": "Keuangan & Akuntansi", "role": "manager"},
    {"email": f"rina@{EMAIL_DOMAIN}", "full_name": "Rina Lestari", "department": "Keuangan & Akuntansi", "role": "worker"},
]
FIRST_NAMES = [
    "Ayu", "Bagus", "Cahya", "Dewi", "Eko", "Fajar", "Gita", "Hadi", "Indah", "Joko", "Kiki", "Lina", "Made",
    "Nur", "Oki", "Putri", "Rizky", "Sari", "Tono", "Wulan", "Yoga", "Zahra", "Agus", "Bunga",
]


def user_id_for(email: str) -> uuid.UUID:
    """Sama dengan sub di scripts.dev_token, supaya akun demo bisa langsung dipakai."""
    return uuid.uuid5(uuid.NAMESPACE_URL, email)


async def seed_reference(db) -> None:
    ref = load_json(DATA_DIR / "reference.json")
    skill_ids = await ingest.upsert_skills(db, [{**s, "category": "ai" if s["code"] == "ai" else "teknis"} for s in ref["skills"]])
    await ingest.upsert_occupations(db, ref["occupations"])
    for occ in ref["occupations"]:
        await ingest.replace_tasks(db, occ["isco_code"], occ["tasks"], source="ILO-NASK 2025 (terjemahan Tim Hearz)")
        links = [(skill_ids[c], "base") for c in occ.get("base_skills", [])]
        links += [(skill_ids[c], "essential") for c in occ.get("target_skills", [])]
        await ingest.set_occupation_skills(db, occ["isco_code"], links)
    await ingest.upsert_kinds(db, ref["kinds"])
    print(f"  jabatan: {len(ref['occupations'])}, skill: {len(ref['skills'])}, jenis tugas: {len(ref['kinds'])}")


async def seed_catalog(db) -> dict:
    n_courses = await ingest.upsert_courses(db, load_json(DATA_DIR / "courses.json")["courses"])
    n_missions = await ingest.upsert_missions(db, load_json(DATA_DIR / "missions.json")["missions"])
    jobs = load_json(DATA_DIR / "jobs.json")
    orgs = {o["slug"]: await ingest.upsert_organization(db, o["name"], o["slug"]) for o in jobs["organizations"]}
    n_jobs = await ingest.upsert_jobs(db, jobs["jobs"], orgs)
    for account in DEMO_ACCOUNTS:
        if await db.scalar(select(Invitation.id).where(Invitation.email == account["email"])):
            continue
        db.add(
            Invitation(
                org_id=orgs[ORG_SLUG].id,
                email=account["email"],
                full_name=account["full_name"],
                department=account["department"],
                role=account["role"],
                token=uuid.uuid4().hex,
                status="pending",
                expires_at=utcnow() + timedelta(days=365),
            )
        )
    print(f"  kelas: {n_courses}, misi: {n_missions}, lowongan: {n_jobs}, undangan akun demo: {len(DEMO_ACCOUNTS)}")
    return orgs


async def _ensure_user(db, email: str, full_name: str, role: str, org, department: str) -> tuple[User, bool]:
    user = await db.get(User, user_id_for(email))
    if user is not None:
        return user, False
    user = User(
        id=user_id_for(email), email=email, full_name=full_name, role=role, org_id=org.id, department=department
    )
    db.add(user)
    await db.flush()  # baris users harus ada sebelum tabel anak (tanpa relationship ORM)
    invitation = await db.scalar(select(Invitation).where(Invitation.email == email, Invitation.status == "pending"))
    if invitation is not None:
        invitation.status, invitation.accepted_at = "accepted", utcnow()
    return user, True


async def seed_sample_workers(db, org) -> None:
    """48 karyawan contoh sesuai DEPTS prototipe; sekitar dua pertiga sudah audit dan berlatih misi."""
    rng = random.Random(42)
    llm, embeddings = LLMService(), get_embeddings()
    depts = load_json(DATA_DIR / "jobs.json")["departments"]
    missions = {m.kind: m for m in (await db.scalars(select(Mission))).all() if m.kind}

    manager, _ = await _ensure_user(
        db, f"ratna@{EMAIL_DOMAIN}", "Ratna Wijaya", "manager", org, "Keuangan & Akuntansi"
    )
    months = [(utcnow().replace(day=1) - timedelta(days=31 * back)).strftime("%Y-%m") for back in (2, 1, 0)]

    created = 0
    index = 0
    for dept in depts:
        for n in range(dept["headcount"]):
            index += 1
            email = f"karyawan{index:02d}@{EMAIL_DOMAIN}"
            name = f"{FIRST_NAMES[index % len(FIRST_NAMES)]} {chr(65 + n % 26)}."
            user, is_new = await _ensure_user(db, email, name, "worker", org, dept["name"])
            if not is_new:
                continue
            created += 1
            code = DEPT_OCCUPATIONS[dept["name"]][n % len(DEPT_OCCUPATIONS[dept["name"]])]
            occupation = await db.get(Occupation, code)
            db.add(Profile(user_id=user.id, occupation_code=code, consent_data_processing=True,
                           consent_version="1.0", consented_at=utcnow(), goal=rng.choice(["perkuat", "naik", "pindah"])))
            db.add(PassportSettings(user_id=user.id, share_with_org=rng.random() < 0.25, public_link=False))
            in_team = dept["name"] == "Keuangan & Akuntansi" and n < 7
            if in_team:
                db.add(TeamMembership(manager_id=manager.id, member_id=user.id))
            await db.flush()
            # Minimal 6 dari 7 anggota tim demo sudah audit supaya ringkasan tim lolos ambang k
            if rng.random() > 0.66 and not (in_team and n < 6):
                continue  # belum audit

            templates = list((await db.scalars(
                select(OccupationTask).where(OccupationTask.isco_code == code, OccupationTask.is_candidate.is_(True))
            )).all())
            chosen = rng.sample(templates, k=min(len(templates), rng.randint(3, 5)))
            selected = [{"ilo_task_id": t.code, "hours_per_week": float(rng.choice([2, 3, 4, 5, 6, 8]))} for t in chosen]
            audit = await create_audit(db, user, occupation=occupation, story=None, template_tasks=selected,
                                       llm=llm, embeddings=embeddings, info=LLMCallInfo())
            for task in audit.tasks:
                task.uses_ai = rng.random() < 0.3

            # Misi yang sudah lulus untuk jenis tugas yang terpapar
            for kind_code in {t.template.kind for t in audit.tasks if t.template and t.group in scoring.EXPOSED}:
                mission = missions.get(kind_code)
                if mission is None or rng.random() > 0.45:
                    continue
                attempt = Attempt(user_id=user.id, mission_id=mission.id, status="graded", passed=True,
                                  rubric_avg=3.25, score=81.0, engine="llm", submitted_at=utcnow(), answers={},
                                  result={"checks": [], "rubric": None, "skills_earned": []})
                db.add(attempt)
                await db.flush()
                await award_passport(db, attempt, mission, [])
            await recompute(db, audit)
            # Riwayat skor dua bulan sebelumnya untuk grafik tren
            for back, month in enumerate(months[:2]):
                score = max(0, audit.readiness_score - rng.randint(8, 16) * (2 - back))
                db.add(ReadinessSnapshot(user_id=user.id, month=month, score=score))
    print(f"  karyawan contoh baru: {created} di {org.name} (atasan demo: ratna@{EMAIL_DOMAIN})")


async def main(sample_workers: bool, embed: bool) -> None:
    async with SessionLocal() as db:
        print("Seed data Levelin (data Tim Hearz)")
        await seed_reference(db)
        orgs = await seed_catalog(db)
        if embed:
            counts = await ingest.embed_reference(db, get_embeddings())
            print(f"  embedding: {counts}")
        if sample_workers:
            await seed_sample_workers(db, orgs[ORG_SLUG])
        await db.commit()
    print("Selesai.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample-workers", action="store_true", help="Tambah 48 karyawan contoh dan tim atasan demo")
    parser.add_argument("--no-embed", action="store_true", help="Lewati perhitungan embedding")
    args = parser.parse_args()
    asyncio.run(main(args.sample_workers, not args.no_embed))

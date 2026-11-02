"""Fungsi impor data referensi. Dipanggil oleh scripts/ingest_*.py dan scripts/seed_demo.py.

Semua fungsi idempoten (upsert) supaya skrip bisa dijalankan ulang.
"""

import logging
from collections import defaultdict

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Course,
    Job,
    Mission,
    Occupation,
    OccupationSkill,
    OccupationTask,
    Organization,
    Skill,
    TaskKind,
)
from app.schemas.admin import MissionAdminIn
from app.services.embeddings import EmbeddingService

logger = logging.getLogger("levelin.ingest")

OCCUPATION_FIELDS = (
    "title_en",
    "description",
    "avg_exposure",
    "exposure_gradient",
    "gradient_cat",
    "naik_code",
    "pindah_code",
)


async def upsert_occupations(db: AsyncSession, rows: list[dict]) -> int:
    for row in rows:
        occ = await db.get(Occupation, row["code"])
        if occ is None:
            occ = Occupation(code=row["code"])
            db.add(occ)
        occ.isco_code = row["isco_code"]
        occ.title = row["title"]
        occ.aliases = row.get("aliases") or []
        occ.is_target = bool(row.get("is_target", False))
        for key in OCCUPATION_FIELDS:
            if key in row:
                setattr(occ, key, row[key])
    await db.flush()
    return len(rows)


async def replace_tasks(db: AsyncSession, isco_code: str, tasks: list[dict], source: str = "ILO") -> int:
    """Ganti seluruh template tugas satu kode ISCO. Tugas audit lama kehilangan tautan (SET NULL)."""
    await db.execute(delete(OccupationTask).where(OccupationTask.isco_code == isco_code))
    for t in tasks:
        db.add(
            OccupationTask(
                code=t.get("code"),
                isco_code=isco_code,
                description=t["description"],
                description_en=t.get("description_en"),
                ilo_score=float(t["ilo_score"]),
                kind=t.get("kind"),
                is_candidate=bool(t.get("candidate", True)),
                source=t.get("source", source),
            )
        )
    await db.flush()
    return len(tasks)


async def recompute_avg_exposure(db: AsyncSession, only_missing: bool = True) -> None:
    """Isi avg_exposure jabatan dari rata-rata skor template tugasnya."""
    tasks = (await db.scalars(select(OccupationTask))).all()
    scores: dict[str, list[float]] = defaultdict(list)
    for t in tasks:
        scores[t.isco_code].append(t.ilo_score)
    for occ in (await db.scalars(select(Occupation))).all():
        if only_missing and occ.avg_exposure is not None:
            continue
        values = scores.get(occ.isco_code)
        if values:
            occ.avg_exposure = round(sum(values) / len(values), 3)
    await db.flush()


async def upsert_skills(db: AsyncSession, rows: list[dict]) -> dict[str, int]:
    """Kunci: code, lalu esco_uri, lalu label. Mengembalikan peta kunci -> id."""
    ids: dict[str, int] = {}
    for row in rows:
        skill = None
        if row.get("code"):
            skill = await db.scalar(select(Skill).where(Skill.code == row["code"]))
        if skill is None and row.get("esco_uri"):
            skill = await db.scalar(select(Skill).where(Skill.esco_uri == row["esco_uri"]))
        if skill is None:
            skill = await db.scalar(select(Skill).where(Skill.label == row["label"]))
        if skill is None:
            skill = Skill(label=row["label"])
            db.add(skill)
        skill.label = row["label"]
        skill.label_en = row.get("label_en")
        skill.code = row.get("code") or skill.code
        skill.esco_uri = row.get("esco_uri") or skill.esco_uri
        skill.category = row.get("category", "ai" if row.get("code") == "ai" else "teknis")
        skill.description = row.get("description")
        await db.flush()
        for key in (row.get("code"), row.get("esco_uri"), row["label"]):
            if key:
                ids[key] = skill.id
    return ids


async def upsert_kinds(db: AsyncSession, rows: list[dict]) -> int:
    for row in rows:
        kind = await db.get(TaskKind, row["code"])
        if kind is None:
            kind = TaskKind(code=row["code"])
            db.add(kind)
        kind.label = row["label"]
        kind.skill_codes = row.get("skill_codes", [])
        kind.mission_slug = row.get("mission_slug")
    await db.flush()
    return len(rows)


async def set_occupation_skills(db: AsyncSession, isco_code: str, links: list[tuple[int, str]]) -> None:
    await db.execute(delete(OccupationSkill).where(OccupationSkill.isco_code == isco_code))
    seen: set[int] = set()
    for skill_id, relation in links:
        if skill_id in seen:
            continue
        seen.add(skill_id)
        db.add(OccupationSkill(isco_code=isco_code, skill_id=skill_id, relation=relation))
    await db.flush()


async def _skills_by_keys(db: AsyncSession, keys: list[str]) -> list[Skill]:
    """Cari skill berdasarkan kode, label, atau URI ESCO, dengan urutan sesuai masukan."""
    if not keys:
        return []
    found = (
        await db.scalars(
            select(Skill).where(or_(Skill.code.in_(keys), Skill.label.in_(keys), Skill.esco_uri.in_(keys)))
        )
    ).all()
    by_key = {}
    for s in found:
        for key in (s.code, s.label, s.esco_uri):
            if key:
                by_key[key] = s
    missing = [k for k in keys if k not in by_key]
    if missing:
        logger.warning("Skill tidak dikenal, dilewati: %s", missing)
    ordered: list[Skill] = []
    for key in keys:
        if key in by_key and by_key[key] not in ordered:
            ordered.append(by_key[key])
    return ordered


async def upsert_courses(db: AsyncSession, rows: list[dict]) -> int:
    for row in rows:
        course = await db.scalar(select(Course).where(Course.url == row["url"]))
        if course is None:
            course = Course(url=row["url"])
            db.add(course)
        for key in ("title", "provider", "description", "duration_hours", "level", "rating", "affiliate_code", "format"):
            setattr(course, key, row.get(key))
        course.price = int(row.get("price", 0))
        course.language = row.get("language", "id")
        course.is_partner = bool(row.get("is_partner", False))
        course.is_active = bool(row.get("is_active", True))
        course.skills = await _skills_by_keys(db, row.get("skills", []))
    await db.flush()
    return len(rows)


async def upsert_missions(db: AsyncSession, rows: list[dict]) -> int:
    """Baris misi memakai bentuk MissionAdminIn, dengan `skills` berupa kode/label/URI ESCO."""
    for row in rows:
        skills = await _skills_by_keys(db, row.get("skills", []))
        payload = MissionAdminIn.model_validate(
            {**{k: v for k, v in row.items() if k != "skills"}, "skill_ids": [s.id for s in skills]}
        )
        mission = await db.scalar(select(Mission).where(Mission.slug == payload.slug))
        if mission is None:
            mission = Mission()
            db.add(mission)
        apply_mission_payload(mission, payload)
        mission.skills = skills
    await db.flush()
    return len(rows)


def apply_mission_payload(mission: Mission, payload: MissionAdminIn) -> None:
    data = payload.model_dump(exclude={"skill_ids", "custom_request_id"})
    for key in ("answer_fields", "rubric", "answer_key"):
        data[key] = [{k: v for k, v in item.items() if v is not None} for item in data[key]]
    for key, value in data.items():
        setattr(mission, key, value)


async def upsert_organization(db: AsyncSession, name: str, slug: str) -> Organization:
    org = await db.scalar(select(Organization).where(Organization.slug == slug))
    if org is None:
        org = Organization(name=name, slug=slug)
        db.add(org)
        await db.flush()
    return org


async def upsert_jobs(db: AsyncSession, rows: list[dict], org_by_slug: dict[str, Organization]) -> int:
    for row in rows:
        org = org_by_slug.get(row.get("org_slug")) if row.get("org_slug") else None
        job = await db.scalar(select(Job).where(Job.title == row["title"], Job.company == row["company"]))
        if job is None:
            job = Job(title=row["title"], company=row["company"])
            db.add(job)
        occ = await db.get(Occupation, row["occupation_code"]) if row.get("occupation_code") else None
        job.org_id = org.id if org else None
        job.location = row.get("location")
        job.employment_type = row.get("employment_type")
        job.description = row.get("description")
        job.url = row.get("url")
        job.source = "internal" if org else row.get("source", "mitra")
        job.source_verified = bool(row.get("source_verified", org is not None))
        job.occupation_code = occ.code if occ else None
        job.isco_code = occ.isco_code if occ else None
        job.salary_min = row.get("salary_min")
        job.salary_max = row.get("salary_max")
        job.is_active = True
        job.skills = await _skills_by_keys(db, row.get("skills", []))
    await db.flush()
    return len(rows)


async def embed_reference(db: AsyncSession, embeddings: EmbeddingService, *, force: bool = False) -> dict:
    """Hitung embedding jabatan dan template tugas yang belum punya (atau model berbeda)."""
    model = embeddings.primary_model
    counts = {"occupations": 0, "tasks": 0}

    occs = [
        o for o in (await db.scalars(select(Occupation))).all()
        if force or o.embedding is None or o.embedding_model != model
    ]
    texts = [" | ".join([o.title, o.title_en or "", *(o.aliases or [])]) for o in occs]
    result = await embeddings.embed(texts, "document")
    for o, vec in zip(occs, result.vectors, strict=True):
        o.embedding, o.embedding_model = vec, result.model
    counts["occupations"] = len(occs)

    tasks = [
        t for t in (await db.scalars(select(OccupationTask))).all()
        if force or t.embedding is None or t.embedding_model != model
    ]
    result = await embeddings.embed([t.description for t in tasks], "document")
    for t, vec in zip(tasks, result.vectors, strict=True):
        t.embedding, t.embedding_model = vec, result.model
    counts["tasks"] = len(tasks)
    await db.flush()
    counts["model"] = result.model
    return counts

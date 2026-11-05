"""Fungsi impor dan sinkronisasi data referensi.

Dipanggil oleh scripts/sync_ref.py, scripts/ingest_*.py, dan scripts/seed_demo.py.
Semua fungsi idempoten (upsert) supaya skrip bisa dijalankan ulang.
"""

import json
import logging
from collections import defaultdict

from sqlalchemy import delete, insert, or_, select, text, update
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
    "esco_uri",
    "label_id",
    "title_en",
    "description",
    "avg_exposure",
    "exposure_gradient",
    "gradient_cat",
    "naik_code",
    "pindah_code",
)

AI_SKILL_KEYWORDS = (
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "large language model",
    "generative ai",
    "prompt engineering",
    "natural language processing",
    "computer vision",
    "neural network",
)


def _as_list(val) -> list[str]:
    if not val:
        return []
    if isinstance(val, list | tuple):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except ValueError:
            return [x.strip() for x in val.split(";") if x.strip()]
    return []


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        clean = (item or "").strip()
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            out.append(clean)
    return out


def build_occupation_search_text(
    title: str | None,
    label_id: str | None,
    title_en: str | None,
    aliases: list[str] | None,
    extra: list[str] | None = None,
) -> str:
    parts = _dedup([title or "", label_id or "", title_en or "", *(aliases or []), *(extra or [])])
    return " | ".join(parts)


def infer_skill_category(skill_type: str | None, reuse_level: str | None, label: str | None) -> str:
    st = (skill_type or "").lower()
    rl = (reuse_level or "").lower()
    lbl = (label or "").lower()
    if any(k in lbl for k in AI_SKILL_KEYWORDS):
        return "ai"
    if "transversal" in rl or "transversal" in st:
        return "transversal"
    if "digital" in st or "digital" in rl:
        return "digital"
    return "teknis"


async def upsert_occupations(db: AsyncSession, rows: list[dict]) -> int:
    for row in rows:
        occ = await db.get(Occupation, row["code"])
        if occ is None:
            occ = Occupation(code=row["code"])
            db.add(occ)
        occ.isco_code = row["isco_code"]
        if "label_id" in row and row["label_id"]:
            occ.label_id = row["label_id"]
        occ.title = occ.label_id or row["title"]
        occ.aliases = row.get("aliases") or []
        occ.is_target = bool(row.get("is_target", False))
        for key in OCCUPATION_FIELDS:
            if key in row:
                setattr(occ, key, row[key])
        occ.search_text = build_occupation_search_text(
            occ.title, occ.label_id, occ.title_en, occ.aliases
        )
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
        if row.get("label_id"):
            skill.label_id = row["label_id"]
        skill.label = skill.label_id or row["label"]
        skill.label_en = row.get("label_en")
        skill.alt_labels = row.get("alt_labels") or skill.alt_labels or []
        skill.code = row.get("code") or skill.code
        skill.esco_uri = row.get("esco_uri") or skill.esco_uri
        skill.category = row.get("category", "ai" if row.get("code") == "ai" else "teknis")
        skill.description = row.get("description")
        await db.flush()
        for key in (row.get("code"), row.get("esco_uri"), row["label"], skill.label):
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


async def set_occupation_skills(
    db: AsyncSession, isco_code: str, links: list[tuple[int, str]], occupation_code: str | None = None
) -> None:
    occ_code = occupation_code or isco_code
    await db.execute(delete(OccupationSkill).where(OccupationSkill.occupation_code == occ_code))
    seen: set[int] = set()
    for skill_id, relation in links:
        if skill_id in seen:
            continue
        seen.add(skill_id)
        db.add(
            OccupationSkill(
                occupation_code=occ_code,
                isco_code=isco_code,
                skill_id=skill_id,
                relation=relation,
            )
        )
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


async def sync_ref(db: AsyncSession) -> dict:
    """Sinkronkan data dari schema `ref` (ESCO, KBJI 2014, Lightcast) ke tabel publik milik backend.

    Idempoten, berjalan dalam transaksi aktif, dan tidak pernah menghapus jabatan atau skill
    yang sudah ada supaya foreign key dari profil, audit, misi, rencana, dan paspor tetap aman.
    """
    # 1. Baca fallback skill_type dari ref.esco_occupation_skills (karena ref.esco_skills bisa NULL)
    rel_skill_types: dict[str, str] = {}
    for uri, st in (
        await db.execute(
            text(
                "SELECT DISTINCT skill_uri, skill_type FROM ref.esco_occupation_skills "
                "WHERE skill_type IS NOT NULL"
            )
        )
    ).all():
        rel_skill_types[uri] = st

    # 2. Sinkronkan ref.esco_skills (+ ref.lightcast_skills bila terisi) ke public.skills
    existing_skills = (
        await db.execute(
            select(
                Skill.id,
                Skill.code,
                Skill.esco_uri,
                Skill.label,
                Skill.label_id,
                Skill.label_en,
                Skill.category,
                Skill.description,
            )
        )
    ).all()
    skill_by_uri = {r.esco_uri: r for r in existing_skills if r.esco_uri}
    skill_by_label_en = {(r.label_en or "").lower(): r for r in existing_skills if r.label_en}

    esco_skills_rows = (
        await db.execute(
            text(
                "SELECT uri, skill_type, reuse_level, label, alt_labels, description "
                "FROM ref.esco_skills WHERE uri IS NOT NULL AND label IS NOT NULL"
            )
        )
    ).all()

    skills_to_insert: list[dict] = []
    skills_to_update: list[dict] = []
    seen_skill_uris: set[str] = set()

    for uri, skill_type, reuse_level, label, alt_labels, description in esco_skills_rows:
        if uri in seen_skill_uris:
            continue
        seen_skill_uris.add(uri)
        clean_label = label.strip()[:255]
        if not clean_label:
            continue
        alt_list = _as_list(alt_labels)
        cat = infer_skill_category(skill_type or rel_skill_types.get(uri), reuse_level, clean_label)
        desc = (description or "").strip()[:2000] or None

        existing = skill_by_uri.get(uri)
        if existing is not None:
            new_label = existing.label_id or clean_label
            new_cat = cat if cat != "teknis" else (existing.category or "teknis")
            new_desc = desc or existing.description
            if (
                existing.label != new_label
                or existing.label_en != clean_label
                or existing.category != new_cat
                or existing.description != new_desc
            ):
                skills_to_update.append(
                    {
                        "id": existing.id,
                        "label": new_label,
                        "label_en": clean_label,
                        "alt_labels": alt_list,
                        "category": new_cat,
                        "description": new_desc,
                    }
                )
        else:
            skills_to_insert.append(
                {
                    "esco_uri": uri,
                    "label": clean_label,
                    "label_id": None,
                    "label_en": clean_label,
                    "alt_labels": alt_list,
                    "category": cat,
                    "description": desc,
                }
            )

    # Lightcast Open Skills (opsional, mungkin kosong)
    lightcast_rows = (
        await db.execute(
            text(
                "SELECT id, name, type_name, category, description "
                "FROM ref.lightcast_skills WHERE id IS NOT NULL AND name IS NOT NULL"
            )
        )
    ).all()
    for lc_id, lc_name, lc_type, lc_cat, lc_desc in lightcast_rows:
        lc_uri = f"lightcast:{lc_id}"
        if lc_uri in seen_skill_uris:
            continue
        seen_skill_uris.add(lc_uri)
        clean_name = lc_name.strip()[:255]
        if not clean_name:
            continue
        cat = "transversal" if "common" in (lc_type or "").lower() else infer_skill_category(lc_type, lc_cat, clean_name)
        desc = (lc_desc or "").strip()[:2000] or None
        existing = skill_by_uri.get(lc_uri) or skill_by_label_en.get(clean_name.lower())
        if existing is not None:
            if desc and not existing.description:
                skills_to_update.append(
                    {
                        "id": existing.id,
                        "label": existing.label_id or existing.label,
                        "label_en": existing.label_en or clean_name,
                        "alt_labels": [],
                        "category": existing.category or cat,
                        "description": desc,
                    }
                )
        else:
            skills_to_insert.append(
                {
                    "esco_uri": lc_uri,
                    "label": clean_name,
                    "label_id": None,
                    "label_en": clean_name,
                    "alt_labels": [],
                    "category": cat,
                    "description": desc,
                }
            )

    for start in range(0, len(skills_to_insert), 500):
        await db.execute(insert(Skill).values(skills_to_insert[start : start + 500]))
    for start in range(0, len(skills_to_update), 1000):
        await db.execute(update(Skill), skills_to_update[start : start + 1000])
    await db.flush()

    skill_id_by_uri = {
        uri: sid
        for sid, uri in (
            await db.execute(select(Skill.id, Skill.esco_uri).where(Skill.esco_uri.is_not(None)))
        ).all()
        if uri
    }

    # 3. Baca grup ISCO 4 digit & KBJI 2014 (bila terisi)
    isco_groups: dict[str, dict] = {}
    for code, label, alt_labels, description in (
        await db.execute(
            text(
                "SELECT code, label, alt_labels, description "
                "FROM ref.esco_isco_groups WHERE length(code) = 4"
            )
        )
    ).all():
        isco_groups[code] = {
            "label": label,
            "alt_labels": _as_list(alt_labels),
            "description": description,
        }

    kbji_rows = (
        await db.execute(
            text(
                "SELECT code, title, description, level, isco4 "
                "FROM ref.kbji WHERE level >= 4 AND code IS NOT NULL AND title IS NOT NULL"
            )
        )
    ).all()
    kbji_by_isco4: dict[str, list[dict]] = defaultdict(list)
    for k_code, k_title, k_desc, k_level, k_isco4 in kbji_rows:
        isco4 = (k_isco4 or k_code)[:4]
        kbji_by_isco4[isco4].append(
            {"code": k_code.strip()[:16], "title": k_title.strip()[:255], "description": k_desc, "isco4": isco4}
        )

    # 4. Peta metrik paparan ILO per kode ISCO 4 digit dari data yang sudah ada
    existing_occs = (await db.scalars(select(Occupation))).all()
    occ_by_code = {o.code: o for o in existing_occs}
    occ_by_uri = {o.esco_uri: o for o in existing_occs if o.esco_uri}

    ilo_by_isco: dict[str, dict] = {}
    for o in existing_occs:
        if o.avg_exposure is not None and o.isco_code not in ilo_by_isco:
            ilo_by_isco[o.isco_code] = {
                "avg_exposure": o.avg_exposure,
                "exposure_gradient": o.exposure_gradient,
                "gradient_cat": o.gradient_cat,
            }
    task_scores: dict[str, list[float]] = defaultdict(list)
    for isco, score in (await db.execute(select(OccupationTask.isco_code, OccupationTask.ilo_score))).all():
        task_scores[isco].append(score)
    for isco, vals in task_scores.items():
        if isco not in ilo_by_isco and vals:
            ilo_by_isco[isco] = {
                "avg_exposure": round(sum(vals) / len(vals), 3),
                "exposure_gradient": None,
                "gradient_cat": None,
            }

    # 5. Sinkronkan ref.esco_occupations (+ ref.kbji bila ada) ke public.occupations
    esco_occ_rows = (
        await db.execute(
            text(
                "SELECT uri, code, isco_group, label, alt_labels, description, definition "
                "FROM ref.esco_occupations WHERE uri IS NOT NULL AND label IS NOT NULL"
            )
        )
    ).all()

    occs_to_insert: list[dict] = []
    occs_to_update: list[dict] = []
    occ_uri_to_code_isco: dict[str, tuple[str, str]] = {}
    esco_labels_by_isco: dict[str, list[str]] = defaultdict(list)
    seen_occ_codes: set[str] = set()

    for uri, raw_code, isco_group, label, alt_labels, description, definition in esco_occ_rows:
        code = (raw_code or uri.rsplit("/", 1)[-1]).strip()[:16]
        if not code or code in seen_occ_codes:
            continue
        seen_occ_codes.add(code)
        isco_code = (isco_group or code.split(".")[0] or "").strip()[:4]
        clean_label = label.strip()[:255]
        occ_uri_to_code_isco[uri] = (code, isco_code)
        esco_labels_by_isco[isco_code].append(clean_label)

        kbji_titles = [k["title"] for k in kbji_by_isco4.get(isco_code, [])]
        isco_grp_label = isco_groups.get(isco_code, {}).get("label")
        aliases = _dedup(
            [
                *_as_list(alt_labels),
                *kbji_titles,
                *([isco_grp_label] if isco_grp_label and isco_grp_label.lower() != clean_label.lower() else []),
            ]
        )
        desc = (description or definition or "").strip() or None
        ilo = ilo_by_isco.get(isco_code, {})

        existing = occ_by_code.get(code) or occ_by_uri.get(uri)
        if existing is not None:
            target_code = existing.code
            occ_uri_to_code_isco[uri] = (target_code, isco_code)
            title = existing.label_id or clean_label
            search_text = build_occupation_search_text(title, existing.label_id, clean_label, aliases)
            new_desc = existing.description if existing.label_id and existing.description else (desc or existing.description)
            new_avg = existing.avg_exposure if existing.avg_exposure is not None else ilo.get("avg_exposure")
            new_grad = existing.exposure_gradient if existing.exposure_gradient is not None else ilo.get("exposure_gradient")
            new_cat = existing.gradient_cat or ilo.get("gradient_cat")
            if (
                existing.esco_uri != uri
                or existing.isco_code != isco_code
                or existing.title != title
                or existing.title_en != clean_label
                or existing.description != new_desc
                or (existing.aliases or []) != aliases
                or existing.search_text != search_text
                or existing.avg_exposure != new_avg
            ):
                occs_to_update.append(
                    {
                        "code": target_code,
                        "esco_uri": uri,
                        "isco_code": isco_code,
                        "title": title,
                        "title_en": clean_label,
                        "description": new_desc,
                        "aliases": aliases,
                        "search_text": search_text,
                        "avg_exposure": new_avg,
                        "exposure_gradient": new_grad,
                        "gradient_cat": new_cat,
                    }
                )
        else:
            search_text = build_occupation_search_text(clean_label, None, clean_label, aliases)
            occs_to_insert.append(
                {
                    "code": code,
                    "esco_uri": uri,
                    "isco_code": isco_code,
                    "title": clean_label,
                    "label_id": None,
                    "title_en": clean_label,
                    "description": desc,
                    "aliases": aliases,
                    "search_text": search_text,
                    "avg_exposure": ilo.get("avg_exposure"),
                    "exposure_gradient": ilo.get("exposure_gradient"),
                    "gradient_cat": ilo.get("gradient_cat"),
                    "is_target": False,
                }
            )

    # Sinkronkan baris ref.kbji (bila ref.kbji sudah diisi)
    kbji_synced = 0
    for isco4, k_list in kbji_by_isco4.items():
        for k in k_list:
            code = k["code"]
            if code in seen_occ_codes:
                continue
            seen_occ_codes.add(code)
            kbji_synced += 1
            ilo = ilo_by_isco.get(isco4, {})
            isco_grp_label = isco_groups.get(isco4, {}).get("label")
            esco_names = esco_labels_by_isco.get(isco4, [])[:15]
            existing = occ_by_code.get(code)
            if existing is not None:
                label_id = existing.label_id or k["title"]
                title = label_id
                title_en = existing.title_en or isco_grp_label
                aliases = _dedup([*(existing.aliases or []), *esco_names])
                search_text = build_occupation_search_text(title, label_id, title_en, aliases)
                occs_to_update.append(
                    {
                        "code": code,
                        "esco_uri": existing.esco_uri,
                        "isco_code": isco4,
                        "title": title,
                        "label_id": label_id,
                        "title_en": title_en,
                        "description": existing.description or k["description"],
                        "aliases": aliases,
                        "search_text": search_text,
                        "avg_exposure": existing.avg_exposure if existing.avg_exposure is not None else ilo.get("avg_exposure"),
                        "exposure_gradient": existing.exposure_gradient if existing.exposure_gradient is not None else ilo.get("exposure_gradient"),
                        "gradient_cat": existing.gradient_cat or ilo.get("gradient_cat"),
                    }
                )
            else:
                aliases = _dedup(esco_names)
                search_text = build_occupation_search_text(k["title"], k["title"], isco_grp_label, aliases)
                occs_to_insert.append(
                    {
                        "code": code,
                        "esco_uri": None,
                        "isco_code": isco4,
                        "title": k["title"],
                        "label_id": k["title"],
                        "title_en": isco_grp_label,
                        "description": k["description"],
                        "aliases": aliases,
                        "search_text": search_text,
                        "avg_exposure": ilo.get("avg_exposure"),
                        "exposure_gradient": ilo.get("exposure_gradient"),
                        "gradient_cat": ilo.get("gradient_cat"),
                        "is_target": False,
                    }
                )

    # Pastikan jabatan bawaan (Tim Hearz) yang belum tersentuh juga punya search_text lengkap
    for o in existing_occs:
        if o.code not in seen_occ_codes:
            extra_esco = esco_labels_by_isco.get(o.isco_code, [])[:15]
            o.search_text = build_occupation_search_text(
                o.title, o.label_id, o.title_en, o.aliases or [], extra=extra_esco
            )

    for start in range(0, len(occs_to_insert), 500):
        await db.execute(insert(Occupation).values(occs_to_insert[start : start + 500]))
    for start in range(0, len(occs_to_update), 500):
        await db.execute(update(Occupation), occs_to_update[start : start + 500])
    await db.flush()

    # 6. Sinkronkan ref.esco_occupation_skills ke public.occupation_skills
    rel_rows = (
        await db.execute(
            text("SELECT occupation_uri, skill_uri, relation_type FROM ref.esco_occupation_skills")
        )
    ).all()

    links_by_occ: dict[tuple[str, int], tuple[str, str]] = {}
    synced_esco_codes: set[str] = set()
    for occ_uri, skill_uri, rel_type in rel_rows:
        occ_info = occ_uri_to_code_isco.get(occ_uri)
        skill_id = skill_id_by_uri.get(skill_uri)
        if not occ_info or not skill_id:
            continue
        occ_code, isco_code = occ_info
        synced_esco_codes.add(occ_code)
        relation = "essential" if rel_type == "essential" else "optional"
        key = (occ_code, skill_id)
        if key not in links_by_occ or links_by_occ[key][1] != "essential":
            links_by_occ[key] = (isco_code, relation)

    if synced_esco_codes:
        existing_links = {
            (occ_c, sid): (isco_c, rel)
            for occ_c, isco_c, sid, rel in (
                await db.execute(
                    select(
                        OccupationSkill.occupation_code,
                        OccupationSkill.isco_code,
                        OccupationSkill.skill_id,
                        OccupationSkill.relation,
                    ).where(OccupationSkill.occupation_code.in_(sorted(synced_esco_codes)))
                )
            ).all()
        }
        if existing_links != links_by_occ:
            changed_codes = sorted(
                {occ_c for (occ_c, _), val in links_by_occ.items() if existing_links.get((occ_c, _)) != val}
                | {occ_c for (occ_c, _) in set(existing_links) - set(links_by_occ)}
            )
            for start in range(0, len(changed_codes), 500):
                chunk = changed_codes[start : start + 500]
                await db.execute(delete(OccupationSkill).where(OccupationSkill.occupation_code.in_(chunk)))
            changed_set = set(changed_codes)
            link_dicts = [
                {
                    "occupation_code": occ_code,
                    "isco_code": isco_code,
                    "skill_id": skill_id,
                    "relation": relation,
                }
                for (occ_code, skill_id), (isco_code, relation) in links_by_occ.items()
                if occ_code in changed_set
            ]
            for start in range(0, len(link_dicts), 1000):
                await db.execute(insert(OccupationSkill).values(link_dicts[start : start + 1000]))

    await db.flush()
    return {
        "esco_occupations": len(esco_occ_rows),
        "kbji_occupations": kbji_synced,
        "occupations_inserted": len(occs_to_insert),
        "occupations_updated": len(occs_to_update),
        "esco_skills": len(esco_skills_rows),
        "lightcast_skills": len(lightcast_rows),
        "skills_inserted": len(skills_to_insert),
        "skills_updated": len(skills_to_update),
        "occupation_skill_links": len(links_by_occ),
    }


async def embed_reference(
    db: AsyncSession,
    embeddings: EmbeddingService,
    *,
    force: bool = False,
    batch_size: int = 64,
    commit_per_batch: bool = False,
) -> dict:
    """Hitung embedding jabatan dan template tugas dalam batch; bisa dilanjutkan bila terputus."""
    model = embeddings.primary_model
    counts = {"occupations": 0, "tasks": 0, "model": model}

    occs = [
        o
        for o in (await db.scalars(select(Occupation).order_by(Occupation.code))).all()
        if force or o.embedding is None or o.embedding_model != model
    ]
    for start in range(0, len(occs), batch_size):
        batch = occs[start : start + batch_size]
        texts = [
            " | ".join(
                _dedup([o.title, o.label_id or "", o.title_en or "", *(o.aliases or [])[:15]])
            )
            for o in batch
        ]
        result = await embeddings.embed(texts, "document")
        for o, vec in zip(batch, result.vectors, strict=True):
            o.embedding, o.embedding_model = vec, result.model
        counts["occupations"] += len(batch)
        counts["model"] = result.model
        await db.flush()
        if commit_per_batch:
            await db.commit()
        logger.info("Embedding jabatan: %d/%d (%s)", counts["occupations"], len(occs), result.model)

    tasks = [
        t
        for t in (await db.scalars(select(OccupationTask).order_by(OccupationTask.id))).all()
        if force or t.embedding is None or t.embedding_model != model
    ]
    for start in range(0, len(tasks), batch_size):
        batch = tasks[start : start + batch_size]
        result = await embeddings.embed([t.description for t in batch], "document")
        for t, vec in zip(batch, result.vectors, strict=True):
            t.embedding, t.embedding_model = vec, result.model
        counts["tasks"] += len(batch)
        counts["model"] = result.model
        await db.flush()
        if commit_per_batch:
            await db.commit()
        logger.info("Embedding tugas: %d/%d (%s)", counts["tasks"], len(tasks), result.model)

    return counts

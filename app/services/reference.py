import difflib
from collections import defaultdict

from sqlalchemy import String, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Occupation, OccupationSkill, Skill
from app.services.embeddings import EmbeddingService, cosine
from app.services.textutil import normalize

CANDIDATE_LIMIT = 50

_trgm_installed: bool | None = None


async def has_trgm(db: AsyncSession) -> bool:
    """Ekstensi pg_trgm opsional; dicek sekali per proses."""
    global _trgm_installed
    if _trgm_installed is None:
        _trgm_installed = bool(await db.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")))
    return _trgm_installed


def occupation_view(o: Occupation, match: str | None = None) -> dict:
    view = {
        "code": o.code,
        "isco_code": o.isco_code,
        "title": o.title,
        "title_en": o.title_en,
        "avg_exposure": o.avg_exposure,
        "exposure_gradient": o.exposure_gradient,
        "gradient_cat": o.gradient_cat,
    }
    if match:
        view["match"] = match
    return view


async def search_occupations(
    db: AsyncSession, embeddings: EmbeddingService, q: str, limit: int, *, postgres: bool
) -> list[dict]:
    q = (q or "").strip()
    onboarding = Occupation.is_target.is_(False)
    if not q:
        rows = (await db.scalars(select(Occupation).where(onboarding).order_by(Occupation.title).limit(limit))).all()
        return [occupation_view(o) for o in rows]

    like = f"%{q.lower()}%"
    conditions = [
        func.lower(Occupation.title).like(like),
        func.lower(Occupation.title_en).like(like),
        func.lower(cast(Occupation.aliases, String)).like(like),
        Occupation.code.like(f"{q}%"),
        Occupation.isco_code.like(f"{q}%"),
    ]
    if postgres and await has_trgm(db):
        # Toleran salah ketik
        conditions.append(func.similarity(Occupation.title, q) > 0.3)
    candidates = (
        await db.scalars(select(Occupation).where(onboarding, or_(*conditions)).limit(CANDIDATE_LIMIT))
    ).all()

    nq = normalize(q)

    def rank(o: Occupation) -> tuple:
        names = [o.title, o.title_en or "", *(o.aliases or [])]
        normed = [normalize(n) for n in names if n]
        starts = any(n.startswith(nq) for n in normed)
        ratio = max((difflib.SequenceMatcher(None, nq, n).ratio() for n in normed), default=0)
        return (not starts, -ratio, o.title)

    results = [occupation_view(o, "text") for o in sorted(candidates, key=rank)[:limit]]
    if len(results) >= limit:
        return results

    # Lengkapi dengan pencarian semantik bila hasil teks kurang
    seen = {r["code"] for r in results}
    query = await embeddings.embed([q], "query")
    vec = query.vectors[0]
    if postgres:
        stmt = (
            select(Occupation)
            .where(onboarding, Occupation.embedding_model == query.model, Occupation.embedding.is_not(None))
            .order_by(Occupation.embedding.op("<=>")(vec))
            .limit(limit)
        )
        semantic = list((await db.scalars(stmt)).all())
    else:
        pool = (
            await db.scalars(
                select(Occupation).where(
                    onboarding, Occupation.embedding_model == query.model, Occupation.embedding.is_not(None)
                )
            )
        ).all()
        semantic = sorted(pool, key=lambda o: -cosine(vec, o.embedding))[:limit]
    for o in semantic:
        if o.code not in seen and len(results) < limit:
            results.append(occupation_view(o, "semantic"))
    return results


async def next_roles(db: AsyncSession, occupation: Occupation, limit: int = 5) -> list[dict]:
    """Jabatan tujuan: arah "naik" dan "pindah" dari data tim, lalu jabatan lain yang skill-nya paling mirip."""
    out: list[dict] = []
    for direction, code in (("naik", occupation.naik_code), ("pindah", occupation.pindah_code)):
        target = await db.get(Occupation, code) if code else None
        if target is None:
            continue
        skills = (
            await db.execute(
                select(Skill.id, Skill.code, Skill.label)
                .join(OccupationSkill, OccupationSkill.skill_id == Skill.id)
                .where(OccupationSkill.isco_code == target.isco_code, OccupationSkill.relation == "essential")
            )
        ).all()
        out.append(
            {
                "direction": direction,
                "occupation": occupation_view(target),
                "similarity": None,
                "skills_to_learn": [{"id": i, "code": c, "label": lbl} for i, c, lbl in skills],
            }
        )
    seen = {occupation.code} | {r["occupation"]["code"] for r in out}
    for row in await similar_roles(db, occupation, limit):
        if row["occupation"]["code"] not in seen and len(out) < limit:
            out.append(row)
    return out


async def similar_roles(db: AsyncSession, occupation: Occupation, limit: int) -> list[dict]:
    """Kemiripan Jaccard himpunan skill (ESCO) antar kode ISCO."""
    rows = (await db.execute(select(OccupationSkill.isco_code, OccupationSkill.skill_id))).all()
    skills_by_isco: dict[str, set[int]] = defaultdict(set)
    for isco, sid in rows:
        skills_by_isco[isco].add(sid)
    mine = skills_by_isco.get(occupation.isco_code, set())
    if not mine:
        return []
    scored = []
    for isco, theirs in skills_by_isco.items():
        if isco == occupation.isco_code:
            continue
        sim = len(mine & theirs) / len(mine | theirs)
        if sim > 0:
            scored.append((isco, sim, theirs - mine))
    scored.sort(key=lambda r: -r[1])
    occs = (await db.scalars(select(Occupation).where(Occupation.isco_code.in_([i for i, _, _ in scored])))).all()
    first_by_isco: dict[str, Occupation] = {}
    for o in sorted(occs, key=lambda o: o.code):
        first_by_isco.setdefault(o.isco_code, o)
    needed = {sid for _, _, new in scored for sid in new}
    skills = {}
    if needed:
        rows = (await db.execute(select(Skill.id, Skill.code, Skill.label).where(Skill.id.in_(needed)))).all()
        skills = {sid: {"id": sid, "code": code, "label": label} for sid, code, label in rows}
    out = []
    for isco, sim, new in scored:
        occ = first_by_isco.get(isco)
        if occ is None:
            continue
        out.append(
            {
                "direction": "mirip",
                "occupation": occupation_view(occ),
                "similarity": round(sim, 3),
                "skills_to_learn": [skills[sid] for sid in sorted(new) if sid in skills][:5],
            }
        )
        if len(out) >= limit:
            break
    return out

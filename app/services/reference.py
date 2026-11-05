import difflib
from collections import defaultdict

from sqlalchemy import String, case, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Occupation, OccupationSkill, Skill
from app.services.embeddings import EmbeddingService, cosine
from app.services.textutil import normalize

CANDIDATE_LIMIT = 200

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
        "title": o.label_id or o.title,
        "title_en": o.title_en,
        "avg_exposure": o.avg_exposure,
        "exposure_gradient": o.exposure_gradient,
        "gradient_cat": o.gradient_cat,
    }
    if match:
        view["match"] = match
    return view


def skill_ref(skill_id: int, code: str | None, label: str, label_id: str | None = None) -> dict:
    return {"id": skill_id, "code": code, "label": label_id or label}


async def search_occupations(
    db: AsyncSession, embeddings: EmbeddingService, q: str, limit: int, *, postgres: bool
) -> list[dict]:
    q = (q or "").strip()
    onboarding = Occupation.is_target.is_(False)
    if not q:
        rows = (
            await db.scalars(
                select(Occupation)
                .where(onboarding)
                .order_by(func.coalesce(Occupation.label_id, Occupation.title))
                .limit(limit)
            )
        ).all()
        return [occupation_view(o) for o in rows]

    like = f"%{q}%"
    prefix = f"{q}%"
    conditions = [
        Occupation.title.ilike(like),
        Occupation.label_id.ilike(like),
        Occupation.title_en.ilike(like),
        Occupation.search_text.ilike(like),
        func.lower(cast(Occupation.aliases, String)).like(f"%{q.lower()}%"),
        Occupation.code.ilike(prefix),
        Occupation.isco_code.ilike(prefix),
    ]
    if postgres and await has_trgm(db):
        # Toleran salah ketik
        conditions.append(func.similarity(Occupation.title, q) > 0.3)
        conditions.append(func.similarity(Occupation.title_en, q) > 0.3)
        conditions.append(func.similarity(Occupation.label_id, q) > 0.3)

    priority = case(
        (or_(Occupation.title.ilike(prefix), Occupation.label_id.ilike(prefix), Occupation.title_en.ilike(prefix)), 0),
        (or_(Occupation.title.ilike(like), Occupation.label_id.ilike(like), Occupation.title_en.ilike(like)), 1),
        else_=2,
    )
    candidates = (
        await db.scalars(
            select(Occupation).where(onboarding, or_(*conditions)).order_by(priority, Occupation.title).limit(CANDIDATE_LIMIT)
        )
    ).all()

    nq = normalize(q)

    def rank(o: Occupation) -> tuple:
        display = o.label_id or o.title
        primary = [o.label_id or "", o.title, o.title_en or ""]
        all_names = [*primary, *(o.aliases or [])]
        norm_primary = [normalize(n) for n in primary if n]
        norm_all = [normalize(n) for n in all_names if n]
        primary_starts = any(n.startswith(nq) for n in norm_primary)
        any_starts = any(n.startswith(nq) for n in norm_all)
        primary_contains = any(nq in n for n in norm_primary)
        ratio = max((difflib.SequenceMatcher(None, nq, n).ratio() for n in norm_all), default=0)
        return (not primary_starts, not any_starts, not primary_contains, -ratio, display)

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
        rows = (
            await db.execute(
                select(Skill.id, Skill.code, Skill.label, Skill.label_id)
                .join(OccupationSkill, OccupationSkill.skill_id == Skill.id)
                .where(
                    or_(
                        OccupationSkill.occupation_code == target.code,
                        OccupationSkill.isco_code == target.isco_code,
                    ),
                    OccupationSkill.relation == "essential",
                )
                .order_by(Skill.code.is_(None), Skill.id)
            )
        ).all()
        curated = [r for r in rows if r.code is not None]
        chosen = curated if curated else rows[:5]
        dedup_skills = []
        seen_sids: set[int] = set()
        for sid, scode, slabel, slabel_id in chosen:
            if sid not in seen_sids:
                seen_sids.add(sid)
                dedup_skills.append(skill_ref(sid, scode, slabel, slabel_id))
        out.append(
            {
                "direction": direction,
                "occupation": occupation_view(target),
                "similarity": None,
                "skills_to_learn": dedup_skills,
            }
        )
    seen = {occupation.code} | {r["occupation"]["code"] for r in out}
    for row in await similar_roles(db, occupation, limit):
        if row["occupation"]["code"] not in seen and len(out) < limit:
            seen.add(row["occupation"]["code"])
            out.append(row)
    return out


async def similar_roles(db: AsyncSession, occupation: Occupation, limit: int) -> list[dict]:
    """Kemiripan Jaccard berbobot (essential=1.0, optional/base=0.5) atas skill antar-jabatan."""
    mine_rows = (
        await db.execute(
            select(OccupationSkill.skill_id, OccupationSkill.relation).where(
                OccupationSkill.occupation_code == occupation.code
            )
        )
    ).all()
    if occupation.esco_uri is None:
        isco_rows = (
            await db.execute(
                select(OccupationSkill.skill_id, OccupationSkill.relation).where(
                    OccupationSkill.isco_code == occupation.isco_code
                )
            )
        ).all()
        mine_rows = [*mine_rows, *isco_rows]

    mine: dict[int, float] = {}
    for sid, rel in mine_rows:
        w = 1.0 if rel == "essential" else 0.5
        if w > mine.get(sid, 0.0):
            mine[sid] = w
    if not mine:
        return []

    mine_essential = [sid for sid, w in mine.items() if w >= 1.0]
    mine_optional = [sid for sid, w in mine.items() if w < 1.0]
    mine_total = sum(mine.values())

    w_cand = case((OccupationSkill.relation == "essential", 1.0), else_=0.5)
    inter_branches = []
    if mine_essential:
        inter_branches.append((OccupationSkill.skill_id.in_(mine_essential), w_cand))
    if mine_optional:
        inter_branches.append((OccupationSkill.skill_id.in_(mine_optional), 0.5))
    w_inter = case(*inter_branches, else_=0.0)

    stmt = (
        select(
            OccupationSkill.occupation_code,
            func.sum(w_inter).label("inter_w"),
            func.sum(w_cand).label("cand_w"),
        )
        .where(OccupationSkill.occupation_code != occupation.code)
        .group_by(OccupationSkill.occupation_code)
        .having(func.sum(w_inter) > 0)
    )
    agg_rows = (await db.execute(stmt)).all()

    scored: list[tuple[str, float]] = []
    for occ_code, inter_w, cand_w in agg_rows:
        denom = mine_total + float(cand_w) - float(inter_w)
        if denom > 0:
            sim = float(inter_w) / denom
            if sim > 0:
                scored.append((occ_code, sim))
    scored.sort(key=lambda r: (-r[1], r[0]))
    top_codes = [c for c, _ in scored[: max(limit * 4, 25)]]
    if not top_codes:
        return []

    occs = (await db.scalars(select(Occupation).where(Occupation.code.in_(top_codes)))).all()
    occ_by_code = {o.code: o for o in occs}

    cand_skill_rows = (
        await db.execute(
            select(
                OccupationSkill.occupation_code,
                OccupationSkill.relation,
                Skill.id,
                Skill.code,
                Skill.label,
                Skill.label_id,
            )
            .join(Skill, Skill.id == OccupationSkill.skill_id)
            .where(OccupationSkill.occupation_code.in_(top_codes))
            .order_by(
                case((OccupationSkill.relation == "essential", 0), else_=1),
                Skill.code.is_(None),
                Skill.id,
            )
        )
    ).all()

    missing_by_occ: dict[str, list[dict]] = defaultdict(list)
    for occ_code, _rel, sid, scode, slabel, slabel_id in cand_skill_rows:
        if sid not in mine and len(missing_by_occ[occ_code]) < 5:
            missing_by_occ[occ_code].append(skill_ref(sid, scode, slabel, slabel_id))

    out = []
    for occ_code, sim in scored:
        occ = occ_by_code.get(occ_code)
        if occ is None:
            continue
        # Jika jabatan asal adalah kode KBJI/ISCO 4 digit yang meminjam skill grup ISCO-nya,
        # lewati okupasi ESCO di grup ISCO yang sama bila tidak ada skill baru untuk dipelajari.
        if (
            occupation.esco_uri is None
            and occ.esco_uri is not None
            and occ.isco_code == occupation.isco_code
            and not missing_by_occ[occ_code]
        ):
            continue
        out.append(
            {
                "direction": "mirip",
                "occupation": occupation_view(occ),
                "similarity": round(sim, 3),
                "skills_to_learn": missing_by_occ[occ_code][:5],
            }
        )
        if len(out) >= limit:
            break
    return out

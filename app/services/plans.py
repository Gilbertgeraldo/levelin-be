import json
import math
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, not_found
from app.models import Attempt, Audit, Mission, Occupation, OccupationSkill, PassportSettings, Plan, Skill, User
from app.services.missions import visible_filter
from app.services.passport import proven_skill_ids

DIRECTIONS = {
    "upskill": "Naik level di jabatan sekarang dengan menguasai kerja bersama AI",
    "pivot": "Beralih ke jabatan tujuan yang skill-nya berdekatan",
}
PHASES = [
    ("fondasi", "Fondasi", 1, 30),
    ("praktik", "Praktik", 31, 60),
    ("bukti", "Bukti", 61, 90),
]
AI_SKILLS_FOR_PIVOT = 2
MISSIONS_PER_PHASE = 2
SHARE_ACTION = "share_passport"


async def essential_skills(db: AsyncSession, isco_code: str) -> list[Skill]:
    rows = await db.scalars(
        select(Skill)
        .join(OccupationSkill, OccupationSkill.skill_id == Skill.id)
        .where(OccupationSkill.isco_code == isco_code, OccupationSkill.relation == "essential")
        .order_by(Skill.id)
    )
    return list(rows.all())


async def ai_skills(db: AsyncSession) -> list[Skill]:
    return list((await db.scalars(select(Skill).where(Skill.category == "ai").order_by(Skill.id))).all())


def _unique(skills: list[Skill]) -> list[Skill]:
    seen: set[int] = set()
    out = []
    for s in skills:
        if s.id not in seen:
            seen.add(s.id)
            out.append(s)
    return out


async def build_phases(
    db: AsyncSession,
    user: User,
    audit: Audit,
    direction: str,
    target: Occupation | None,
) -> tuple[list[int], list[dict]]:
    if direction == "pivot":
        if target is None:
            raise ApiError(400, "target_required", "Arah 'pivot' butuh jabatan tujuan.")
        targets = await essential_skills(db, target.isco_code)
        targets += (await ai_skills(db))[:AI_SKILLS_FOR_PIVOT]
    else:
        targets = await ai_skills(db) + await essential_skills(db, audit.isco_code)
    targets = _unique(targets)
    proven = await proven_skill_ids(db, user.id)
    gap = [s for s in targets if s.id not in proven]

    missions = list((await db.scalars(select(Mission).where(*visible_filter(user)))).all())
    used_missions: set[uuid.UUID] = set()
    per_phase = math.ceil(len(gap) / len(PHASES)) if gap else 0

    phases = []
    for idx, (key, title, day_start, day_end) in enumerate(PHASES):
        phase_skills = gap[idx * per_phase : (idx + 1) * per_phase] if per_phase else []
        skill_ids = {s.id for s in phase_skills}
        items: list[dict] = [{"type": "skill", "ref_id": str(s.id), "title": s.label} for s in phase_skills]
        # Misi yang melatih skill fase ini; utamakan tingkat kesulitan sesuai urutan fase
        candidates = sorted(
            (m for m in missions if m.id not in used_missions and skill_ids & {s.id for s in m.skills}),
            key=lambda m: (abs(m.difficulty - (idx + 1)), -len(skill_ids & {s.id for s in m.skills})),
        )
        for m in candidates[:MISSIONS_PER_PHASE]:
            used_missions.add(m.id)
            items.append({"type": "mission", "ref_id": str(m.id), "title": m.title})
        if key == "bukti":
            items.append(
                {"type": "action", "ref_id": SHARE_ACTION, "title": "Bagikan Paspor Skill dan lamar lowongan"}
            )
        phases.append(
            {"key": key, "title": title, "day_start": day_start, "day_end": day_end, "items": items}
        )
    return [s.id for s in targets], phases


async def create_plan(
    db: AsyncSession, user: User, audit: Audit, direction: str, target_code: str | None
) -> Plan:
    if direction not in DIRECTIONS:
        raise ApiError(400, "invalid_direction", "Arah rencana harus 'upskill' atau 'pivot'.")
    target = None
    if target_code:
        target = await db.get(Occupation, target_code)
        if target is None:
            raise not_found("Jabatan tujuan")
    target_ids, phases = await build_phases(db, user, audit, direction, target)

    previous = (
        await db.scalars(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    ).all()
    for p in previous:
        p.status = "archived"

    plan = Plan(
        user_id=user.id,
        audit_id=audit.id,
        direction=direction,
        target_occupation_code=target.code if target else None,
        status="active",
        target_skill_ids=target_ids,
        phases=phases,
    )
    db.add(plan)
    await db.flush()
    return plan


async def regenerate(db: AsyncSession, user: User, plan: Plan, direction: str, target_code: str | None) -> Plan:
    if direction not in DIRECTIONS:
        raise ApiError(400, "invalid_direction", "Arah rencana harus 'upskill' atau 'pivot'.")
    audit = await db.get(Audit, plan.audit_id) if plan.audit_id else None
    if audit is None:
        raise ApiError(409, "audit_missing", "Audit asal rencana sudah dihapus; buat rencana baru.")
    target = await db.get(Occupation, target_code) if target_code else None
    if target_code and target is None:
        raise not_found("Jabatan tujuan")
    plan.target_skill_ids, plan.phases = await build_phases(db, user, audit, direction, target)
    plan.direction = direction
    plan.target_occupation_code = target.code if target else None
    await db.flush()
    return plan


async def get_owned_plan(db: AsyncSession, user: User, plan_id: uuid.UUID) -> Plan:
    plan = await db.get(Plan, plan_id)
    if plan is None or plan.user_id != user.id:
        raise not_found("Rencana")
    return plan


async def active_plan(db: AsyncSession, user_id: uuid.UUID) -> Plan | None:
    return await db.scalar(
        select(Plan)
        .where(Plan.user_id == user_id, Plan.status == "active")
        .order_by(Plan.created_at.desc())
        .limit(1)
    )


async def plan_view(db: AsyncSession, plan: Plan) -> dict:
    """Rencana beserta status selesai tiap item dan progres per fase (dihitung saat dibaca)."""
    proven = await proven_skill_ids(db, plan.user_id)
    passed = set(
        (
            await db.scalars(
                select(Attempt.mission_id).where(Attempt.user_id == plan.user_id, Attempt.passed.is_(True))
            )
        ).all()
    )
    sharing = await db.get(PassportSettings, plan.user_id)
    shared = bool(sharing and (sharing.share_with_org or sharing.public_link))

    def done(item: dict) -> bool:
        if item["type"] == "skill":
            return int(item["ref_id"]) in proven
        if item["type"] == "mission":
            return uuid.UUID(item["ref_id"]) in passed
        if item["ref_id"] == SHARE_ACTION:
            return shared
        return False

    phases = []
    total = completed = 0
    for phase in plan.phases or []:
        items = [{**item, "done": done(item)} for item in phase["items"]]
        n_done = sum(i["done"] for i in items)
        total += len(items)
        completed += n_done
        phases.append(
            {**phase, "items": items, "progress": round(n_done / len(items), 3) if items else 0.0}
        )

    target = await db.get(Occupation, plan.target_occupation_code) if plan.target_occupation_code else None
    gap_ids = [sid for sid in plan.target_skill_ids or [] if sid not in proven]
    return {
        "id": plan.id,
        "audit_id": plan.audit_id,
        "direction": plan.direction,
        "direction_label": DIRECTIONS.get(plan.direction, plan.direction),
        "target_occupation": {"code": target.code, "title": target.title} if target else None,
        "status": plan.status,
        "skill_gap_ids": gap_ids,
        "phases": phases,
        "progress": round(completed / total, 3) if total else 0.0,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }


def narrative_prompt(view: dict) -> str:
    data = {
        "arah": view["direction_label"],
        "jabatan_tujuan": (view["target_occupation"] or {}).get("title"),
        "progres_total": view["progress"],
        "fase": [
            {
                "nama": p["title"],
                "hari": f"{p['day_start']}-{p['day_end']}",
                "skill": [i["title"] for i in p["items"] if i["type"] == "skill"],
                "misi": [i["title"] for i in p["items"] if i["type"] == "mission"],
                "progres": p["progress"],
            }
            for p in view["phases"]
        ],
    }
    return "Data rencana:\n```json\n" + json.dumps(data, ensure_ascii=False, indent=1) + "\n```"


def narrative_fallback(view: dict) -> str:
    lines = []
    target = (view["target_occupation"] or {}).get("title")
    if view["direction"] == "pivot" and target:
        lines.append(f"Rencanamu 90 hari ke depan: bersiap beralih menjadi {target}.")
    else:
        lines.append("Rencanamu 90 hari ke depan: naik level di jabatanmu sekarang dengan bekerja bersama AI.")
    for p in view["phases"]:
        skills = [i["title"] for i in p["items"] if i["type"] == "skill"]
        missions = [i["title"] for i in p["items"] if i["type"] == "mission"]
        part = f"Fase {p['title']} (hari {p['day_start']}-{p['day_end']})"
        if skills:
            part += ": pelajari " + ", ".join(skills[:3])
        if missions:
            part += ", lalu buktikan lewat misi \"" + missions[0] + "\""
        lines.append(part + ".")
    lines.append("Kerjakan sedikit demi sedikit; setiap misi yang lulus langsung tercatat di Paspor Skill-mu.")
    return "\n\n".join(lines)

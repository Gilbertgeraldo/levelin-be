"""Rencana Naik Level 90 hari, mengikuti prototipe Tim Hearz.

Arah: perkuat (kuasai AI di jabatan sekarang), naik (jabatan bernilai lebih tinggi), pindah (jabatan
berdekatan). Skill dibutuhkan = skill jabatan tujuan + "ai", atau untuk "perkuat": "ai" + skill dari dua
jenis tugas yang paling terpapar AI. Celah skill = dibutuhkan dikurangi skill yang sudah terbukti lewat misi.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, not_found
from app.models import Attempt, Audit, Occupation, OccupationSkill, OccupationTask, PassportSettings, Plan, Skill, TaskKind, User
from app.services.audits import first_name, latest_audit
from app.services.missions import exposed_kinds, recommended_order
from app.services.passport import proven_skill_ids

DIRECTIONS = {
    "perkuat": "Perkuat posisi sekarang dengan menguasai AI untuk tugas yang bisa dibantu AI",
    "naik": "Naik peran: ambil tugas yang bernilai lebih tinggi",
    "pindah": "Pindah ke jabatan berdekatan dengan kemiripan skill tertinggi",
}
SHARE_ACTION = "share_passport"
APPLY_ACTION = "apply_target"
AI_SKILL = "ai"


def gradient_label(occupation: Occupation, labels: dict[str, str] | None = None) -> str:
    cat = occupation.gradient_cat or ""
    if labels and cat in labels:
        return labels[cat]
    if "Gradient" in cat:
        return f"Gradien {cat.rsplit(' ', 1)[-1]}"
    return "Paparan minimal" if cat else "Belum ada data"


def occupation_brief(occupation: Occupation | None) -> dict | None:
    if occupation is None:
        return None
    return {
        "code": occupation.code,
        "name": occupation.title,
        "mean": occupation.avg_exposure,
        "gradient_label": gradient_label(occupation),
    }


async def skills_by_code(db: AsyncSession) -> dict[str, Skill]:
    return {s.code: s for s in (await db.scalars(select(Skill).where(Skill.code.is_not(None)))).all()}


async def target_for(db: AsyncSession, audit: Audit, direction: str) -> Occupation | None:
    if direction == "perkuat":
        return None
    current = await db.get(Occupation, audit.occupation_code)
    code = (current.naik_code if direction == "naik" else current.pindah_code) if current else None
    target = await db.get(Occupation, code) if code else None
    if target is None:
        raise ApiError(400, "no_target", "Belum ada jabatan tujuan untuk arah ini di jabatanmu.")
    return target


async def needed_skill_codes(db: AsyncSession, audit: Audit, target: Occupation | None) -> list[str]:
    if target is not None:
        rows = await db.scalars(
            select(Skill.code)
            .join(OccupationSkill, OccupationSkill.skill_id == Skill.id)
            .where(OccupationSkill.isco_code == target.isco_code, OccupationSkill.relation == "essential")
            .order_by(Skill.id)
        )
        codes = list(rows.all())
        return codes + ([AI_SKILL] if AI_SKILL not in codes else [])
    kinds = {k.code: k for k in (await db.scalars(select(TaskKind))).all()}
    codes = [AI_SKILL]
    for kind_code, _ in exposed_kinds(audit)[:2]:
        for code in kinds[kind_code].skill_codes if kind_code in kinds else []:
            if code not in codes:
                codes.append(code)
    return codes


async def build(db: AsyncSession, user: User, audit: Audit, direction: str) -> tuple[Occupation | None, list[int], list[dict]]:
    if direction not in DIRECTIONS:
        raise ApiError(400, "invalid_direction", "Arah rencana harus 'perkuat', 'naik', atau 'pindah'.")
    target = await target_for(db, audit, direction)
    catalog = await skills_by_code(db)
    needed = [catalog[c] for c in await needed_skill_codes(db, audit, target) if c in catalog]
    proven = await proven_skill_ids(db, user.id)
    gap = [s for s in needed if s.id not in proven]
    ordered, _ = await recommended_order(db, user)
    missions = ordered[:3]

    def mission_item(index: int) -> list[dict]:
        if index >= len(missions):
            return []
        m = missions[index]
        return [{"type": "mission", "ref_id": str(m.id), "title": m.title}]

    last_actions = [{"type": "action", "ref_id": SHARE_ACTION, "title": "Lengkapi dan bagikan Paspor Skill"}]
    if target is not None:
        last_actions.append({"type": "action", "ref_id": APPLY_ACTION, "title": f"Mulai lamar posisi {target.title}"})
    phases = [
        {
            "key": "bulan-1",
            "title": "Bulan pertama: fondasi",
            "day_start": 1,
            "day_end": 30,
            "items": [{"type": "skill", "ref_id": str(s.id), "title": s.label} for s in gap] + mission_item(0),
        },
        {"key": "bulan-2", "title": "Bulan kedua: perluas", "day_start": 31, "day_end": 60, "items": mission_item(1)},
        {
            "key": "bulan-3",
            "title": "Bulan ketiga: buktikan",
            "day_start": 61,
            "day_end": 90,
            "items": mission_item(2) + last_actions,
        },
    ]
    return target, [s.id for s in needed], phases


async def create_plan(db: AsyncSession, user: User, audit: Audit, direction: str) -> Plan:
    target, needed_ids, phases = await build(db, user, audit, direction)
    for previous in (await db.scalars(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))).all():
        previous.status = "archived"
    plan = Plan(
        user_id=user.id,
        audit_id=audit.id,
        direction=direction,
        target_occupation_code=target.code if target else None,
        status="active",
        target_skill_ids=needed_ids,
        phases=phases,
    )
    db.add(plan)
    await db.flush()
    return plan


async def regenerate(db: AsyncSession, user: User, plan: Plan, direction: str) -> Plan:
    audit = await db.get(Audit, plan.audit_id) if plan.audit_id else None
    if audit is None:
        raise ApiError(409, "audit_missing", "Audit asal rencana sudah dihapus; buat rencana baru.")
    target, plan.target_skill_ids, plan.phases = await build(db, user, audit, direction)
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
    """Rencana beserta status tiap item dan progres per fase (dihitung saat dibaca)."""
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
        return item["ref_id"] == SHARE_ACTION and shared

    phases, total, completed = [], 0, 0
    for phase in plan.phases or []:
        items = [{**item, "done": done(item)} for item in phase["items"]]
        n_done = sum(i["done"] for i in items)
        total += len(items)
        completed += n_done
        phases.append({**phase, "items": items, "progress": round(n_done / len(items), 3) if items else 0.0})

    audit = await db.get(Audit, plan.audit_id) if plan.audit_id else None
    current = await db.get(Occupation, audit.occupation_code) if audit else None
    target = await db.get(Occupation, plan.target_occupation_code) if plan.target_occupation_code else None
    target_view = occupation_brief(target)
    if target is not None:
        tasks = (
            await db.scalars(select(OccupationTask).where(OccupationTask.isco_code == target.isco_code).order_by(OccupationTask.id))
        ).all()
        target_view["tasks"] = [{"description": t.description, "ilo_score": t.ilo_score} for t in tasks]

    skills = {s.id: s for s in (await db.scalars(select(Skill).where(Skill.id.in_(plan.target_skill_ids or [])))).all()}
    needed = [
        {"id": sid, "code": skills[sid].code, "label": skills[sid].label, "proven": sid in proven}
        for sid in plan.target_skill_ids or []
        if sid in skills
    ]
    latest = await latest_audit(db, plan.user_id)
    return {
        "id": plan.id,
        "audit_id": plan.audit_id,
        "direction": plan.direction,
        "direction_label": DIRECTIONS.get(plan.direction, plan.direction),
        "current_occupation": occupation_brief(current),
        "target_occupation": target_view,
        "needed_skills": needed,
        "gap_skills": [s for s in needed if not s["proven"]],
        "skill_gap_ids": [s["id"] for s in needed if not s["proven"]],
        "status": plan.status,
        "phases": phases,
        "progress": round(completed / total, 3) if total else 0.0,
        "readiness_score": latest.readiness_score if latest else None,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }


def mission_titles(view: dict) -> list[str]:
    return [i["title"] for p in view["phases"] for i in p["items"] if i["type"] == "mission"]


def narrative_facts(view: dict, user: User) -> str:
    current = view["current_occupation"] or {}
    target = view["target_occupation"]
    lines = [
        f"Nama: {first_name(user)}",
        f"Jabatan sekarang: {current.get('name')} (paparan rata-rata {current.get('mean')})",
        f"Arah: {view['direction']}",
    ]
    if target:
        lines.append(f"Jabatan tujuan: {target['name']} (paparan rata-rata {target['mean']})")
    lines += [
        f"Skill yang belum terbukti: {', '.join(s['label'] for s in view['gap_skills']) or 'tidak ada'}",
        f"Urutan misi: {' -> '.join(mission_titles(view))}",
        f"Skor Kesiapan sekarang: {view['readiness_score']}",
    ]
    return "Fakta:\n" + "\n".join(lines)


def narrative_fallback(view: dict, user: User) -> str:
    missions = mission_titles(view) + ["misi berikutnya"] * 3
    target = view["target_occupation"]
    ending = f" dan mulai lamar posisi {target['name']}" if target else " lalu cek ulang Peta Tugas"
    return (
        f"{first_name(user)}, bulan pertama fokus ke fondasi: ikuti kelas yang paling murah untuk skill yang belum "
        f"terbukti, lalu selesaikan misi “{missions[0]}”. Bulan kedua, perluas ke “{missions[1]}” "
        "supaya tugas yang paling banyak bergeser sudah kamu kuasai bersama AI. Bulan ketiga, lengkapi Paspor "
        f"Skill dengan “{missions[2]}”{ending}."
    )

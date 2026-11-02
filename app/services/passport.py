import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Mission, PassportEntry, PassportSettings, Skill, User


async def proven_skill_ids(db: AsyncSession, user_id: uuid.UUID) -> set[int]:
    rows = await db.scalars(
        select(PassportEntry.skill_id).where(PassportEntry.user_id == user_id).distinct()
    )
    return set(rows.all())


async def get_settings_row(db: AsyncSession, user_id: uuid.UUID) -> PassportSettings:
    row = await db.get(PassportSettings, user_id)
    if row is None:
        row = PassportSettings(user_id=user_id, share_with_org=False, public_link=False)
        db.add(row)
        await db.flush()
    return row


def public_url(slug: str | None) -> str | None:
    if not slug:
        return None
    return f"{get_settings().passport_public_base_url.rstrip('/')}/{slug}"


def sharing_view(row: PassportSettings) -> dict:
    return {
        "share_with_org": row.share_with_org,
        "public_link": row.public_link,
        "public_slug": row.public_slug if row.public_link else None,
        "public_url": public_url(row.public_slug) if row.public_link else None,
    }


async def update_sharing(
    db: AsyncSession, user_id: uuid.UUID, share_with_org: bool | None, public_link: bool | None
) -> PassportSettings:
    row = await get_settings_row(db, user_id)
    if share_with_org is not None:
        row.share_with_org = share_with_org
    if public_link is not None:
        row.public_link = public_link
        if public_link and not row.public_slug:
            row.public_slug = secrets.token_urlsafe(9).replace("_", "x").replace("-", "y").lower()
    await db.commit()
    return row


async def passport_skills(db: AsyncSession, user_id: uuid.UUID, *, include_scores: bool = True) -> list[dict]:
    """Skill terbukti beserta daftar bukti (misi, nilai, tanggal), terbaru di atas."""
    rows = (
        await db.execute(
            select(PassportEntry, Skill, Mission)
            .join(Skill, Skill.id == PassportEntry.skill_id)
            .join(Mission, Mission.id == PassportEntry.mission_id)
            .where(PassportEntry.user_id == user_id)
            .order_by(PassportEntry.earned_at.desc())
        )
    ).all()
    skills: dict[int, dict] = {}
    for entry, skill, mission in rows:
        item = skills.setdefault(
            skill.id,
            {
                "skill": {"id": skill.id, "label": skill.label, "category": skill.category},
                "evidence_count": 0,
                "best_score": 0.0,
                "last_earned_at": entry.earned_at,
                "evidence": [],
            },
        )
        item["evidence_count"] += 1
        item["best_score"] = max(item["best_score"], entry.score)
        evidence = {
            "mission_id": mission.id,
            "mission_title": mission.title,
            "attempt_id": entry.attempt_id,
            "earned_at": entry.earned_at,
            "rubric": [r.get("criterion") for r in (mission.rubric or [])],
        }
        if include_scores:
            evidence["score"] = entry.score
        item["evidence"].append(evidence)
    result = list(skills.values())
    if not include_scores:
        for item in result:
            item.pop("best_score", None)
    return result


def display_name(user: User, *, public: bool) -> str:
    name = (user.full_name or "").strip()
    if not name:
        return "Pengguna Levelin"
    if not public:
        return name
    # Halaman publik: nama depan + inisial belakang
    parts = name.split()
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1][0]}."

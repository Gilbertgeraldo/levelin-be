from fastapi import APIRouter, Response
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, cache_public
from app.core.errors import not_found
from app.db.base import utcnow
from app.models import PassportSettings, User
from app.schemas.common import SharingOut
from app.schemas.passport import PassportOut, PublicPassportOut, SharingIn
from app.services import passport

router = APIRouter(tags=["Paspor Skill"])


@router.get("/passport", response_model=PassportOut, summary="Skill terbukti dan daftar bukti (misi, rubrik, nilai, tanggal)")
async def get_passport(db: DB, user: CurrentUser):
    skills = await passport.passport_skills(db, user.id)
    sharing = await db.get(PassportSettings, user.id) or PassportSettings(
        user_id=user.id, share_with_org=False, public_link=False
    )
    return {
        "name": passport.display_name(user, public=False),
        "total_skills": len(skills),
        "skills": skills,
        "sharing": passport.sharing_view(sharing),
    }


@router.patch("/passport/sharing", response_model=SharingOut, summary="Atur akses HR dan link publik")
async def update_sharing(body: SharingIn, db: DB, user: CurrentUser):
    row = await passport.update_sharing(db, user.id, body.share_with_org, body.public_link)
    return passport.sharing_view(row)


@router.get(
    "/public/passports/{slug}",
    response_model=PublicPassportOut,
    summary="Halaman Paspor publik (tanpa login, hanya-baca)",
)
async def public_passport(slug: str, response: Response, db: DB):
    row = await db.scalar(
        select(PassportSettings).where(PassportSettings.public_slug == slug, PassportSettings.public_link.is_(True))
    )
    if row is None:
        raise not_found("Paspor")
    user = await db.get(User, row.user_id)
    skills = await passport.passport_skills(db, row.user_id)
    cache_public(response, 300)
    return {
        "name": passport.display_name(user, public=True),
        "total_skills": len(skills),
        "skills": skills,
        "generated_at": utcnow(),
    }

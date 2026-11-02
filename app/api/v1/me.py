from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from app.api.deps import DB, CurrentUser
from app.core.errors import ApiError
from app.db.base import utcnow
from app.models import Occupation, Organization, PassportSettings, Profile, User
from app.schemas.me import MeOut, ProfileIn
from app.services import account, passport
from app.services.billing import mission_quota
from app.services.invitations import accept_pending_invitation

router = APIRouter(prefix="/me", tags=["Akun & profil"])


async def me_view(db, user: User) -> dict:
    org = await db.get(Organization, user.org_id) if user.org_id else None
    profile = await db.get(Profile, user.id)
    occupation = (
        await db.get(Occupation, profile.occupation_code) if profile and profile.occupation_code else None
    )
    sharing = await db.get(PassportSettings, user.id) or PassportSettings(
        user_id=user.id, share_with_org=False, public_link=False
    )
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "department": user.department,
        "organization": {"id": org.id, "name": org.name, "slug": org.slug} if org else None,
        "profile": {
            "occupation": {"code": occupation.code, "title": occupation.title, "isco_code": occupation.isco_code}
            if occupation
            else None,
            "job_title": profile.job_title,
            "industry": profile.industry,
            "years_experience": profile.years_experience,
            "goal": profile.goal,
            "consent_data_processing": profile.consent_data_processing,
            "consent_version": profile.consent_version,
            "consented_at": profile.consented_at,
        }
        if profile
        else None,
        "subscription": await mission_quota(db, user),
        "passport_sharing": passport.sharing_view(sharing),
    }


@router.get("", response_model=MeOut, summary="Profil, peran, organisasi, paket langganan")
async def get_me(db: DB, user: CurrentUser):
    if user.org_id is None and await accept_pending_invitation(db, user):
        await db.commit()
    return await me_view(db, user)


@router.put("/profile", response_model=MeOut, summary="Simpan jabatan, industri, lama kerja, tujuan, persetujuan")
async def put_profile(body: ProfileIn, db: DB, user: CurrentUser):
    if await db.get(Occupation, body.occupation_code) is None:
        raise ApiError(400, "unknown_occupation", "Kode jabatan tidak dikenal. Pilih dari /v1/occupations.")
    profile = await db.get(Profile, user.id)
    if profile is None:
        profile = Profile(user_id=user.id)
        db.add(profile)
    newly_consented = body.consent_data_processing and (
        not profile.consent_data_processing or profile.consent_version != body.consent_version
    )
    profile.occupation_code = body.occupation_code
    profile.job_title = body.job_title
    profile.industry = body.industry
    profile.years_experience = body.years_experience
    profile.goal = body.goal
    profile.consent_data_processing = body.consent_data_processing
    profile.consent_version = body.consent_version
    if newly_consented:
        profile.consented_at = utcnow()
    elif not body.consent_data_processing:
        profile.consented_at = None
    if body.full_name:
        user.full_name = body.full_name.strip()
    await db.commit()
    return await me_view(db, user)


@router.delete("", status_code=204, summary="Hapus akun dan semua data (hak hapus UU PDP)")
async def delete_me(db: DB, user: CurrentUser):
    await account.delete_user(db, user)
    return Response(status_code=204)


@router.get("/export", summary="Unduh semua data milik pengguna (UU PDP)")
async def export_me(db: DB, user: CurrentUser):
    data = await account.export_user_data(db, user)
    return JSONResponse(
        data,
        headers={"Content-Disposition": f'attachment; filename="levelin-data-{user.id}.json"'},
    )

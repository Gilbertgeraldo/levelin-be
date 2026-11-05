import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, Pagination
from app.core.errors import ApiError, not_found
from app.core.pagination import Page, paginate_list
from app.db.base import utcnow
from app.models import Job, JobApplication, PassportSettings
from app.schemas.jobs import ApplicationOut, ApplyIn, JobMatchOut
from app.services import jobs, passport
from app.services.notifications import notify

router = APIRouter(prefix="/jobs", tags=["Lowongan"])


@router.get("/matches", response_model=Page[JobMatchOut], summary="Lowongan beserta % kecocokan; flag source_verified")
async def job_matches(db: DB, user: CurrentUser, page: Pagination, internal_first: bool = True):
    ranked = await jobs.matches(db, user, internal_first=internal_first)
    items, meta = paginate_list(ranked, page)
    return {"data": items, "meta": meta}


@router.post("/{job_id}/apply", status_code=201, response_model=ApplicationOut, summary="Melamar dengan tautan Paspor")
async def apply(job_id: uuid.UUID, body: ApplyIn, db: DB, user: CurrentUser):
    job = await db.get(Job, job_id)
    now = utcnow()
    if (
        job is None
        or not job.is_active
        or (job.expires_at is not None and job.expires_at <= now)
        or job.org_id not in (None, user.org_id)
    ):
        raise not_found("Lowongan")
    sharing = await db.get(PassportSettings, user.id)
    internal = job.org_id is not None
    if sharing is None or not (sharing.public_link or (internal and sharing.share_with_org)):
        raise ApiError(
            400,
            "passport_not_shared",
            "Nyalakan link publik Paspor (atau 'Bagikan ke HR' untuk lowongan internal) sebelum melamar.",
        )
    exists = await db.scalar(
        select(JobApplication.id).where(JobApplication.job_id == job.id, JobApplication.user_id == user.id)
    )
    if exists:
        raise ApiError(409, "already_applied", "Kamu sudah melamar lowongan ini.")
    application = JobApplication(
        job_id=job.id,
        user_id=user.id,
        passport_url=passport.public_url(sharing.public_slug) if sharing.public_link else None,
        cover_note=body.cover_note,
        status="submitted",
    )
    db.add(application)
    await db.flush()
    if job.created_by is not None and job.created_by != user.id:
        applicant_name = passport.display_name(user, public=False)
        await notify(
            db,
            job.created_by,
            "job_application",
            "Pelamar baru masuk",
            f'{applicant_name} melamar posisi "{job.title}".',
            {"job_id": str(job.id), "application_id": str(application.id)},
        )
    await db.commit()
    return application

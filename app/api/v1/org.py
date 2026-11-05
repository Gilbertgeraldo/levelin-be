import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from starlette.datastructures import UploadFile as FormFile

from app.api.deps import DB, HRAdmin, Pagination
from app.core.config import get_settings
from app.core.errors import ApiError, not_found
from app.core.openapi import invitation_request_body
from app.core.pagination import Page, paginate_list, paginate_scalars
from app.db.base import utcnow
from app.models import CustomMissionRequest, Invitation, Job, JobApplication, Occupation, Organization, PassportSettings, Skill, TeamMembership, User
from app.schemas.jobs import JobOut
from app.schemas.org import (
    CandidateOut,
    CustomMissionOut,
    DashboardOut,
    InvitationIn,
    InvitationOut,
    InvitationResult,
    JobApplicantOut,
    MemberOut,
    MemberPatch,
    OrgJobIn,
    OrgRole,
    ProgressReportOut,
)
from app.services import org, passport, storage
from app.services.jobs import job_view

router = APIRouter(prefix="/org", tags=["Dashboard HR"])

MAX_CSV_BYTES = 1024 * 1024
MAX_INVITES = 500


@router.get("/dashboard", response_model=DashboardOut, summary="Ringkasan agregat organisasi (k-anonim, payload v0.1)")
async def dashboard(db: DB, hr: HRAdmin):
    return await org.dashboard(db, hr.org_id)


@router.get(
    "/candidates",
    response_model=Page[CandidateOut],
    summary="Kandidat internal: hanya pekerja yang menyalakan berbagi Paspor ke HR & atasan",
)
async def candidates(
    db: DB,
    hr: HRAdmin,
    page: Pagination,
    skill_id: int | None = None,
    department: Annotated[str | None, Query(max_length=120)] = None,
):
    members = await org.org_members(db, hr.org_id)
    if department:
        members = [m for m in members if m.department == department]
    items, meta = paginate_list(await org.candidates(db, members, skill_id=skill_id), page)
    return {"data": items, "meta": meta}


@router.get(
    "/members",
    response_model=Page[MemberOut],
    summary="Daftar karyawan (identitas, peran, departemen; tanpa hasil audit)",
)
async def members(
    db: DB,
    hr: HRAdmin,
    page: Pagination,
    role: OrgRole | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
):
    stmt = select(User).where(User.org_id == hr.org_id).order_by(User.full_name, User.email)
    if role:
        stmt = stmt.where(User.role == role)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(User.full_name).like(like), func.lower(User.email).like(like)))
    rows, meta = await paginate_scalars(db, stmt, page)
    managers = await _manager_names(db, [u.id for u in rows])
    return {"data": [_member_view(u, managers.get(u.id, [])) for u in rows], "meta": meta}


async def _manager_names(db, member_ids: list) -> dict:
    if not member_ids:
        return {}
    rows = (
        await db.execute(
            select(TeamMembership.member_id, User.full_name, User.email)
            .join(User, User.id == TeamMembership.manager_id)
            .where(TeamMembership.member_id.in_(member_ids))
        )
    ).all()
    out: dict = {}
    for member_id, name, email in rows:
        out.setdefault(member_id, []).append(name or email or "Atasan")
    return out


def _member_view(user: User, manager_names: list[str]) -> dict:
    return {
        "user_id": user.id,
        "name": passport.display_name(user, public=False),
        "email": user.email,
        "department": user.department,
        "role": user.role,
        "manager_names": manager_names,
        "last_active_at": user.last_active_at,
        "is_online": org.is_online(user.last_active_at),
    }


@router.patch("/members/{user_id}", response_model=MemberOut, summary="Ubah peran (mis. jadikan atasan tim) atau departemen")
async def update_member(user_id: uuid.UUID, body: MemberPatch, db: DB, hr: HRAdmin):
    member = await db.get(User, user_id)
    if member is None or member.org_id != hr.org_id:
        raise ApiError(404, "not_found", "Karyawan tidak ditemukan.")
    if member.id == hr.id and body.role is not None and body.role != hr.role:
        raise ApiError(400, "own_role", "Tidak bisa mengubah peranmu sendiri.")
    fields = body.model_fields_set
    if "role" in fields and body.role:
        if member.role == "manager" and body.role != "manager":
            # Atasan yang diturunkan perannya tidak lagi punya tim
            await db.execute(delete(TeamMembership).where(TeamMembership.manager_id == member.id))
        member.role = body.role
    if "department" in fields:
        member.department = body.department
    await db.commit()
    managers = await _manager_names(db, [member.id])
    return _member_view(member, managers.get(member.id, []))


@router.post(
    "/invitations",
    status_code=201,
    response_model=InvitationResult,
    summary="Undang karyawan (JSON berisi email, atau CSV via multipart/form-data)",
    openapi_extra=invitation_request_body(InvitationIn, with_role=True),
)
async def invite(request: Request, db: DB, hr: HRAdmin):
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, FormFile):
            raise ApiError(400, "missing_file", "Sertakan berkas CSV di kolom 'file'.")
        content = await upload.read(MAX_CSV_BYTES + 1)
        if len(content) > MAX_CSV_BYTES:
            raise ApiError(413, "payload_too_large", "CSV maksimal 1 MB.")
        entries = org.parse_invite_csv(content)
        if not entries:
            raise ApiError(400, "invalid_csv", "CSV harus punya header dan kolom 'email'.")
        default_dept = form.get("department") or None
        default_role = form.get("role") or "worker"
        for e in entries:
            e["department"] = e["department"] or default_dept
            e["role"] = e["role"] or default_role
    else:
        try:
            body = InvitationIn.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            raise ApiError(400, "validation_error", "Data undangan tidak valid.") from exc
        entries = [{"email": e, "department": body.department, "role": body.role} for e in body.emails]
        entries += [i.model_dump() for i in body.invitees]
    if not entries:
        raise ApiError(400, "no_invitees", "Isi minimal satu email.")
    if len(entries) > MAX_INVITES:
        raise ApiError(400, "too_many_invitees", f"Maksimal {MAX_INVITES} undangan per permintaan.")
    return await org.create_invitations(db, hr.org_id, hr, entries)


@router.get("/invitations", response_model=Page[InvitationOut], summary="Status undangan")
async def list_invitations(
    db: DB,
    hr: HRAdmin,
    page: Pagination,
    status: Literal["pending", "accepted", "expired", "revoked"] | None = None,
):
    await org.expire_old_invitations(db, hr.org_id)
    stmt = (
        select(Invitation)
        .where(Invitation.org_id == hr.org_id, Invitation.manager_id.is_(None))
        .order_by(Invitation.created_at.desc())
    )
    if status:
        stmt = stmt.where(Invitation.status == status)
    rows, meta = await paginate_scalars(db, stmt, page)
    return {"data": rows, "meta": meta}


@router.post(
    "/custom-missions",
    status_code=201,
    response_model=CustomMissionOut,
    summary="Pesan misi kustom (unggah dokumen PDF/Word)",
)
async def order_custom_mission(
    db: DB,
    hr: HRAdmin,
    title: Annotated[str, Form(min_length=3, max_length=255)],
    file: Annotated[UploadFile, File(description="Dokumen SOP/contoh kerja, PDF atau Word")],
    description: Annotated[str | None, Form(max_length=5000)] = None,
    target_role: Annotated[str | None, Form(max_length=200)] = None,
):
    limit = get_settings().max_upload_mb * 1024 * 1024
    content = await file.read(limit + 1)
    ext = storage.check_document(file.filename, file.content_type, content)
    key = await storage.save_document(hr.org_id, file.filename, content, file.content_type or "", ext)
    request = CustomMissionRequest(
        org_id=hr.org_id,
        requested_by=hr.id,
        title=title.strip(),
        description=description,
        target_role=target_role,
        file_name=file.filename,
        content_type=file.content_type,
        file_size=len(content),
        storage_key=key,
        status="requested",
    )
    db.add(request)
    await db.commit()
    return request


@router.delete("/invitations/{invitation_id}", status_code=204, summary="Batalkan undangan organisasi yang belum diterima")
async def revoke_org_invitation(invitation_id: uuid.UUID, db: DB, hr: HRAdmin):
    invitation = await db.get(Invitation, invitation_id)
    if invitation is None or invitation.org_id != hr.org_id or invitation.manager_id is not None:
        raise not_found("Undangan")
    if invitation.status != "pending":
        raise ApiError(409, "not_pending", "Undangan ini sudah tidak menunggu.")
    invitation.status = "revoked"
    await db.commit()
    return Response(status_code=204)


@router.get("/custom-missions", response_model=Page[CustomMissionOut], summary="Daftar pesanan misi kustom")
async def list_custom_missions(db: DB, hr: HRAdmin, page: Pagination):
    stmt = (
        select(CustomMissionRequest)
        .where(CustomMissionRequest.org_id == hr.org_id)
        .order_by(CustomMissionRequest.created_at.desc())
    )
    rows, meta = await paginate_scalars(db, stmt, page)
    return {"data": rows, "meta": meta}


@router.post("/jobs", status_code=201, response_model=JobOut, summary="Pasang lowongan internal")
async def create_job(body: OrgJobIn, db: DB, hr: HRAdmin):
    organization = await db.get(Organization, hr.org_id)
    skills = []
    if body.skill_ids:
        skills = list((await db.scalars(select(Skill).where(Skill.id.in_(body.skill_ids)))).all())
        missing = sorted(set(body.skill_ids) - {s.id for s in skills})
        if missing:
            raise ApiError(400, "unknown_skills", "Ada skill yang tidak dikenal.", details=missing)
    isco = None
    if body.occupation_code:
        occupation = await db.get(Occupation, body.occupation_code)
        if occupation is None:
            raise ApiError(400, "unknown_occupation", "Kode jabatan tidak dikenal.")
        isco = occupation.isco_code
    if body.salary_min and body.salary_max and body.salary_min > body.salary_max:
        raise ApiError(400, "invalid_salary", "salary_min tidak boleh lebih besar dari salary_max.")
    job = Job(
        org_id=hr.org_id,
        title=body.title,
        company=organization.name,
        location=body.location,
        employment_type=body.employment_type,
        description=body.description,
        url=body.url,
        source="internal",
        source_verified=True,
        occupation_code=body.occupation_code,
        isco_code=isco,
        salary_min=body.salary_min,
        salary_max=body.salary_max,
        expires_at=body.expires_at,
        created_by=hr.id,
        is_active=True,
    )
    job.skills = skills
    db.add(job)
    await db.commit()
    return job_view(job)


@router.get("/jobs", response_model=Page[JobOut], summary="Lowongan internal organisasi")
async def list_jobs(db: DB, hr: HRAdmin, page: Pagination):
    stmt = select(Job).where(Job.org_id == hr.org_id).order_by(Job.posted_at.desc())
    rows, meta = await paginate_scalars(db, stmt, page)
    counts = dict(
        (
            await db.execute(
                select(JobApplication.job_id, func.count(JobApplication.id))
                .where(JobApplication.job_id.in_([j.id for j in rows]))
                .group_by(JobApplication.job_id)
            )
        ).all()
    ) if rows else {}
    return {"data": [{**job_view(j), "applicant_count": counts.get(j.id, 0)} for j in rows], "meta": meta}


@router.get("/jobs/{job_id}/applications", response_model=list[JobApplicantOut], summary="Daftar pelamar lowongan internal")
async def list_job_applications(job_id: uuid.UUID, db: DB, hr: HRAdmin):
    job = await db.get(Job, job_id)
    if job is None or job.org_id != hr.org_id:
        raise not_found("Lowongan")
    rows = (
        await db.execute(
            select(JobApplication, User, PassportSettings)
            .join(User, User.id == JobApplication.user_id)
            .outerjoin(PassportSettings, PassportSettings.user_id == User.id)
            .where(JobApplication.job_id == job_id)
            .order_by(JobApplication.created_at.desc())
        )
    ).all()
    return [
        {
            "id": app.id,
            "user_id": u.id,
            "name": passport.display_name(u, public=False),
            "email": u.email,
            "department": u.department,
            "cover_note": app.cover_note,
            "passport_url": (passport.public_url(ps.public_slug) if ps and ps.public_link else app.passport_url),
            "status": app.status,
            "created_at": app.created_at,
        }
        for app, u, ps in rows
    ]


@router.get(
    "/reports/progress",
    response_model=ProgressReportOut,
    summary="Laporan progres bulanan (JSON atau unduh CSV dengan format=csv)",
    responses={200: {"content": {"text/csv": {}}}},
)
async def progress_report(
    db: DB,
    hr: HRAdmin,
    format: Literal["json", "csv"] = "json",
    months: Annotated[int, Query(ge=1, le=24)] = 6,
):
    rows = await org.progress_report(db, hr.org_id, months)
    if format == "csv":
        stamp = utcnow().strftime("%Y%m%d")
        return Response(
            org.report_csv(rows),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="levelin-progres-{stamp}.csv"'},
        )
    organization = await db.get(Organization, hr.org_id)
    return {
        "organization": organization.name,
        "k_anonymity_min": get_settings().k_anonymity_min,
        "rows": rows,
        "generated_at": utcnow(),
    }

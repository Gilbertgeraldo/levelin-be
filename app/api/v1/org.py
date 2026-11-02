from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy import select
from starlette.datastructures import UploadFile as FormFile

from app.api.deps import DB, HRAdmin, Pagination
from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.pagination import Page, paginate_list, paginate_scalars
from app.db.base import utcnow
from app.models import CustomMissionRequest, Invitation, Job, Occupation, Organization, PassportSettings, Skill, User
from app.schemas.jobs import JobOut
from app.schemas.org import (
    CandidateOut,
    CustomMissionOut,
    DashboardOut,
    InvitationIn,
    InvitationOut,
    InvitationResult,
    OrgJobIn,
    ProgressReportOut,
)
from app.services import org, passport, storage
from app.services.jobs import job_view

router = APIRouter(prefix="/org", tags=["Dashboard HR"])

MAX_CSV_BYTES = 1024 * 1024
MAX_INVITES = 500


@router.get("/dashboard", response_model=DashboardOut, summary="Ringkasan agregat organisasi (k-anonim)")
async def dashboard(db: DB, hr: HRAdmin):
    return await org.dashboard(db, hr.org_id)


@router.get(
    "/candidates",
    response_model=Page[CandidateOut],
    summary="Kandidat internal: hanya pekerja yang menyalakan 'Bagikan ke HR'",
)
async def candidates(
    db: DB,
    hr: HRAdmin,
    page: Pagination,
    skill_id: int | None = None,
    department: Annotated[str | None, Query(max_length=120)] = None,
):
    stmt = (
        select(User, PassportSettings)
        .join(PassportSettings, PassportSettings.user_id == User.id)
        .where(User.org_id == hr.org_id, User.role == "worker", PassportSettings.share_with_org.is_(True))
    )
    if department:
        stmt = stmt.where(User.department == department)
    rows = (await db.execute(stmt)).all()
    out = []
    for user, sharing in rows:
        skills = await passport.passport_skills(db, user.id, include_scores=False)
        refs = [{"id": s["skill"]["id"], "label": s["skill"]["label"]} for s in skills]
        if skill_id is not None and skill_id not in {r["id"] for r in refs}:
            continue
        out.append(
            {
                "user_id": user.id,
                "name": passport.display_name(user, public=False),
                "email": user.email,
                "department": user.department,
                "proven_skills": refs,
                "skill_count": len(refs),
                "passport_url": passport.public_url(sharing.public_slug) if sharing.public_link else None,
                "last_active_at": user.last_active_at,
            }
        )
    out.sort(key=lambda c: (-c["skill_count"], c["name"]))
    items, meta = paginate_list(out, page)
    return {"data": items, "meta": meta}


@router.post(
    "/invitations",
    status_code=201,
    response_model=InvitationResult,
    summary="Undang karyawan (JSON berisi email, atau CSV via multipart/form-data)",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {"schema": InvitationIn.model_json_schema()},
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "format": "binary", "description": "CSV kolom email,name,department,role"},
                            "department": {"type": "string"},
                            "role": {"type": "string", "enum": ["worker", "hr_admin"]},
                        },
                        "required": ["file"],
                    }
                },
            },
            "required": True,
        }
    },
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
    stmt = select(Invitation).where(Invitation.org_id == hr.org_id).order_by(Invitation.created_at.desc())
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
    return {"data": [job_view(j) for j in rows], "meta": meta}


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

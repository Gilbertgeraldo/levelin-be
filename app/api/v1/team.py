"""Endpoint atasan tim (peran manager): undang tim sendiri, lihat agregat tim dan anggota yang berbagi.

Atasan tidak pernah melihat hasil audit per orang. Agregat tim mengikuti ambang k-anonim yang sama
dengan dashboard HR.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import ValidationError
from sqlalchemy import select
from starlette.datastructures import UploadFile as FormFile

from app.api.deps import DB, Pagination, TeamManager
from app.core.errors import ApiError, not_found
from app.core.openapi import invitation_request_body
from app.core.pagination import Page, paginate_list, paginate_scalars
from app.models import Invitation, TeamMembership
from app.schemas.org import CandidateOut, DashboardOut, InvitationIn, InvitationOut, InvitationResult, MemberOut
from app.services import org, passport

router = APIRouter(prefix="/team", tags=["Atasan tim"])

MAX_CSV_BYTES = 1024 * 1024
MAX_INVITES = 200


@router.get("/dashboard", response_model=DashboardOut, summary="Agregat tim (k-anonim, tanpa data per orang)")
async def team_dashboard(db: DB, manager: TeamManager):
    return await org.team_dashboard(db, manager)


@router.get("/candidates", response_model=Page[CandidateOut], summary="Anggota tim yang membagikan Paspor Skill")
async def team_candidates(db: DB, manager: TeamManager, page: Pagination):
    members = await org.team_members(db, manager.id)
    items, meta = paginate_list(await org.candidates(db, members), page)
    return {"data": items, "meta": meta}


@router.get("/members", response_model=list[MemberOut], summary="Anggota tim (identitas saja)")
async def team_members(db: DB, manager: TeamManager):
    members = await org.team_members(db, manager.id)
    return [
        {
            "user_id": m.id,
            "name": passport.display_name(m, public=False),
            "email": m.email,
            "department": m.department,
            "role": m.role,
            "manager_names": [manager.full_name or manager.email or "Atasan"],
            "last_active_at": m.last_active_at,
            "is_online": org.is_online(m.last_active_at),
        }
        for m in sorted(members, key=lambda m: (m.full_name or m.email or ""))
    ]


@router.delete("/members/{user_id}", status_code=204, summary="Keluarkan anggota dari tim")
async def remove_member(user_id: uuid.UUID, db: DB, manager: TeamManager):
    membership = await db.get(TeamMembership, (manager.id, user_id))
    if membership is None:
        raise not_found("Anggota tim")
    await db.delete(membership)
    await db.commit()
    return Response(status_code=204)


@router.post(
    "/invitations",
    status_code=201,
    response_model=InvitationResult,
    summary="Undang anggota tim (JSON berisi email, atau CSV via multipart/form-data)",
    openapi_extra=invitation_request_body(InvitationIn, with_role=False),
)
async def invite_team(request: Request, db: DB, manager: TeamManager):
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
    else:
        try:
            body = InvitationIn.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            raise ApiError(400, "validation_error", "Data undangan tidak valid.") from exc
        entries = [{"email": e, "department": body.department} for e in body.emails]
        entries += [i.model_dump() for i in body.invitees]
    if not entries:
        raise ApiError(400, "no_invitees", "Isi minimal satu email.")
    if len(entries) > MAX_INVITES:
        raise ApiError(400, "too_many_invitees", f"Maksimal {MAX_INVITES} undangan per permintaan.")
    return await org.create_invitations(db, manager.org_id, manager, entries, manager=manager)


@router.get("/invitations", response_model=Page[InvitationOut], summary="Status undangan tim")
async def list_team_invitations(
    db: DB,
    manager: TeamManager,
    page: Pagination,
    status: Literal["pending", "accepted", "expired", "revoked"] | None = None,
):
    await org.expire_old_invitations(db, manager.org_id)
    stmt = select(Invitation).where(Invitation.manager_id == manager.id).order_by(Invitation.created_at.desc())
    if status:
        stmt = stmt.where(Invitation.status == status)
    rows, meta = await paginate_scalars(db, stmt, page)
    return {"data": rows, "meta": meta}


@router.delete("/invitations/{invitation_id}", status_code=204, summary="Batalkan undangan tim yang belum diterima")
async def revoke_invitation(invitation_id: uuid.UUID, db: DB, manager: TeamManager):
    invitation = await db.get(Invitation, invitation_id)
    if invitation is None or invitation.manager_id != manager.id:
        raise not_found("Undangan")
    if invitation.status != "pending":
        raise ApiError(409, "not_pending", "Undangan ini sudah tidak menunggu.")
    invitation.status = "revoked"
    await db.commit()
    return Response(status_code=204)

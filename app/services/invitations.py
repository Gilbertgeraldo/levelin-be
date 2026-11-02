from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import Invitation, TeamMembership, User

# Peran yang bisa didapat lewat undangan; tidak pernah menurunkan peran yang sudah lebih tinggi
_ROLE_RANK = {"worker": 0, "manager": 1, "hr_admin": 2, "reviewer": 2}


async def accept_pending_invitation(db: AsyncSession, user: User) -> list[Invitation]:
    """Terima undangan aktif untuk email pengguna: gabung organisasi dan/atau tim atasan.

    Undangan dari organisasi lain diabaikan bila pengguna sudah tergabung di sebuah organisasi.
    """
    if not user.email:
        return []
    now = utcnow()
    invitations = (
        await db.scalars(
            select(Invitation)
            .where(
                func.lower(Invitation.email) == user.email.lower(),
                Invitation.status == "pending",
                or_(Invitation.expires_at.is_(None), Invitation.expires_at > now),
            )
            .order_by(Invitation.created_at)
        )
    ).all()
    accepted = []
    for invitation in invitations:
        if user.org_id is not None and invitation.org_id != user.org_id:
            continue
        if user.org_id is None:
            user.org_id = invitation.org_id
            user.department = invitation.department
        if invitation.full_name and not user.full_name:
            user.full_name = invitation.full_name
        if _ROLE_RANK.get(invitation.role, 0) > _ROLE_RANK.get(user.role, 0):
            user.role = invitation.role
        if invitation.manager_id is not None and invitation.manager_id != user.id:
            exists = await db.get(TeamMembership, (invitation.manager_id, user.id))
            if exists is None:
                db.add(TeamMembership(manager_id=invitation.manager_id, member_id=user.id))
        invitation.status = "accepted"
        invitation.accepted_at = now
        accepted.append(invitation)
    return accepted

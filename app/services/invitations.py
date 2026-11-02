from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import Invitation, User


async def accept_pending_invitation(db: AsyncSession, user: User) -> Invitation | None:
    """Gabungkan pengguna ke organisasi bila ada undangan aktif untuk email-nya."""
    if user.org_id is not None or not user.email:
        return None
    now = utcnow()
    invitation = await db.scalar(
        select(Invitation)
        .where(
            func.lower(Invitation.email) == user.email.lower(),
            Invitation.status == "pending",
            or_(Invitation.expires_at.is_(None), Invitation.expires_at > now),
        )
        .order_by(Invitation.created_at.desc())
        .limit(1)
    )
    if invitation is None:
        return None
    user.org_id = invitation.org_id
    user.department = invitation.department
    if invitation.full_name and not user.full_name:
        user.full_name = invitation.full_name
    if invitation.role == "hr_admin":
        user.role = "hr_admin"
    invitation.status = "accepted"
    invitation.accepted_at = now
    return invitation

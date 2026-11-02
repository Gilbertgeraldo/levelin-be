import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response
from sqlalchemy import func, select

from app.api.deps import DB, CurrentUser
from app.core.errors import not_found
from app.core.pagination import Page, PageParams, paginate_scalars
from app.db.base import utcnow
from app.models import DeviceToken, Notification
from app.schemas.notifications import DeviceTokenIn, DeviceTokenOut, NotificationOut

router = APIRouter(tags=["Notifikasi & pengingat"])


@router.post("/me/device-tokens", status_code=201, response_model=DeviceTokenOut, summary="Daftarkan token FCM perangkat")
async def register_device(body: DeviceTokenIn, db: DB, user: CurrentUser):
    device = await db.scalar(select(DeviceToken).where(DeviceToken.token == body.token))
    if device is None:
        device = DeviceToken(user_id=user.id, token=body.token, platform=body.platform)
        db.add(device)
    else:
        # Token yang sama di perangkat yang sekarang dipakai akun lain
        device.user_id = user.id
        device.platform = body.platform
        device.last_seen_at = utcnow()
    await db.commit()
    return device


@router.delete("/me/device-tokens/{token:path}", status_code=204, summary="Hapus token perangkat (mis. saat logout)")
async def unregister_device(token: str, db: DB, user: CurrentUser):
    device = await db.scalar(select(DeviceToken).where(DeviceToken.token == token, DeviceToken.user_id == user.id))
    if device is not None:
        await db.delete(device)
        await db.commit()
    return Response(status_code=204)


@router.get("/notifications", response_model=Page[NotificationOut], summary="Daftar notifikasi in-app")
async def list_notifications(
    response: Response,
    db: DB,
    user: CurrentUser,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    unread_only: bool = False,
):
    stmt = select(Notification).where(Notification.user_id == user.id).order_by(Notification.created_at.desc())
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    rows, meta = await paginate_scalars(db, stmt, PageParams(page, limit))
    unread = await db.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == user.id, Notification.read_at.is_(None)
        )
    )
    response.headers["X-Unread-Count"] = str(unread or 0)
    return {"data": rows, "meta": meta}


@router.patch("/notifications/{notification_id}/read", response_model=NotificationOut, summary="Tandai sudah dibaca")
async def mark_read(notification_id: uuid.UUID, db: DB, user: CurrentUser):
    note = await db.get(Notification, notification_id)
    if note is None or note.user_id != user.id:
        raise not_found("Notifikasi")
    if note.read_at is None:
        note.read_at = utcnow()
        await db.commit()
    return note

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeviceToken, Notification
from app.services.push import get_fcm


async def notify(
    db: AsyncSession,
    user_id: uuid.UUID,
    type: str,
    title: str,
    body: str,
    data: dict | None = None,
    *,
    push: bool = True,
) -> Notification:
    """Simpan notifikasi in-app dan (bila FCM dikonfigurasi) kirim push ke semua perangkat pengguna."""
    note = Notification(user_id=user_id, type=type, title=title, body=body, data=data or {})
    db.add(note)
    await db.flush()
    fcm = get_fcm()
    if push and fcm is not None:
        tokens = (await db.scalars(select(DeviceToken).where(DeviceToken.user_id == user_id))).all()
        payload = {**(data or {}), "notification_id": str(note.id), "type": type}
        for device in tokens:
            if await fcm.send(device.token, title, body, payload) == "invalid":
                await db.delete(device)
    return note

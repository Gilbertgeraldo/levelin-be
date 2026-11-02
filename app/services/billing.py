import base64
import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.base import utcnow
from app.models import Attempt, Organization, Payment, Subscription, User


def plan_catalog() -> dict[str, dict]:
    s = get_settings()
    return {
        "plus": {
            "name": "Levelin Plus",
            "price": s.plan_plus_price,
            "mission_quota": s.plan_plus_quota,
            "days": s.plan_plus_days,
        },
        "paket_jalur": {
            "name": "Paket Jalur",
            "price": s.plan_jalur_price,
            "mission_quota": s.plan_jalur_quota,
            "days": s.plan_jalur_days,
        },
    }


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


async def active_subscription(db: AsyncSession, user_id: uuid.UUID) -> Subscription | None:
    now = utcnow()
    return await db.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == "active",
            Subscription.period_start <= now,
            Subscription.period_end > now,
        )
        .order_by(Subscription.period_end.desc())
        .limit(1)
    )


async def mission_quota(db: AsyncSession, user: User) -> dict:
    now = utcnow()
    sub = await active_subscription(db, user.id)
    if sub is not None:
        plan, limit, start, end = sub.plan, sub.mission_quota, sub.period_start, sub.period_end
    elif user.org_id is not None:
        org = await db.get(Organization, user.org_id)
        limit = (org.mission_quota_per_user if org else None) or get_settings().org_member_mission_quota
        plan = "organisasi"
        start, end = month_bounds(now)
    else:
        plan, limit = "free", get_settings().free_mission_quota
        start, end = month_bounds(now)
    used = await db.scalar(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.user_id == user.id, Attempt.created_at >= start, Attempt.created_at < end)
    )
    used = used or 0
    return {
        "plan": plan,
        "mission_quota": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "period_start": start,
        "period_end": end,
    }


async def activate_subscription(db: AsyncSession, payment: Payment) -> Subscription:
    catalog = plan_catalog()[payment.plan]
    now = utcnow()
    current = await active_subscription(db, payment.user_id)
    # Perpanjangan paket yang sama dimulai setelah periode berjalan habis
    start = current.period_end if current is not None and current.plan == payment.plan else now
    sub = Subscription(
        user_id=payment.user_id,
        plan=payment.plan,
        status="active",
        mission_quota=catalog["mission_quota"],
        period_start=start,
        period_end=start + timedelta(days=catalog["days"]),
        payment_id=payment.id,
    )
    db.add(sub)
    return sub


async def cancel_subscription(db: AsyncSession, payment: Payment) -> None:
    subs = await db.scalars(
        select(Subscription).where(Subscription.payment_id == payment.id, Subscription.status == "active")
    )
    for sub in subs.all():
        sub.status = "cancelled"


# ---------- Midtrans Snap ----------


def _snap_url() -> str:
    if get_settings().midtrans_is_production:
        return "https://app.midtrans.com/snap/v1/transactions"
    return "https://app.sandbox.midtrans.com/snap/v1/transactions"


async def create_snap_transaction(payment: Payment, user: User, item_name: str) -> dict:
    server_key = get_settings().midtrans_server_key
    if not server_key:
        raise ApiError(503, "payment_not_configured", "Pembayaran belum tersedia saat ini.")
    auth = base64.b64encode(f"{server_key}:".encode()).decode()
    body = {
        "transaction_details": {"order_id": payment.order_id, "gross_amount": payment.amount},
        "item_details": [
            {"id": payment.plan, "price": payment.amount, "quantity": 1, "name": item_name[:50]}
        ],
        "customer_details": {"email": user.email, "first_name": (user.full_name or "")[:50]},
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                _snap_url(),
                json=body,
                headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise ApiError(503, "payment_gateway_error", "Gagal menghubungi payment gateway.") from exc


def verify_midtrans_signature(payload: dict) -> bool:
    server_key = get_settings().midtrans_server_key
    if not server_key:
        return False
    raw = (
        f"{payload.get('order_id', '')}{payload.get('status_code', '')}"
        f"{payload.get('gross_amount', '')}{server_key}"
    )
    expected = hashlib.sha512(raw.encode()).hexdigest()
    return hmac.compare_digest(expected, str(payload.get("signature_key", "")))


def map_midtrans_status(transaction_status: str | None, fraud_status: str | None) -> str:
    if transaction_status == "capture":
        return "paid" if fraud_status in (None, "accept") else "pending"
    return {
        "settlement": "paid",
        "pending": "pending",
        "deny": "failed",
        "cancel": "failed",
        "failure": "failed",
        "expire": "expired",
        "refund": "refunded",
        "chargeback": "refunded",
    }.get(transaction_status or "", "pending")

import logging
import secrets

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import DB, CurrentUser
from app.core.errors import ApiError
from app.db.base import utcnow
from app.models import Payment
from app.schemas.billing import CheckoutIn, CheckoutOut, SubscriptionOut
from app.services import billing
from app.services.notifications import notify

logger = logging.getLogger("levelin.billing")

router = APIRouter(tags=["Pembayaran"])


@router.get("/me/subscription", response_model=SubscriptionOut, summary="Paket aktif dan kuota misi")
async def get_subscription(db: DB, user: CurrentUser):
    sub = await billing.active_subscription(db, user.id)
    return {
        "quota": await billing.mission_quota(db, user),
        "active": {
            "plan": sub.plan,
            "status": sub.status,
            "period_start": sub.period_start,
            "period_end": sub.period_end,
        }
        if sub
        else None,
        "available_plans": [{"code": code, **info} for code, info in billing.plan_catalog().items()],
    }


@router.post("/billing/checkout", status_code=201, response_model=CheckoutOut, summary="Buat transaksi lewat payment gateway")
async def checkout(body: CheckoutIn, db: DB, user: CurrentUser):
    plan = billing.plan_catalog()[body.plan]
    payment = Payment(
        order_id=f"LVL-{utcnow():%Y%m%d}-{secrets.token_hex(5).upper()}",
        user_id=user.id,
        plan=body.plan,
        amount=plan["price"],
        currency="IDR",
        status="pending",
        provider="midtrans",
    )
    snap = await billing.create_snap_transaction(payment, user, plan["name"])
    payment.provider_token = snap.get("token")
    payment.redirect_url = snap.get("redirect_url")
    db.add(payment)
    await db.commit()
    return {
        "payment_id": payment.id,
        "order_id": payment.order_id,
        "plan": payment.plan,
        "amount": payment.amount,
        "currency": payment.currency,
        "snap_token": payment.provider_token,
        "redirect_url": payment.redirect_url,
    }


@router.post("/webhooks/payment", summary="Notifikasi dari payment gateway (verifikasi signature)")
async def payment_webhook(request: Request, db: DB):
    try:
        payload = await request.json()
    except ValueError as exc:
        raise ApiError(400, "invalid_payload", "Payload bukan JSON.") from exc
    if not isinstance(payload, dict) or not billing.verify_midtrans_signature(payload):
        raise ApiError(401, "invalid_signature", "Signature tidak valid.")

    payment = await db.scalar(select(Payment).where(Payment.order_id == payload.get("order_id")))
    if payment is None:
        raise ApiError(404, "not_found", "Transaksi tidak ditemukan.")
    try:
        gross = round(float(payload.get("gross_amount", 0)))
    except (TypeError, ValueError):
        gross = -1
    if gross != payment.amount:
        logger.warning("Payment amount mismatch for %s", payment.order_id)
        raise ApiError(400, "amount_mismatch", "Nominal transaksi tidak cocok.")

    new_status = billing.map_midtrans_status(payload.get("transaction_status"), payload.get("fraud_status"))
    payment.raw_notification = payload
    plan_name = billing.plan_catalog()[payment.plan]["name"]
    if payment.status == "paid" and new_status == "refunded":
        payment.status = "refunded"
        await billing.cancel_subscription(db, payment)
        if payment.user_id is not None:
            await notify(
                db,
                payment.user_id,
                "payment",
                "Pembayaran dikembalikan",
                f"Dana paket {plan_name} sudah dikembalikan, jadi paketnya tidak aktif lagi.",
                {"order_id": payment.order_id},
            )
    # Idempoten: notifikasi berulang untuk transaksi lunas/refund diabaikan
    elif payment.status not in ("paid", "refunded"):
        payment.status = new_status
        if new_status == "paid" and payment.user_id is not None:
            payment.paid_at = utcnow()
            await billing.activate_subscription(db, payment)
            await notify(
                db,
                payment.user_id,
                "payment",
                "Pembayaran berhasil",
                f"Paket {plan_name} sudah aktif. Selamat naik level!",
                {"order_id": payment.order_id},
            )
    await db.commit()
    return {"ok": True, "status": payment.status}

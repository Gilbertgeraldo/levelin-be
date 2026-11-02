"""Peninjau, bank misi, pembayaran, dan hak hapus akun."""

import hashlib

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Payment, User
from app.services import supabase_admin
from tests.conftest import auth
from tests.test_worker_flow import M1_ANSWERS, create_audit, mission_id, onboard

pytestmark = pytest.mark.anyio

REVIEWER = {"email": "reviewer@levelin.test", "role": "reviewer", "name": "Rudi Reviewer"}


async def test_reviewer_override_revokes_passport_and_readiness(client):
    worker = auth()
    await onboard(client, worker)
    before = (await create_audit(client, worker, story=None))["summary"]["readiness_score"]
    attempt = (await client.post(f"/v1/missions/{await mission_id(client, worker, 'rapikan-data-penjualan')}/attempts", headers=worker)).json()
    result = (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=worker, json={"answers": M1_ANSWERS})).json()
    assert result["passed"] and result["readiness_score"] > before
    await client.post(f"/v1/attempts/{attempt['id']}/appeal", headers=worker, json={"reason": "Mohon dicek ulang rubriknya."})

    reviewer = auth(**REVIEWER)
    assert (await client.get("/v1/admin/reviews", headers=worker)).status_code == 403
    queue = (await client.get("/v1/admin/reviews", headers=reviewer)).json()["data"]
    assert queue[0]["kind"] == "appeal" and queue[0]["mission"]["answer_key"]
    assert (await client.post(f"/v1/admin/reviews/{queue[0]['id']}", headers=reviewer,
                              json={"decision": "override"})).status_code == 400
    r = await client.post(f"/v1/admin/reviews/{queue[0]['id']}", headers=reviewer,
                          json={"decision": "override", "passed": False, "note": "Ringkasan belum menjawab brief."})
    assert r.status_code == 200

    result = (await client.get(f"/v1/attempts/{attempt['id']}/result", headers=worker)).json()
    assert result["passed"] is False and result["passport_evidence_id"] is None
    assert (await client.get("/v1/passport", headers=worker)).json()["total_skills"] == 0
    assert (await client.get("/v1/audits/latest", headers=worker)).json()["summary"]["readiness_score"] == before
    notes = (await client.get("/v1/notifications", headers=worker)).json()["data"]
    assert notes[0]["type"] == "review_result" and "belum lulus" in notes[0]["body"]

    # Sampel lain untuk percobaan yang sama: disetujui tanpa mengubah hasil
    rest = (await client.get("/v1/admin/reviews", headers=reviewer)).json()["data"]
    r = await client.post(f"/v1/admin/reviews/{rest[0]['id']}", headers=reviewer, json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["decision"] == "approve"


async def test_admin_mission_upsert_with_combined_fields(client):
    reviewer = auth(**REVIEWER)
    mission = {
        "slug": "misi-uji",
        "title": "Misi Uji",
        "brief": "Brief misi uji yang cukup panjang.",
        "answer_fields": [{"id": "subjek", "label": "Subjek"}, {"id": "isi", "label": "Isi", "type": "long_text", "rows": 6}],
        "rubric": [{"id": "r1", "criterion": "Ketepatan"}],
        "answer_key": [{"id": "c1", "field_id": "isi", "field_ids": ["subjek", "isi"], "type": "regex", "expected": "andi"}],
        "kind": "dok",
        "duration_label": "20 menit",
        "skill_ids": [1],
    }
    r = await client.post("/v1/admin/missions", headers=reviewer, json=mission)
    assert r.status_code == 201 and r.json()["kind"] == "dok" and r.json()["answer_key"][0]["field_ids"] == ["subjek", "isi"]
    r = await client.post("/v1/admin/missions", headers=reviewer, json={**mission, "title": "Misi Uji v2"})
    assert r.status_code == 200 and r.json()["title"] == "Misi Uji v2"
    bad = {**mission, "answer_key": [{**mission["answer_key"][0], "field_ids": ["subjek", "tidak_ada"]}]}
    assert (await client.post("/v1/admin/missions", headers=reviewer, json=bad)).status_code == 400


async def test_billing_checkout_webhook_and_refund(client, sessionmaker, monkeypatch):
    h = auth("mandiri@contoh.test", name="Rani")
    sub = (await client.get("/v1/me/subscription", headers=h)).json()
    assert sub["quota"]["plan"] == "free" and sub["active"] is None
    r = await client.post("/v1/billing/checkout", headers=h, json={"plan": "plus"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "payment_not_configured"

    monkeypatch.setattr(get_settings(), "midtrans_server_key", "K")
    async with sessionmaker() as db:
        user = await db.scalar(select(User).where(User.email == "mandiri@contoh.test"))
        db.add(Payment(order_id="O1", user_id=user.id, plan="plus", amount=49000, status="pending"))
        await db.commit()

    async def send(status: str, signature: str | None = None):
        return await client.post("/v1/webhooks/payment", json={
            "order_id": "O1", "gross_amount": "49000.00", "status_code": "200", "transaction_status": status,
            "signature_key": signature or hashlib.sha512(b"O120049000.00K").hexdigest(),
        })

    assert (await send("settlement", "palsu")).status_code == 401
    assert (await send("settlement")).json()["status"] == "paid"
    sub = (await client.get("/v1/me/subscription", headers=h)).json()
    assert sub["active"]["plan"] == "plus" and sub["quota"]["mission_quota"] == 30
    assert (await send("partial_refund")).json()["status"] == "paid"
    assert (await send("refund")).json()["status"] == "refunded"
    assert (await client.get("/v1/me/subscription", headers=h)).json()["active"] is None
    assert (await send("settlement")).json()["status"] == "refunded"  # kiriman ulang lama diabaikan


async def test_delete_keeps_data_when_auth_delete_fails(client, monkeypatch):
    h = auth()
    await onboard(client, h)
    await create_audit(client, h, story=None)

    async def failed(_):
        return False

    monkeypatch.setattr(supabase_admin, "configured", lambda: True)
    monkeypatch.setattr(supabase_admin, "delete_auth_user", failed)
    r = await client.delete("/v1/me", headers=h)
    assert r.status_code == 503 and r.json()["error"]["code"] == "auth_delete_failed"
    assert (await client.get("/v1/audits", headers=h)).json()["meta"]["total"] == 1

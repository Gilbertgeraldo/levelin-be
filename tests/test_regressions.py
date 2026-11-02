"""Tes regresi untuk temuan audit: setiap kasus pernah terbukti gagal sebelum diperbaiki."""

import hashlib

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Audit, Organization, Payment, User
from app.services import supabase_admin
from scripts.seed_demo import seed_sample_workers
from tests.conftest import FakeLLM, auth, parse_sse, sse_text, use_llm
from tests.test_worker_flow import CORRECT_REKAP, _mission_id, _onboard

pytestmark = pytest.mark.anyio

AUDIT_BODY = {"occupation_code": "4110", "tasks": [{"template_task_id": 1, "hours_per_week": 10}]}


async def _attempt(client, h, slug="rekap-penjualan-mingguan"):
    mission_id = await _mission_id(client, h, slug)
    return (await client.post(f"/v1/missions/{mission_id}/attempts", headers=h)).json()


def _grade(first_feedback: str, overall: str) -> dict:
    return {
        "criteria": [
            {"id": "r_verifikasi", "level": 2, "feedback": first_feedback},
            {"id": "r_ringkasan", "level": 2, "feedback": "Cukup jelas."},
            {"id": "r_saran", "level": 2, "feedback": "Saran masih umum."},
        ],
        "overall_feedback": overall,
    }


async def test_rubric_feedback_is_filtered(client):
    h = auth()
    await _onboard(client, h)
    wrong = {**CORRECT_REKAP, "total_penjualan": "1"}
    use_llm(FakeLLM({"RubricGrade": _grade(
        "Kalau begini kamu bisa di-PHK. Totalnya seharusnya Rp162 juta. Wilayah sudah benar.",
        "Posisimu akan hilang.",
    )}))
    attempt = await _attempt(client, h)
    result = (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": wrong})).json()
    feedback = result["rubric"]["criteria"][0]["feedback"]
    assert "PHK" not in feedback and "162" not in feedback
    assert "Wilayah sudah benar." in feedback
    assert "hilang" not in result["rubric"]["overall_feedback"]


async def test_rubric_may_quote_answer_the_user_got_right(client):
    h = auth()
    await _onboard(client, h)
    use_llm(FakeLLM({"RubricGrade": _grade("Total Rp162 juta sudah tepat.", "Bagus.")}))
    attempt = await _attempt(client, h)
    result = (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": CORRECT_REKAP})).json()
    assert "162 juta" in result["rubric"]["criteria"][0]["feedback"]


async def test_chat_filter_catches_units_and_percent(client):
    h = auth()
    await _onboard(client, h)
    attempt = await _attempt(client, h)
    use_llm(FakeLLM(chunks=["Totalnya Rp162 juta. ", "Lalu naik 8 persen. ", "Cek lagi tabelnya."]))
    r = await client.post(f"/v1/attempts/{attempt['id']}/messages", headers=h, json={"content": "jawabannya apa?"})
    text = sse_text(parse_sse(r.text))
    assert "162" not in text and "8 persen" not in text
    assert "Cek lagi tabelnya." in text


async def test_readiness_follows_passport(client):
    h = auth()
    await _onboard(client, h)
    before = (await client.post("/v1/audits", headers=h, json=AUDIT_BODY)).json()["summary"]["readiness_score"]
    attempt = await _attempt(client, h)
    await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": CORRECT_REKAP})
    latest = (await client.get("/v1/audits/latest", headers=h)).json()["summary"]["readiness_score"]
    assert latest > before

    reviewer = auth("reviewer@levelin.test", role="reviewer", name="Rudi")
    queue = (await client.get("/v1/admin/reviews", headers=reviewer)).json()["data"]
    await client.post(f"/v1/admin/reviews/{queue[0]['id']}", headers=reviewer,
                      json={"decision": "override", "new_score": 10})
    reverted = (await client.get("/v1/audits/latest", headers=h)).json()["summary"]["readiness_score"]
    assert reverted == before


async def test_override_updates_passport_score(client):
    h = auth()
    await _onboard(client, h)
    attempt = await _attempt(client, h)
    await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": CORRECT_REKAP})
    reviewer = auth("reviewer@levelin.test", role="reviewer", name="Rudi")
    queue = (await client.get("/v1/admin/reviews", headers=reviewer)).json()["data"]
    await client.post(f"/v1/admin/reviews/{queue[0]['id']}", headers=reviewer,
                      json={"decision": "override", "new_score": 71})
    passport = (await client.get("/v1/passport", headers=h)).json()
    assert passport["skills"][0]["best_score"] == 71


async def test_refund_after_paid_cancels_subscription(client, sessionmaker, monkeypatch):
    h = auth("mandiri@contoh.test", name="Rani")
    await client.get("/v1/me", headers=h)
    monkeypatch.setattr(get_settings(), "midtrans_server_key", "K")
    async with sessionmaker() as db:
        user = await db.scalar(select(User).where(User.email == "mandiri@contoh.test"))
        db.add(Payment(order_id="O1", user_id=user.id, plan="plus", amount=49000, status="pending"))
        await db.commit()

    async def send(status: str):
        signature = hashlib.sha512(b"O120049000.00K").hexdigest()
        return await client.post("/v1/webhooks/payment", json={
            "order_id": "O1", "gross_amount": "49000.00", "status_code": "200",
            "transaction_status": status, "signature_key": signature,
        })

    await send("settlement")
    assert (await client.get("/v1/me/subscription", headers=h)).json()["active"]["plan"] == "plus"
    assert (await send("partial_refund")).json()["status"] == "paid"
    assert (await send("refund")).json()["status"] == "refunded"
    assert (await client.get("/v1/me/subscription", headers=h)).json()["active"] is None
    # Notifikasi lama yang terkirim ulang tidak boleh menghidupkan paket lagi
    assert (await send("settlement")).json()["status"] == "refunded"
    assert (await client.get("/v1/me/subscription", headers=h)).json()["active"] is None


async def _set_departments(sessionmaker, sizes: dict[str, int]) -> int:
    async with sessionmaker() as db:
        org = await db.scalar(select(Organization).where(Organization.slug == "contoh-sejahtera"))
        await seed_sample_workers(db, org, sum(sizes.values()))
        users = list((await db.scalars(
            select(User).where(User.org_id == org.id, User.role == "worker").order_by(User.email)
        )).all())
        names = [name for name, n in sizes.items() for _ in range(n)]
        for user, name in zip(users, names, strict=True):
            user.department = name
        await db.commit()
        solo = users[-1]
        return (await db.scalar(select(Audit).where(Audit.user_id == solo.id))).readiness_score


async def test_dashboard_blocks_inference_by_subtraction(client, sessionmaker):
    h = auth(email="hr@contoh-sejahtera.test", role="hr_admin", name="Hana")
    await client.get("/v1/me", headers=h)
    await _set_departments(sessionmaker, {"Tim Besar": 5, "Solo": 1})
    body = (await client.get("/v1/org/dashboard", headers=h)).json()
    assert [d["department"] for d in body["departments"]] == ["Lainnya"]
    assert body["departments"][0]["people"] == 6


async def test_dashboard_keeps_department_when_remainder_is_safe(client, sessionmaker):
    h = auth(email="hr@contoh-sejahtera.test", role="hr_admin", name="Hana")
    await client.get("/v1/me", headers=h)
    await _set_departments(sessionmaker, {"A": 5, "B": 2, "C": 3})
    rows = (await client.get("/v1/org/dashboard", headers=h)).json()["departments"]
    assert {d["department"]: d["people"] for d in rows} == {"A": 5, "Lainnya": 5}
    assert all(not d["suppressed"] for d in rows)


async def test_delete_keeps_data_when_auth_delete_fails(client, monkeypatch):
    h = auth()
    await _onboard(client, h)
    await client.post("/v1/audits", headers=h, json=AUDIT_BODY)

    async def failed(_):
        return False

    monkeypatch.setattr(supabase_admin, "configured", lambda: True)
    monkeypatch.setattr(supabase_admin, "delete_auth_user", failed)
    r = await client.delete("/v1/me", headers=h)
    assert r.status_code == 503 and r.json()["error"]["code"] == "auth_delete_failed"
    assert (await client.get("/v1/audits", headers=h)).json()["meta"]["total"] == 1


async def test_notifications_default_limit(client):
    r = await client.get("/v1/notifications", headers=auth())
    assert r.json()["meta"]["limit"] == 20

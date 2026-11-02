"""Dashboard HR (agregat k-anonim), peninjau, dan pembayaran."""

import hashlib

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Organization, Payment, User
from scripts.seed_demo import seed_sample_workers
from tests.conftest import HR_EMAIL, auth
from tests.test_worker_flow import CORRECT_REKAP, _mission_id, _onboard

pytestmark = pytest.mark.anyio

HR = {"email": HR_EMAIL, "role": "hr_admin", "name": "Hana HR"}
PDF = b"%PDF-1.4\n%demo\n"


async def _org(sessionmaker) -> Organization:
    async with sessionmaker() as db:
        return await db.scalar(select(Organization).where(Organization.slug == "contoh-sejahtera"))


async def test_hr_dashboard_respects_k_anonymity(client, sessionmaker):
    h = auth(**HR)
    me = (await client.get("/v1/me", headers=h)).json()
    assert me["role"] == "hr_admin" and me["organization"]["slug"] == "contoh-sejahtera"

    # Belum ada cukup orang: angka disembunyikan
    r = await client.get("/v1/org/dashboard", headers=h)
    assert r.status_code == 200
    assert r.json()["overview"]["suppressed"] is True and r.json()["overview"]["avg_readiness"] is None

    org = await _org(sessionmaker)
    async with sessionmaker() as db:
        await seed_sample_workers(db, org, 16)  # 6/5/5 per departemen
        await db.commit()

    body = (await client.get("/v1/org/dashboard", headers=h)).json()
    assert body["overview"]["members"] == 16 and body["overview"]["suppressed"] is False
    assert body["overview"]["avg_readiness"] is not None
    assert {d["department"] for d in body["departments"]} == {"Operasional", "Keuangan", "Layanan Pelanggan"}
    assert all(d["people"] >= 5 for d in body["departments"])
    assert all(s["people"] >= 5 for s in body["top_proven_skills"])
    # Tidak ada data audit per orang di dashboard
    assert "tasks" not in r.text and "story" not in r.text

    r = await client.get("/v1/org/candidates", headers=h, params={"limit": 100})
    candidates = r.json()["data"]
    async with sessionmaker() as db:
        from app.models import PassportSettings

        sharing = (await db.scalars(select(PassportSettings).where(PassportSettings.share_with_org.is_(True)))).all()
    assert len(candidates) == len(sharing)
    assert all("readiness" not in str(c) for c in candidates)

    r = await client.get("/v1/org/reports/progress", headers=h, params={"format": "csv"})
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0] == "month,active_people,audited_people,avg_readiness,missions_passed,suppressed"
    r = await client.get("/v1/org/reports/progress", headers=h)
    assert r.json()["organization"] == "PT Contoh Sejahtera"


async def test_hr_invitations_custom_missions_jobs(client):
    h = auth(**HR)
    r = await client.post("/v1/org/invitations", headers=h,
                          json={"emails": ["a@contoh.test", "a@contoh.test", "salah"], "department": "Keuangan"})
    assert r.status_code == 201
    body = r.json()
    assert [c["email"] for c in body["created"]] == ["a@contoh.test"]
    assert {s["reason"] for s in body["skipped"]} == {"duplicate", "invalid_email"}

    csv_bytes = b"email,name,department\nb@contoh.test,Bima,Operasional\na@contoh.test,Ani,Keuangan\n"
    r = await client.post("/v1/org/invitations", headers=h, files={"file": ("karyawan.csv", csv_bytes, "text/csv")})
    assert r.status_code == 201
    assert [c["email"] for c in r.json()["created"]] == ["b@contoh.test"]
    assert r.json()["skipped"] == [{"email": "a@contoh.test", "reason": "already_invited"}]

    r = await client.get("/v1/org/invitations", headers=h, params={"status": "pending"})
    assert r.json()["meta"]["total"] == 3  # budi (seed) + a + b

    # Karyawan yang diundang otomatis masuk organisasi saat login pertama
    me = (await client.get("/v1/me", headers=auth("b@contoh.test", name="Bima"))).json()
    assert me["organization"]["slug"] == "contoh-sejahtera" and me["department"] == "Operasional"

    r = await client.post("/v1/org/custom-missions", headers=h, data={"title": "Misi SOP Gudang"},
                          files={"file": ("sop.pdf", PDF, "application/pdf")})
    assert r.status_code == 201 and r.json()["status"] == "requested"
    r = await client.post("/v1/org/custom-missions", headers=h, data={"title": "Misi palsu"},
                          files={"file": ("virus.pdf", b"MZ bukan pdf", "application/pdf")})
    assert r.status_code == 415

    r = await client.post("/v1/org/jobs", headers=h,
                          json={"title": "Staf Data Internal", "skill_ids": [5], "occupation_code": "4110"})
    assert r.status_code == 201 and r.json()["is_internal"] and r.json()["source_verified"]
    r = await client.get("/v1/org/jobs", headers=h)
    assert r.json()["meta"]["total"] == 3

    # Pekerja tidak boleh mengakses endpoint HR; pekerja organisasi lain tidak melihat lowongan internal
    assert (await client.get("/v1/org/dashboard", headers=auth())).status_code == 403
    outsider = (await client.get("/v1/jobs/matches", headers=auth("luar@lain.test"), params={"limit": 50})).json()
    assert not any(j["is_internal"] for j in outsider["data"])


async def test_reviewer_override_updates_passport_and_notifies(client):
    worker = auth()
    await _onboard(client, worker)
    mission_id = await _mission_id(client, worker, "rekap-penjualan-mingguan")
    attempt = (await client.post(f"/v1/missions/{mission_id}/attempts", headers=worker)).json()
    await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=worker, json={"answers": CORRECT_REKAP})
    await client.post(f"/v1/attempts/{attempt['id']}/appeal", headers=worker, json={"reason": "Mohon dicek ulang rubriknya."})
    assert (await client.get("/v1/passport", headers=worker)).json()["total_skills"] == 3

    reviewer = auth("reviewer@levelin.test", role="reviewer", name="Rudi Reviewer")
    assert (await client.get("/v1/admin/reviews", headers=worker)).status_code == 403
    queue = (await client.get("/v1/admin/reviews", headers=reviewer)).json()["data"]
    assert queue[0]["kind"] == "appeal"  # banding didahulukan
    assert queue[0]["mission"]["answer_key"]  # peninjau melihat kunci jawaban

    r = await client.post(f"/v1/admin/reviews/{queue[0]['id']}", headers=reviewer, json={"decision": "override"})
    assert r.status_code == 400  # new_score wajib untuk override
    r = await client.post(f"/v1/admin/reviews/{queue[0]['id']}", headers=reviewer,
                          json={"decision": "override", "new_score": 40, "note": "Ringkasan belum menjawab brief."})
    assert r.status_code == 200 and r.json()["new_score"] == 40

    result = (await client.get(f"/v1/attempts/{attempt['id']}/result", headers=worker)).json()
    assert result["score"] == 40 and result["passed"] is False
    assert (await client.get("/v1/passport", headers=worker)).json()["total_skills"] == 0
    notes = (await client.get("/v1/notifications", headers=worker)).json()["data"]
    assert notes[0]["type"] == "review_result"
    r = await client.patch(f"/v1/notifications/{notes[0]['id']}/read", headers=worker)
    assert r.json()["read_at"] is not None


async def test_admin_mission_upsert(client):
    reviewer = auth("reviewer@levelin.test", role="reviewer")
    mission = {
        "slug": "misi-uji",
        "title": "Misi Uji",
        "brief": "Brief misi uji yang cukup panjang.",
        "answer_fields": [{"id": "jawaban", "label": "Jawaban", "type": "number"}],
        "rubric": [{"id": "r1", "criterion": "Ketepatan"}],
        "answer_key": [{"id": "k1", "field_id": "jawaban", "type": "number", "expected": 42}],
        "skill_ids": [1],
    }
    r = await client.post("/v1/admin/missions", headers=reviewer, json=mission)
    assert r.status_code == 201 and r.json()["answer_key"][0]["expected"] == 42
    r = await client.post("/v1/admin/missions", headers=reviewer, json={**mission, "title": "Misi Uji v2"})
    assert r.status_code == 200 and r.json()["title"] == "Misi Uji v2"
    bad = {**mission, "answer_key": [{"id": "k1", "field_id": "tidak_ada", "type": "number", "expected": 1}]}
    assert (await client.post("/v1/admin/missions", headers=reviewer, json=bad)).status_code == 400


async def test_billing_checkout_and_webhook(client, sessionmaker, monkeypatch):
    h = auth("mandiri@contoh.test", name="Rani")
    sub = (await client.get("/v1/me/subscription", headers=h)).json()
    assert sub["quota"]["plan"] == "free" and sub["active"] is None
    r = await client.post("/v1/billing/checkout", headers=h, json={"plan": "plus"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "payment_not_configured"

    monkeypatch.setattr(get_settings(), "midtrans_server_key", "SB-test-key")
    async with sessionmaker() as db:
        user = await db.scalar(select(User).where(User.email == "mandiri@contoh.test"))
        db.add(Payment(order_id="LVL-TEST-1", user_id=user.id, plan="plus", amount=49000, status="pending"))
        await db.commit()

    payload = {"order_id": "LVL-TEST-1", "status_code": "200", "gross_amount": "49000.00",
               "transaction_status": "settlement"}
    r = await client.post("/v1/webhooks/payment", json={**payload, "signature_key": "palsu"})
    assert r.status_code == 401
    payload["signature_key"] = hashlib.sha512(b"LVL-TEST-120049000.00SB-test-key").hexdigest()
    r = await client.post("/v1/webhooks/payment", json=payload)
    assert r.status_code == 200 and r.json()["status"] == "paid"
    assert (await client.post("/v1/webhooks/payment", json=payload)).json()["status"] == "paid"  # idempoten

    sub = (await client.get("/v1/me/subscription", headers=h)).json()
    assert sub["active"]["plan"] == "plus" and sub["quota"]["mission_quota"] == 30
    notes = (await client.get("/v1/notifications", headers=h)).json()["data"]
    assert [n["type"] for n in notes] == ["payment"]

"""Alur lengkap aplikasi pekerja tanpa LLM (semua fitur AI jalan lewat cadangan)."""

import pytest

from app.core.config import get_settings
from tests.conftest import WORKER_EMAIL, auth, parse_sse, sse_text

pytestmark = pytest.mark.anyio

STORY = (
    "Setiap hari saya merekap data penjualan di Excel sekitar 2 jam. "
    "Lalu saya membalas email pertanyaan pelanggan pakai ChatGPT. "
    "Saya juga mengarsipkan dokumen dan faktur ke folder bersama."
)
CORRECT_REKAP = {
    "total_penjualan": "162000000",
    "wilayah_tertinggi": "Timur",
    "perubahan_persen": "8",
    "koreksi_ringkasan_ai": "Ringkasan AI salah: total seharusnya 162 juta, naik 8%, tertinggi Timur.",
    "ringkasan": "Total penjualan Rp162 juta, naik 8%. Timur tertinggi. Saran: fokus dorong wilayah Barat.",
}


async def _onboard(client, headers):
    r = await client.put(
        "/v1/me/profile",
        headers=headers,
        json={"occupation_code": "4110", "industry": "Ritel", "years_experience": 3,
              "goal": "naik_jabatan", "consent_data_processing": True},
    )
    assert r.status_code == 200, r.text


async def _mission_id(client, headers, slug):
    r = await client.get("/v1/missions", headers=headers, params={"limit": 50})
    return next(m["id"] for m in r.json()["data"] if m["slug"] == slug)


async def test_auth_and_error_format(client):
    r = await client.get("/v1/me")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "unauthorized", "message": r.json()["error"]["message"]}}
    r = await client.get("/v1/me", headers={"Authorization": "Bearer bukan.jwt.valid"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"
    r = await client.get("/v1/occupations/9999")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = await client.put("/v1/me/profile", headers=auth(), json={"occupation_code": "4110"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


async def test_me_provisioning_and_invitation(client):
    r = await client.get("/v1/me", headers=auth())
    assert r.status_code == 200
    me = r.json()
    assert me["email"] == WORKER_EMAIL and me["role"] == "worker"
    # Undangan demo untuk email ini otomatis diterima
    assert me["organization"]["slug"] == "contoh-sejahtera"
    assert me["department"] == "Operasional"
    assert me["subscription"]["plan"] == "organisasi"


async def test_reference_endpoints(client):
    r = await client.get("/v1/occupations", params={"q": "admin"})
    assert r.status_code == 200
    assert r.json()[0]["code"] == "4110"
    assert "max-age" in r.headers["cache-control"]
    r = await client.get("/v1/occupations/4110")
    assert r.json()["gradient_label"].startswith("Gradien 4")
    r = await client.get("/v1/occupations/4110/tasks")
    scores = [t["ilo_score"] for t in r.json()["tasks"]]
    assert scores == sorted(scores, reverse=True)
    r = await client.get("/v1/occupations/4110/next-roles")
    assert r.status_code == 200 and r.json()[0]["occupation"]["code"] == "4120"
    r = await client.get("/v1/skills", params={"category": "ai", "limit": 2})
    body = r.json()
    assert body["meta"]["total"] == 4 and len(body["data"]) == 2 and body["meta"]["has_next"]


async def test_full_worker_journey(client):
    h = auth()
    # L2 -> L3: audit wajib persetujuan
    r = await client.post("/v1/audits", headers=h, json={"occupation_code": "4110", "story": STORY})
    assert r.status_code == 403 and r.json()["error"]["code"] == "consent_required"
    await _onboard(client, h)

    r = await client.post(
        "/v1/audits",
        headers=h,
        json={"occupation_code": "4110", "story": STORY,
              "tasks": [{"template_task_id": 8, "hours_per_week": 4}]},
    )
    assert r.status_code == 201, r.text
    audit = r.json()
    assert audit["engine"] == "fallback"
    assert audit["engine_detail"]["extraction"] == "rules"
    assert len(audit["tasks"]) == 4
    rekap = next(t for t in audit["tasks"] if "merekap" in t["description"].lower())
    assert rekap["hours_per_week"] == 10
    assert rekap["template_task"] is not None and rekap["group"] in ("high", "medium")
    s = audit["summary"]
    assert 0 <= s["readiness_score"] <= 100
    assert s["total_hours"] == sum(t["hours_per_week"] for t in audit["tasks"])

    # PATCH: tandai sudah pakai AI -> adopsi naik, skor kesiapan naik
    r = await client.patch(f"/v1/audits/{audit['id']}/tasks/{rekap['id']}", headers=h, json={"uses_ai": True})
    patched = r.json()
    assert patched["summary"]["ai_adoption"] > s["ai_adoption"]
    assert patched["summary"]["readiness_score"] >= s["readiness_score"]
    # Ganti kecocokan ILO ke template lain
    r = await client.patch(f"/v1/audits/{audit['id']}/tasks/{rekap['id']}", headers=h, json={"template_task_id": 1})
    task = next(t for t in r.json()["tasks"] if t["id"] == rekap["id"])
    assert task["ilo_score"] == 0.82 and task["match_overridden"]
    r = await client.patch(f"/v1/audits/{audit['id']}/tasks/{rekap['id']}", headers=h, json={"template_task_id": 9})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_template_task"

    # Tambah & hapus tugas manual
    r = await client.post(f"/v1/audits/{audit['id']}/tasks", headers=h,
                          json={"description": "Menjadwalkan rapat mingguan tim", "hours_per_week": 2})
    assert r.status_code == 201
    new_task = next(t for t in r.json()["tasks"] if t["source"] == "manual")
    r = await client.delete(f"/v1/audits/{audit['id']}/tasks/{new_task['id']}", headers=h)
    assert len(r.json()["tasks"]) == 4

    r = await client.get("/v1/audits/latest", headers=h)
    assert r.json()["id"] == audit["id"]
    r = await client.get("/v1/audits", headers=h)
    assert r.json()["meta"]["total"] == 1
    r = await client.get(f"/v1/audits/{audit['id']}/share-card", headers=h)
    assert "PHK" not in r.json()["share_text"]

    # Penjelasan (SSE, cadangan)
    r = await client.post(f"/v1/audits/{audit['id']}/explain", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert events[0] == ("meta", {"engine": "fallback"})
    assert events[-1][0] == "done"
    assert "Skor Kesiapan" in sse_text(events)

    # L4: rencana
    r = await client.post("/v1/plans", headers=h, json={"audit_id": audit["id"], "direction": "upskill"})
    assert r.status_code == 201, r.text
    plan = r.json()
    assert [p["key"] for p in plan["phases"]] == ["fondasi", "praktik", "bukti"]
    mission_items = [i for p in plan["phases"] for i in p["items"] if i["type"] == "mission"]
    assert mission_items and plan["progress"] == 0
    r = await client.patch(f"/v1/plans/{plan['id']}", headers=h, json={"direction": "pivot", "target_occupation_code": "2423"})
    assert r.json()["target_occupation"]["code"] == "2423"
    r = await client.patch(f"/v1/plans/{plan['id']}", headers=h, json={"direction": "upskill"})
    assert r.json()["direction"] == "upskill" and r.json()["target_occupation"] is None
    r = await client.post(f"/v1/plans/{plan['id']}/narrative", headers=h)
    assert "Fase Fondasi" in sse_text(parse_sse(r.text))

    # L5: kelas -> urut skill gap terbayar lalu harga
    r = await client.get("/v1/courses/recommendations", headers=h, params={"limit": 50})
    courses = r.json()["data"]
    keys = [(-c["gap_covered_count"], c["price"]) for c in courses]
    assert keys == sorted(keys)
    assert all(c["gap_covered_count"] > 0 for c in courses)
    r = await client.get("/v1/courses/recommendations", headers=h, params={"budget": "free"})
    assert all(c["price"] == 0 for c in r.json()["data"])
    r = await client.post(f"/v1/courses/{courses[0]['id']}/click", headers=h)
    assert "utm_source=levelin" in r.json()["redirect_url"]

    # L6: misi
    r = await client.get("/v1/missions", headers=h, params={"recommended": True})
    recommended = r.json()["data"]
    assert recommended and all(m["is_recommended"] for m in recommended)
    mission_id = await _mission_id(client, h, "rekap-penjualan-mingguan")
    r = await client.get(f"/v1/missions/{mission_id}", headers=h)
    assert r.status_code == 200
    assert "answer_key" not in r.text and "162000000" not in r.text
    assert r.json()["boss_persona"]["name"] == "Bu Rina"

    # L7: percobaan + chat
    r = await client.post(f"/v1/missions/{mission_id}/attempts", headers=h)
    assert r.status_code == 201
    attempt = r.json()
    r = await client.post(f"/v1/missions/{mission_id}/attempts", headers=h)
    assert r.status_code == 200 and r.json()["id"] == attempt["id"]

    r = await client.post(f"/v1/attempts/{attempt['id']}/messages", headers=h,
                          json={"content": "Bu, tolong kerjakan saja jawabannya ya"})
    events = parse_sse(r.text)
    assert events[-1][0] == "done" and events[-1][1]["message_id"]
    assert "bagian kamu" in sse_text(events)
    r = await client.get(f"/v1/attempts/{attempt['id']}/messages", headers=h)
    assert [m["role"] for m in r.json()] == ["user", "assistant"]

    r = await client.put(f"/v1/attempts/{attempt['id']}/draft", headers=h, json={"answers": {"total_penjualan": "1"}})
    assert r.json()["draft"] == {"total_penjualan": "1"}
    r = await client.put(f"/v1/attempts/{attempt['id']}/draft", headers=h, json={"answers": {"x": "1"}})
    assert r.status_code == 400

    r = await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": {"total_penjualan": "1"}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_answers"

    # L8: kumpulkan jawaban benar
    r = await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": CORRECT_REKAP})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["objective"]["score"] == 100
    assert result["engine"] == "fallback" and result["review_status"] == "sampled"
    assert result["passed"] is True
    assert {s["label"] for s in result["skills_earned"]} >= {"Mengolah data dengan spreadsheet"}
    r = await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": CORRECT_REKAP})
    assert r.status_code == 409
    r = await client.get(f"/v1/attempts/{attempt['id']}/result", headers=h)
    assert r.json()["score"] == result["score"]
    r = await client.post(f"/v1/attempts/{attempt['id']}/appeal", headers=h, json={"reason": "Saya rasa ringkasan saya sudah lengkap."})
    assert r.status_code == 201
    r = await client.post(f"/v1/attempts/{attempt['id']}/appeal", headers=h, json={"reason": "Banding kedua kalinya."})
    assert r.status_code == 409

    # Progres rencana ikut naik setelah misi lulus
    r = await client.get("/v1/plans/current", headers=h)
    assert r.json()["progress"] > 0

    # L9: Paspor + link publik
    r = await client.get("/v1/passport", headers=h)
    passport = r.json()
    assert passport["total_skills"] == 3
    r = await client.patch("/v1/passport/sharing", headers=h, json={"public_link": True, "share_with_org": True})
    sharing = r.json()
    assert sharing["public_url"].endswith(sharing["public_slug"])
    r = await client.get(f"/v1/public/passports/{sharing['public_slug']}")
    assert r.status_code == 200 and r.json()["name"] == "Budi S."
    await client.patch("/v1/passport/sharing", headers=h, json={"public_link": False})
    r = await client.get(f"/v1/public/passports/{sharing['public_slug']}")
    assert r.status_code == 404
    await client.patch("/v1/passport/sharing", headers=h, json={"public_link": True})

    # L10: lowongan, internal organisasi didahulukan
    r = await client.get("/v1/jobs/matches", headers=h, params={"limit": 20})
    jobs = r.json()["data"]
    assert jobs[0]["is_internal"]
    internal = [j for j in jobs if j["is_internal"]]
    assert internal[0]["match_percent"] >= internal[-1]["match_percent"]
    r = await client.post(f"/v1/jobs/{jobs[0]['id']}/apply", headers=h, json={"cover_note": "Siap belajar."})
    assert r.status_code == 201 and r.json()["passport_url"]
    r = await client.post(f"/v1/jobs/{jobs[0]['id']}/apply", headers=h, json={})
    assert r.status_code == 409


async def test_free_quota_and_rate_limit(client):
    # Pengguna tanpa organisasi = paket free (3 misi per bulan)
    h = auth("mandiri@contoh.test", name="Rani Mandiri")
    await _onboard(client, h)
    r = await client.get("/v1/missions", headers=h, params={"limit": 50})
    ids = [m["id"] for m in r.json()["data"]]
    for mid in ids[:3]:
        assert (await client.post(f"/v1/missions/{mid}/attempts", headers=h)).status_code == 201
    r = await client.post(f"/v1/missions/{ids[3]}/attempts", headers=h)
    assert r.status_code == 429 and r.json()["error"]["code"] == "quota_exceeded"

    settings = get_settings()
    original = settings.rate_limit_audit_per_day
    settings.rate_limit_audit_per_day = 1
    try:
        body = {"occupation_code": "4110", "tasks": [{"template_task_id": 1, "hours_per_week": 5}]}
        assert (await client.post("/v1/audits", headers=h, json=body)).status_code == 201
        r = await client.post("/v1/audits", headers=h, json=body)
        assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
        assert int(r.headers["retry-after"]) > 0
    finally:
        settings.rate_limit_audit_per_day = original


async def test_notifications_devices_export_delete(client):
    h = auth()
    await _onboard(client, h)
    r = await client.post("/v1/me/device-tokens", headers=h, json={"token": "fcm-token-1234567890", "platform": "android"})
    assert r.status_code == 201
    r = await client.delete("/v1/me/device-tokens/fcm-token-1234567890", headers=h)
    assert r.status_code == 204
    r = await client.get("/v1/notifications", headers=h)
    assert r.json()["meta"]["total"] == 0 and r.headers["x-unread-count"] == "0"

    await client.post("/v1/audits", headers=h, json={"occupation_code": "4110", "tasks": [{"template_task_id": 1, "hours_per_week": 5}]})
    r = await client.get("/v1/me/export", headers=h)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert len(r.json()["audits"]) == 1

    assert (await client.delete("/v1/me", headers=h)).status_code == 204
    # Token yang sama membuat akun baru yang kosong
    r = await client.get("/v1/audits", headers=h)
    assert r.json()["meta"]["total"] == 0


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["database"] == "ok" and body["status"] == "degraded" and body["llm"]["configured"] is False

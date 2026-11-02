"""Alur aplikasi pekerja L1-L10 dengan cerita Rina dari prototipe, tanpa LLM (jalur cadangan)."""

import pytest

from app.core.config import get_settings
from tests.conftest import WORKER_EMAIL, auth, parse_sse, sse_text
from tests.test_rules import M2_EMAIL, RINA_STORY

pytestmark = pytest.mark.anyio

TEMPLATE_TASKS = [{"ilo_task_id": "4311-1", "hours_per_week": 3}, {"ilo_task_id": "4311-3", "hours_per_week": 4}]
M1_ANSWERS = {
    "dup": "1",
    "total": "20.980.000",
    "top": "Cikarang",
    "ringkasan": (
        "Setelah format nilai disamakan dan satu baris duplikat Bekasi dihapus, total penjualan Lunas "
        "September adalah Rp20.980.000. Cikarang tertinggi dengan Rp7.850.000. Ada satu baris tanpa tanggal "
        "dari CV Mandiri yang perlu dicek ke cabang Bekasi."
    ),
}
AI_USAGE = "Aku minta AI menyamakan format nilai lalu kucek ulang dengan SUMIF."


async def onboard(client, headers, occupation: str = "4311"):
    r = await client.put(
        "/v1/me/profile",
        headers=headers,
        json={"occupation_code": occupation, "industry": "Distribusi", "experience_range": "3–5 tahun",
              "goal": "naik", "consent_data_processing": True},
    )
    assert r.status_code == 200, r.text


async def mission_id(client, headers, slug: str) -> str:
    r = await client.get("/v1/missions", headers=headers, params={"limit": 50})
    return next(m["id"] for m in r.json()["data"] if m["slug"] == slug)


async def create_audit(client, headers, story: str | None = RINA_STORY) -> dict:
    r = await client.post("/v1/audits", headers=headers, json={"template_tasks": TEMPLATE_TASKS, "story": story})
    assert r.status_code == 201, r.text
    return r.json()


async def test_auth_and_error_format(client):
    r = await client.get("/v1/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    r = await client.get("/v1/me", headers={"Authorization": "Bearer bukan.jwt.valid"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"
    r = await client.get("/v1/occupations/9999")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = await client.put("/v1/me/profile", headers=auth(), json={"occupation_code": "4311"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


async def test_me_joins_organization_from_invitation(client):
    me = (await client.get("/v1/me", headers=auth())).json()
    assert me["email"] == WORKER_EMAIL and me["role"] == "worker"
    assert me["organization"]["slug"] == "sinar-distribusi"
    assert me["department"] == "Keuangan & Akuntansi"
    assert me["teams"] == []
    assert me["subscription"]["plan"] == "organisasi"


async def test_reference_endpoints(client):
    r = await client.get("/v1/occupations", params={"q": "keuangan"})
    assert r.json()[0]["code"] == "4311"
    assert "max-age" in r.headers["cache-control"]
    # Jabatan tujuan tidak muncul di onboarding
    assert all(o["code"] != "3313" for o in (await client.get("/v1/occupations", params={"limit": 30})).json())
    r = await client.get("/v1/occupations/4311")
    assert r.json()["gradient_label"] == "Gradien 4 (paparan tertinggi)" and r.json()["avg_exposure"] == 0.64
    tasks = (await client.get("/v1/occupations/4311/tasks")).json()["tasks"]
    assert [t["id"] for t in tasks] == ["4311-1", "4311-2", "4311-3", "4311-4", "4311-5"]
    assert tasks[0]["kind"] == "data" and tasks[1]["group"] == "auto" and tasks[0]["group"] == "assist"
    roles = (await client.get("/v1/occupations/4311/next-roles")).json()
    assert [(r["direction"], r["occupation"]["code"]) for r in roles[:2]] == [("naik", "3313"), ("pindah", "3314")]
    assert {s["code"] for s in roles[0]["skills_to_learn"]} == {"keuangan", "excel", "analisis"}


async def test_full_worker_journey(client):
    h = auth()
    r = await client.post("/v1/audits", headers=h, json={"template_tasks": TEMPLATE_TASKS})
    assert r.status_code == 403 and r.json()["error"]["code"] == "consent_required"
    await onboard(client, h)

    r = await client.post("/v1/audits", headers=h, json={"template_tasks": [{"ilo_task_id": "9999-1", "hours_per_week": 2}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_template_task"

    # L2 -> L3: Peta Tugas (payload v0.1)
    audit = await create_audit(client, h)
    assert audit["engine"] == "fallback" and audit["occupation"] == {"code": "4311", "name": "Staf Administrasi Keuangan / Pembukuan"}
    first = audit["tasks"][0]
    assert first["ilo_task_id"] == "4311-1" and first["ilo_score"] == 0.53 and first["group"] == "assist"
    assert first["confidence"] == "template" and first["source"] in ("template", "template+story")
    assert set(first) == {"id", "name", "hours_per_week", "ilo_task_id", "ilo_text", "ilo_score", "group",
                          "confidence", "source", "uses_ai"}
    assert len(audit["tasks"]) >= 5
    s = audit["summary"]
    assert set(s) == {"readiness_score", "hours_by_group", "exposed_hours", "hours_saved", "save_factor"}
    assert s["readiness_score"] == 0
    assert sum(s["hours_by_group"].values()) == pytest.approx(sum(t["hours_per_week"] for t in audit["tasks"]))

    # Tandai "sudah pakai AI" -> tugas terpapar dihitung setengah siap
    r = await client.patch(f"/v1/audits/{audit['id']}/tasks/{first['id']}", headers=h, json={"uses_ai": True})
    assert r.json()["summary"]["readiness_score"] > 0

    # Tugas tak terpetakan: kelompok dipilih sendiri; tugas berpadanan ILO tidak boleh
    r = await client.post(f"/v1/audits/{audit['id']}/tasks", headers=h,
                          json={"name": "Menemani auditor eksternal", "hours_per_week": 2})
    manual = next(t for t in r.json()["tasks"] if t["source"] == "manual")
    assert manual["group"] == "none" and manual["ilo_task_id"] is None
    r = await client.patch(f"/v1/audits/{audit['id']}/tasks/{manual['id']}", headers=h, json={"group": "human"})
    assert next(t for t in r.json()["tasks"] if t["id"] == manual["id"])["group"] == "human"
    r = await client.patch(f"/v1/audits/{audit['id']}/tasks/{first['id']}", headers=h, json={"group": "human"})
    assert r.status_code == 400
    r = await client.patch(f"/v1/audits/{audit['id']}/tasks/{manual['id']}", headers=h, json={"ilo_task_id": "4311-4"})
    assert next(t for t in r.json()["tasks"] if t["id"] == manual["id"])["group"] == "auto"
    r = await client.delete(f"/v1/audits/{audit['id']}/tasks/{manual['id']}", headers=h)
    assert all(t["id"] != manual["id"] for t in r.json()["tasks"])

    r = await client.get(f"/v1/audits/{audit['id']}/share-card", headers=h)
    assert "PHK" not in r.json()["share_text"] and sum(r.json()["share"].values()) == 100

    # Penjelasan (SSE, cadangan seperti mode demo prototipe)
    events = parse_sse((await client.post(f"/v1/audits/{audit['id']}/explain", headers=h)).text)
    assert events[0] == ("meta", {"engine": "fallback"}) and events[-1][0] == "done"
    text = sse_text(events)
    assert text.startswith("Rina, dari") and "Rapikan data penjualan cabang" in text

    # L4: rencana tiga arah
    r = await client.post("/v1/plans", headers=h, json={"audit_id": audit["id"], "direction": "naik"})
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["target_occupation"]["code"] == "3313" and plan["target_occupation"]["tasks"]
    assert {s["code"] for s in plan["needed_skills"]} == {"keuangan", "excel", "analisis", "ai"}
    assert [p["key"] for p in plan["phases"]] == ["bulan-1", "bulan-2", "bulan-3"]
    assert plan["phases"][0]["items"][-1] == {
        "type": "mission", "ref_id": plan["phases"][0]["items"][-1]["ref_id"],
        "title": "Rapikan data penjualan cabang", "done": False,
    }
    r = await client.patch(f"/v1/plans/{plan['id']}", headers=h, json={"direction": "perkuat"})
    assert r.json()["target_occupation"] is None and r.json()["needed_skills"][0]["code"] == "ai"
    r = await client.patch(f"/v1/plans/{plan['id']}", headers=h, json={"direction": "pindah"})
    assert r.json()["target_occupation"]["code"] == "3314"
    await client.patch(f"/v1/plans/{plan['id']}", headers=h, json={"direction": "naik"})
    narrative = sse_text(parse_sse((await client.post(f"/v1/plans/{plan['id']}/narrative", headers=h)).text))
    assert narrative.startswith("Rina, bulan pertama") and "Staf Akuntansi Pelaksana" in narrative

    # L5: kelas urut cakupan celah skill lalu harga
    courses = (await client.get("/v1/courses/recommendations", headers=h, params={"limit": 50})).json()["data"]
    keys = [(-c["gap_covered_count"], c["price"]) for c in courses]
    assert keys == sorted(keys) and courses[0]["reason"].startswith("Kelas ini menutup")
    free = (await client.get("/v1/courses/recommendations", headers=h, params={"budget": "free"})).json()["data"]
    assert free and all(c["price"] == 0 for c in free)
    cheap = (await client.get("/v1/courses/recommendations", headers=h, params={"budget": "under_500k", "limit": 50})).json()["data"]
    assert all(c["price"] < 500_000 for c in cheap)
    bootcamps = (await client.get("/v1/courses/recommendations", headers=h, params={"budget": "bootcamp", "limit": 50})).json()["data"]
    assert bootcamps and all("bootcamp" in c["format"].lower() for c in bootcamps)

    # L6: misi untuk jenis tugas paling terpapar didahulukan
    listed = (await client.get("/v1/missions", headers=h, params={"recommended": True})).json()["data"]
    assert listed[0]["slug"] == "rapikan-data-penjualan" and "Olah & input data" in listed[0]["recommend_reason"]
    m1 = listed[0]["id"]
    detail = await client.get(f"/v1/missions/{m1}", headers=h)
    assert "answer_key" not in detail.text and "20980000" not in detail.text
    body = detail.json()
    assert body["simulation_data"]["table"]["columns"][0] == "No" and len(body["rubric"]) == 4
    assert body["checks"][0] == "Jumlah baris duplikat tepat" and body["boss_persona"]["name"] == "Bu Ratna"

    # L7: percobaan + chat atasan AI
    attempt = (await client.post(f"/v1/missions/{m1}/attempts", headers=h)).json()
    r = await client.post(f"/v1/missions/{m1}/attempts", headers=h)
    assert r.status_code == 200 and r.json()["id"] == attempt["id"]
    events = parse_sse((await client.post(f"/v1/attempts/{attempt['id']}/messages", headers=h,
                                         json={"content": "Bu, tolong kerjakan saja jawabannya ya"})).text)
    assert events[-1][0] == "done" and events[-1][1]["message_id"] and events[-1][1]["flagged"] is False
    assert "bagian kamu" in sse_text(events)
    r = await client.put(f"/v1/attempts/{attempt['id']}/draft", headers=h, json={"answers": {"dup": "1"}})
    assert r.json()["draft"] == {"dup": "1"}
    r = await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": {"dup": "1"}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_answers"

    # L8: kumpulkan (payload v0.1)
    r = await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h,
                          json={"answers": M1_ANSWERS, "ai_usage": AI_USAGE})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["passed"] is True and all(c["ok"] for c in result["checks"])
    assert result["checks"][1] == {"label": "Total penjualan Lunas tepat", "ok": True, "hint": None}
    assert result["rubric"] is None and result["rubric_avg"] is None and result["engine"] == "fallback"
    assert result["passport_evidence_id"] and result["review_status"] == "sampled"
    assert result["readiness_score"] > 0
    assert {s["code"] for s in result["skills_earned"]} == {"excel", "kualitas", "ai"}
    assert (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h,
                              json={"answers": M1_ANSWERS})).status_code == 409
    assert (await client.get(f"/v1/attempts/{attempt['id']}/result", headers=h)).json()["passed"] is True
    assert (await client.post(f"/v1/attempts/{attempt['id']}/appeal", headers=h,
                              json={"reason": "Mohon ditinjau kualitas ringkasanku."})).status_code == 201

    # Tugas berjenis "data" sekarang siap penuh -> Skor Kesiapan naik
    latest = (await client.get("/v1/audits/latest", headers=h)).json()
    assert latest["summary"]["readiness_score"] == result["readiness_score"]
    assert latest["summary"]["readiness_score"] > s["readiness_score"]
    assert (await client.get("/v1/plans/current", headers=h)).json()["progress"] > 0

    # L9: Paspor
    passport = (await client.get("/v1/passport", headers=h)).json()
    assert passport["total_skills"] == 3
    evidence = passport["skills"][0]["evidence"][0]
    assert evidence["checks_passed"] == 3 and evidence["checks_total"] == 3
    assert evidence["excerpt"].startswith("Setelah format nilai")
    sharing = (await client.patch("/v1/passport/sharing", headers=h,
                                  json={"public_link": True, "share_with_org": True})).json()
    public = await client.get(f"/v1/public/passports/{sharing['public_slug']}")
    assert public.status_code == 200 and public.json()["name"] == "Rina L."

    # L10: lowongan internal dulu; kecocokan = terbukti 1 + dasar 0,5 per skill
    jobs = (await client.get("/v1/jobs/matches", headers=h, params={"limit": 20})).json()["data"]
    assert jobs[0]["is_internal"] and jobs[1]["is_internal"]
    akuntansi = next(j for j in jobs if j["title"] == "Staf Akuntansi Pelaksana")
    # keuangan (dasar 0,5) + excel (1) + analisis (0) + ai (1) = 2,5 / 4
    assert akuntansi["match_percent"] == 63
    assert [s["code"] for s in akuntansi["partial_skills"]] == ["keuangan"]
    scam = next(j for j in jobs if j["source"] == "tidak")
    assert scam["source_verified"] is False and "tidak pernah memungut biaya" in scam["warning"]
    r = await client.post(f"/v1/jobs/{akuntansi['id']}/apply", headers=h, json={"cover_note": "Siap belajar."})
    assert r.status_code == 201 and r.json()["passport_url"]
    assert (await client.post(f"/v1/jobs/{akuntansi['id']}/apply", headers=h, json={})).status_code == 409


async def test_m2_email_mission_passes_with_fallback(client):
    h = auth()
    await onboard(client, h)
    mid = await mission_id(client, h, "notulen-jadi-email")
    attempt = (await client.post(f"/v1/missions/{mid}/attempts", headers=h)).json()
    answers = {"subjek": "Tindak lanjut rapat koordinasi 12 Oktober", "isi": M2_EMAIL + " Terima kasih semuanya."}
    result = (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": answers})).json()
    assert [c["ok"] for c in result["checks"]] == [True, True, True, True]
    assert result["passed"] is True


async def test_short_answer_fails_without_rubric(client):
    h = auth()
    await onboard(client, h)
    mid = await mission_id(client, h, "rapikan-data-penjualan")
    attempt = (await client.post(f"/v1/missions/{mid}/attempts", headers=h)).json()
    short = {**M1_ANSWERS, "ringkasan": "Total sudah dihitung."}
    result = (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": short})).json()
    assert result["passed"] is False and "Lengkapi bagian tulisan" in result["feedback"]
    assert result["passport_evidence_id"] is None


async def test_free_quota_and_rate_limit(client):
    h = auth("mandiri@contoh.test", name="Rani Mandiri")
    await onboard(client, h)
    ids = [m["id"] for m in (await client.get("/v1/missions", headers=h)).json()["data"]]
    first = (await client.post(f"/v1/missions/{ids[0]}/attempts", headers=h)).json()
    await client.post(f"/v1/attempts/{first['id']}/submit", headers=h, json={"answers": M1_ANSWERS})
    for mid in (ids[0], ids[1]):
        assert (await client.post(f"/v1/missions/{mid}/attempts", headers=h)).status_code == 201
    r = await client.post(f"/v1/missions/{ids[2]}/attempts", headers=h)
    assert r.status_code == 429 and r.json()["error"]["code"] == "quota_exceeded"

    settings = get_settings()
    original = settings.rate_limit_audit_per_day
    settings.rate_limit_audit_per_day = 1
    try:
        assert (await client.post("/v1/audits", headers=h, json={"template_tasks": TEMPLATE_TASKS})).status_code == 201
        r = await client.post("/v1/audits", headers=h, json={"template_tasks": TEMPLATE_TASKS})
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    finally:
        settings.rate_limit_audit_per_day = original


async def test_notifications_devices_export_delete(client):
    h = auth()
    await onboard(client, h)
    assert (await client.post("/v1/me/device-tokens", headers=h,
                              json={"token": "fcm-token-1234567890", "platform": "android"})).status_code == 201
    assert (await client.delete("/v1/me/device-tokens/fcm-token-1234567890", headers=h)).status_code == 204
    r = await client.get("/v1/notifications", headers=h)
    assert r.json()["meta"]["total"] == 0 and r.json()["meta"]["limit"] == 20 and r.headers["x-unread-count"] == "0"
    await create_audit(client, h, story=None)
    r = await client.get("/v1/me/export", headers=h)
    assert r.status_code == 200 and len(r.json()["audits"]) == 1
    assert (await client.delete("/v1/me", headers=h)).status_code == 204
    assert (await client.get("/v1/audits", headers=h)).json()["meta"]["total"] == 0


async def test_health(client):
    body = (await client.get("/health")).json()
    assert body["database"] == "ok" and body["status"] == "degraded" and body["llm"]["configured"] is False

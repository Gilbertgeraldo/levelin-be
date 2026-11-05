"""Dashboard HR dan atasan tim: hanya agregat (k-anonim), kandidat hanya yang berbagi Paspor."""

import uuid

import pytest
from sqlalchemy import select

from app.models import Occupation, Organization, PassportSettings, Profile, User
from app.services.audits import create_audit
from app.services.embeddings import EmbeddingService
from app.services.llm import LLMCallInfo
from scripts.seed_demo import seed_sample_workers
from tests.conftest import HR_EMAIL, MANAGER_EMAIL, auth
from tests.test_worker_flow import onboard

pytestmark = pytest.mark.anyio

HR = {"email": HR_EMAIL, "role": None, "name": "Hana Pratiwi"}
MANAGER = {"email": MANAGER_EMAIL, "role": None, "name": "Ratna Wijaya"}
REVIEWER = {"email": "reviewer@levelin.test", "role": "reviewer", "name": "Rudi Reviewer"}
PDF = b"%PDF-1.4\n%demo\n"


async def _org(sessionmaker) -> Organization:
    async with sessionmaker() as db:
        return await db.scalar(select(Organization).where(Organization.slug == "sinar-distribusi"))


async def _make_workers(sessionmaker, sizes: dict[str, int]) -> dict[str, list[uuid.UUID]]:
    """Karyawan berisi audit (semua audit) per departemen, untuk skenario k-anonim yang terkendali."""
    org = await _org(sessionmaker)
    out: dict[str, list[uuid.UUID]] = {}
    async with sessionmaker() as db:
        occupation = await db.get(Occupation, "4311")
        for dept, n in sizes.items():
            for i in range(n):
                user = User(id=uuid.uuid4(), email=f"{dept.lower()}{i}@uji.test", full_name=f"{dept} {i}",
                            role="worker", org_id=org.id, department=dept)
                db.add(user)
                await db.flush()
                db.add(Profile(user_id=user.id, occupation_code="4311", consent_data_processing=True))
                await create_audit(db, user, occupation=occupation, story=None,
                                   template_tasks=[{"ilo_task_id": "4311-2", "hours_per_week": 2 + i}],
                                   llm=None, embeddings=EmbeddingService(), info=LLMCallInfo())
                out.setdefault(dept, []).append(user.id)
        await db.commit()
    return out


async def test_hr_dashboard_matches_v01_and_hides_small_groups(client, sessionmaker):
    h = auth(**HR)
    me = (await client.get("/v1/me", headers=h)).json()
    assert me["role"] == "hr_admin" and me["organization"]["slug"] == "sinar-distribusi"

    empty = (await client.get("/v1/org/dashboard", headers=h)).json()
    assert empty["suppressed"] is True and empty["readiness_avg"] is None and empty["trend"] == []

    async with sessionmaker() as db:
        await seed_sample_workers(db, await _org(sessionmaker))
        await db.commit()
    r = await client.get("/v1/org/dashboard", headers=h)
    body = r.json()
    assert set(body) >= {"org", "readiness_avg", "hours_saved_per_week", "missions_passed_this_month",
                         "internal_candidates", "departments", "trend", "suppressed_departments", "insight"}
    assert body["org"]["name"] == "PT Sinar Distribusi" and body["org"]["invited"] == 50  # 49 anggota + 1 undangan tertunda
    assert body["headcount"] == 49 and 5 <= body["org"]["audited"] < body["headcount"]
    assert isinstance(body["readiness_avg"], int) and body["hours_saved_per_week"] > 0
    assert len(body["trend"]) == 3 and all(p["readiness_avg"] is not None for p in body["trend"])
    for dept in body["departments"]:
        assert sum(dept["share"].values()) == 100 and dept["headcount"] >= 5
    assert "Gudang" not in {d["name"] for d in body["departments"]}  # 4 orang: disembunyikan
    assert body["suppressed_departments"] >= 1 and "paling siap" in body["insight"]
    assert "tasks" not in r.text and "story" not in r.text

    report = await client.get("/v1/org/reports/progress", headers=h, params={"format": "csv"})
    assert report.headers["content-type"].startswith("text/csv")
    assert report.text.splitlines()[0] == "month,active_people,audited_people,avg_readiness,missions_passed,suppressed"


async def test_dashboard_blocks_inference_by_subtraction(client, sessionmaker):
    h = auth(**HR)
    await client.get("/v1/me", headers=h)
    await _make_workers(sessionmaker, {"Besar": 5, "Solo": 1})
    body = (await client.get("/v1/org/dashboard", headers=h)).json()
    # Total organisasi tampil, tapi "Besar" ikut disembunyikan supaya skor "Solo" tidak bisa dihitung balik
    assert body["readiness_avg"] is not None and body["departments"] == [] and body["suppressed_departments"] == 2

    await _make_workers(sessionmaker, {"Kecil": 4})
    body = (await client.get("/v1/org/dashboard", headers=h)).json()
    assert [d["name"] for d in body["departments"]] == ["Besar"] and body["suppressed_departments"] == 2


async def test_candidates_members_and_invitations(client, sessionmaker):
    h = auth(**HR)
    await client.get("/v1/me", headers=h)
    worker = auth()
    await onboard(client, worker)
    assert (await client.get("/v1/org/candidates", headers=h)).json()["meta"]["total"] == 0
    await client.patch("/v1/passport/sharing", headers=worker, json={"share_with_org": True})
    candidates = (await client.get("/v1/org/candidates", headers=h)).json()["data"]
    assert [c["name"] for c in candidates] == ["Rina Lestari"]
    assert candidates[0]["initials"] == "RL" and candidates[0]["target"] == "Perkuat posisi sekarang"
    assert "readiness" not in str(candidates)

    members = (await client.get("/v1/org/members", headers=h)).json()["data"]
    rina = next(m for m in members if m["email"] == "rina@sinar-distribusi.test")
    assert set(rina) == {"user_id", "name", "email", "department", "role", "manager_names", "last_active_at", "is_online"}
    assert rina["is_online"] is True
    await client.post("/v1/me/presence", headers=worker, json={"online": False})
    rina_off = next(m for m in (await client.get("/v1/org/members", headers=h)).json()["data"] if m["user_id"] == rina["user_id"])
    assert rina_off["is_online"] is False
    await client.post("/v1/me/presence", headers=worker, json={"online": True})
    rina_on = next(m for m in (await client.get("/v1/org/members", headers=h)).json()["data"] if m["user_id"] == rina["user_id"])
    assert rina_on["is_online"] is True
    r = await client.patch(f"/v1/org/members/{rina['user_id']}", headers=h, json={"role": "manager"})
    assert r.json()["role"] == "manager"
    me = next(m for m in members if m["email"] == HR_EMAIL)
    assert (await client.patch(f"/v1/org/members/{me['user_id']}", headers=h, json={"role": "worker"})).status_code == 400

    r = await client.post("/v1/org/invitations", headers=h,
                          json={"emails": ["a@uji.test", "a@uji.test", "salah"], "department": "Gudang"})
    assert [c["email"] for c in r.json()["created"]] == ["a@uji.test"]
    assert {s["reason"] for s in r.json()["skipped"]} == {"duplicate", "invalid_email"}
    csv_bytes = b"email,name,department,role\nb@uji.test,Bima,Gudang,manager\na@uji.test,Ani,Gudang,worker\n"
    r = await client.post("/v1/org/invitations", headers=h, files={"file": ("karyawan.csv", csv_bytes, "text/csv")})
    assert r.json()["created"][0]["role"] == "manager"
    assert r.json()["skipped"] == [{"email": "a@uji.test", "reason": "already_invited"}]
    me_b = (await client.get("/v1/me", headers=auth("b@uji.test", name="Bima"))).json()
    assert me_b["role"] == "manager" and me_b["department"] == "Gudang"

    r = await client.post("/v1/org/custom-missions", headers=h, data={"title": "Misi SOP Gudang"},
                          files={"file": ("sop.pdf", PDF, "application/pdf")})
    assert r.status_code == 201 and r.json()["status"] == "requested"
    r = await client.post("/v1/org/custom-missions", headers=h, data={"title": "Palsu"},
                          files={"file": ("virus.pdf", b"MZ bukan pdf", "application/pdf")})
    assert r.status_code == 415
    r = await client.post("/v1/org/jobs", headers=h, json={"title": "Analis Data Internal", "skill_ids": [1]})
    assert r.status_code == 201 and r.json()["is_internal"] and r.json()["source"] == "internal"
    job_id = r.json()["id"]
    await client.post(f"/v1/jobs/{job_id}/apply", headers=worker, json={"cover_note": "Siap bergabung"})
    org_jobs = (await client.get("/v1/org/jobs", headers=h)).json()["data"]
    assert next(j for j in org_jobs if j["id"] == job_id)["applicant_count"] == 1
    apps = (await client.get(f"/v1/org/jobs/{job_id}/applications", headers=h)).json()
    assert len(apps) == 1 and apps[0]["name"] == "Rina Lestari" and apps[0]["cover_note"] == "Siap bergabung"

    pending_org = (await client.get("/v1/org/invitations", headers=h, params={"status": "pending"})).json()["data"]
    inv_a = next(i for i in pending_org if i["email"] == "a@uji.test")
    assert (await client.delete(f"/v1/org/invitations/{inv_a['id']}", headers=h)).status_code == 204


async def test_phase4_reviewer_mission_studio_ats_and_mobile_bridge(client):
    h = auth(**HR)
    worker = auth()
    reviewer = auth(**REVIEWER)
    await client.get("/v1/me", headers=h)
    await onboard(client, worker)
    await client.patch("/v1/passport/sharing", headers=worker, json={"share_with_org": True, "public_link": True})

    # 1. HR memesan misi kustom SOP -> Reviewer melihat di /v1/admin/custom-missions -> Menerbitkan misi
    cm_res = await client.post(
        "/v1/org/custom-missions",
        headers=h,
        data={"title": "SOP Rekonsiliasi Kas Harian", "description": "Verifikasi selisih kas akhir hari", "target_role": "Staf Keuangan"},
        files={"file": ("sop-kas.pdf", PDF, "application/pdf")},
    )
    assert cm_res.status_code == 201
    cm_id = cm_res.json()["id"]

    admin_cms = (await client.get("/v1/admin/custom-missions", headers=reviewer)).json()["data"]
    found_cm = next(c for c in admin_cms if c["id"] == cm_id)
    assert found_cm["org_name"] == "PT Sinar Distribusi" and found_cm["status"] == "requested"

    pub_res = await client.post(
        "/v1/admin/missions",
        headers=reviewer,
        json={
            "slug": "sop-rekonsiliasi-kas-harian",
            "title": "Simulasi Rekonsiliasi Kas Harian",
            "summary": "Latihan verifikasi kas sesuai SOP PT Sinar Distribusi",
            "brief": "Periksa laporan kas harian dan temukan penyebab selisih Rp 250.000.",
            "answer_fields": [{"id": "temuan", "label": "Temuan & Rekomendasi", "type": "long_text", "rows": 5}],
            "rubric": [{"id": "r1", "criterion": "Ketepatan Analisis Selisih"}],
            "answer_key": [],
            "skill_ids": [1],
            "custom_request_id": cm_id,
            "is_published": True,
        },
    )
    assert pub_res.status_code == 201
    mission_id = pub_res.json()["id"]

    admin_missions = (await client.get("/v1/admin/missions", headers=reviewer)).json()["data"]
    assert any(m["id"] == mission_id and m["slug"] == "sop-rekonsiliasi-kas-harian" for m in admin_missions)

    hr_cms = (await client.get("/v1/org/custom-missions", headers=h)).json()["data"]
    assert next(c for c in hr_cms if c["id"] == cm_id)["status"] == "published"

    hr_notes = (await client.get("/v1/notifications", headers=h)).json()["data"]
    assert any(n["type"] == "custom_mission" and n["data"].get("mission_id") == mission_id for n in hr_notes)

    # 2. ATS Lite HR & Riwayat Lamaran Pekerja (/v1/me/applications)
    job_res = await client.post("/v1/org/jobs", headers=h, json={"title": "Supervisor Keuangan", "skill_ids": [1]})
    job_id = job_res.json()["id"]
    app_res = await client.post(f"/v1/jobs/{job_id}/apply", headers=worker, json={"cover_note": "Berpengalaman 3 tahun"})
    assert app_res.status_code == 201
    app_id = app_res.json()["id"]

    my_apps = (await client.get("/v1/me/applications", headers=worker)).json()
    assert len(my_apps) == 1 and my_apps[0]["job_title"] == "Supervisor Keuangan" and my_apps[0]["status"] == "submitted"

    patch_res = await client.patch(
        f"/v1/org/jobs/{job_id}/applications/{app_id}",
        headers=h,
        json={"status": "interview"},
    )
    assert patch_res.status_code == 200 and patch_res.json()["status"] == "interview"

    my_apps_after = (await client.get("/v1/me/applications", headers=worker)).json()
    assert my_apps_after[0]["status"] == "interview"

    worker_notes = (await client.get("/v1/notifications", headers=worker)).json()["data"]
    assert any(n["type"] == "application_status" and n["data"].get("status") == "interview" for n in worker_notes)

    # 3. Manajemen Perangkat Mobile (/v1/me/device-tokens) & Read-All Notifications (/v1/notifications/read-all)
    tok = "fcm_token_android_levelin_device_001"
    reg = await client.post("/v1/me/device-tokens", headers=worker, json={"token": tok, "platform": "android"})
    assert reg.status_code == 201
    devices = (await client.get("/v1/me/device-tokens", headers=worker)).json()
    assert len(devices) == 1 and devices[0]["platform"] == "android" and devices[0]["token"] == tok

    read_all = (await client.post("/v1/notifications/read-all", headers=worker)).json()
    assert read_all["updated"] >= 1
    unread_after = (await client.get("/v1/notifications", headers=worker, params={"unread_only": True})).json()["data"]
    assert unread_after == []


async def test_manager_invites_team_and_sees_only_aggregates(client, sessionmaker):
    m = auth(**MANAGER)
    me = (await client.get("/v1/me", headers=m)).json()
    assert me["role"] == "manager"
    assert (await client.get("/v1/org/dashboard", headers=m)).status_code == 403
    assert (await client.get("/v1/team/dashboard", headers=auth())).status_code == 403

    r = await client.post("/v1/team/invitations", headers=m, json={"emails": ["rina@sinar-distribusi.test", "baru@uji.test"]})
    assert {c["email"] for c in r.json()["created"]} == {"rina@sinar-distribusi.test", "baru@uji.test"}
    assert all(c["role"] == "worker" for c in r.json()["created"])

    worker = auth()
    me_w = (await client.get("/v1/me", headers=worker)).json()
    assert me_w["organization"]["slug"] == "sinar-distribusi"
    assert [t["manager_name"] for t in me_w["teams"]] == ["Ratna Wijaya"]
    r = await client.post("/v1/team/invitations", headers=m, json={"emails": ["rina@sinar-distribusi.test"]})
    assert r.json()["skipped"] == [{"email": "rina@sinar-distribusi.test", "reason": "already_in_team"}]
    assert [x["email"] for x in (await client.get("/v1/team/members", headers=m)).json()] == ["rina@sinar-distribusi.test"]

    small = (await client.get("/v1/team/dashboard", headers=m)).json()
    assert small["suppressed"] is True and small["readiness_avg"] is None and small["departments"] == []

    async with sessionmaker() as db:
        await seed_sample_workers(db, await _org(sessionmaker))  # 7 karyawan Keuangan masuk tim Ratna
        await db.commit()
    team = (await client.get("/v1/team/dashboard", headers=m)).json()
    assert team["org"]["name"] == "Tim Ratna Wijaya" and team["headcount"] == 8 and team["departments"] == []

    await onboard(client, worker)
    await client.patch("/v1/passport/sharing", headers=worker, json={"share_with_org": True})
    shared = (await client.get("/v1/team/candidates", headers=m)).json()["data"]
    assert "Rina Lestari" in [c["name"] for c in shared]
    async with sessionmaker() as db:
        sharing_ids = set((await db.scalars(select(PassportSettings.user_id).where(PassportSettings.share_with_org.is_(True)))).all())
    assert all(c["user_id"] in {str(i) for i in sharing_ids} for c in shared)

    pending = (await client.get("/v1/team/invitations", headers=m, params={"status": "pending"})).json()["data"]
    assert [i["email"] for i in pending] == ["baru@uji.test"]
    assert (await client.delete(f"/v1/team/invitations/{pending[0]['id']}", headers=m)).status_code == 204

    manager_id = me_w["teams"][0]["manager_id"]
    assert (await client.delete(f"/v1/me/teams/{manager_id}", headers=worker)).status_code == 204
    assert (await client.get("/v1/me", headers=worker)).json()["teams"] == []
    assert (await client.delete(f"/v1/team/members/{me_w['id']}", headers=m)).status_code == 404


async def test_demoted_manager_loses_team(client):
    h, m = auth(**HR), auth(**MANAGER)
    await client.get("/v1/me", headers=h)
    me_m = (await client.get("/v1/me", headers=m)).json()
    await client.post("/v1/team/invitations", headers=m, json={"emails": ["rina@sinar-distribusi.test"]})
    await client.get("/v1/me", headers=auth())
    assert len((await client.get("/v1/team/members", headers=m)).json()) == 1
    await client.patch(f"/v1/org/members/{me_m['id']}", headers=h, json={"role": "worker"})
    assert (await client.get("/v1/team/members", headers=m)).status_code == 403
    assert (await client.get("/v1/me", headers=auth())).json()["teams"] == []

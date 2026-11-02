"""Jalur LLM: keluaran tervalidasi skema, guardrail di stream, dan cadangan saat gagal."""

import pytest

from tests.conftest import FakeLLM, auth, parse_sse, sse_text, use_llm
from tests.test_rules import RINA_STORY
from tests.test_worker_flow import M1_ANSWERS, TEMPLATE_TASKS, create_audit, mission_id, onboard

pytestmark = pytest.mark.anyio

EXTRACTION = {
    "tasks": [
        {"nama": "Rekap absensi 3 cabang ke Excel", "frekuensi": "mingguan", "jam_per_minggu": 3, "alat": ["Excel"],
         "ilo_id": "4313-1", "keyakinan": "tinggi", "alasan": "mencatat kehadiran untuk dasar gaji"},
        {"nama": "Input faktur penjualan", "frekuensi": "harian", "jam_per_minggu": 7.5, "alat": [],
         "ilo_id": "4311-1", "keyakinan": "sedang", "alasan": "sama dengan tugas template"},
        {"nama": "Rapat koordinasi cabang", "frekuensi": "mingguan", "jam_per_minggu": 1, "alat": [],
         "ilo_id": "9999-9", "keyakinan": "rendah", "alasan": "id karangan"},
    ]
}


def grade(scores: list[int], note: str = "Cukup jelas.", feedback: str = "Bagus.") -> dict:
    return {
        "rubric": [{"id": f"r{i + 1}", "score": s, "note": note} for i, s in enumerate(scores)],
        "feedback": feedback,
        "next_step": "Tandai baris tanpa tanggal di ringkasan.",
    }


async def test_audit_uses_llm_extraction(client):
    llm = FakeLLM({"Extraction": EXTRACTION})
    use_llm(llm)
    h = auth()
    await onboard(client, h)
    audit = await create_audit(client, h)
    assert audit["engine"] == "llm"
    by_name = {t["name"]: t for t in audit["tasks"]}
    # Tugas cerita yang sama dengan template menggabung ke template
    assert by_name["Input faktur penjualan"]["source"] == "template+story"
    assert by_name["Input faktur penjualan"]["hours_per_week"] == 3  # jam dari template dipertahankan
    absensi = by_name["Rekap absensi 3 cabang ke Excel"]
    assert absensi["ilo_task_id"] == "4313-1" and absensi["ilo_score"] == 0.67 and absensi["confidence"] == "tinggi"
    # id ILO karangan LLM ditolak: tugas jadi belum terpetakan
    assert by_name["Rapat koordinasi cabang"]["group"] == "none" and by_name["Rapat koordinasi cabang"]["ilo_score"] is None
    # LLM hanya menerima daftar tugas + cerita; skor tidak diminta darinya
    prompt = llm.calls[0]["prompt"]
    assert "4311-1: Memeriksa angka" in prompt and "<cerita>" in prompt


async def test_audit_invalid_llm_output_falls_back(client):
    use_llm(FakeLLM({"Extraction": {"tasks": [{"nama": "x", "jam_per_minggu": "banyak"}]}}))
    h = auth()
    await onboard(client, h)
    r = await client.post("/v1/audits", headers=h, json={"story": RINA_STORY})
    assert r.status_code == 201 and r.json()["engine"] == "fallback"


async def test_explain_stream_filters_layoff_talk(client):
    h = auth()
    await onboard(client, h)
    audit = await create_audit(client, h)
    llm = FakeLLM(chunks=["Rina, skormu 0. ", "Tanpa belajar AI kamu bisa ", "di-PHK tahun depan. ", "Mulai dari misi pertama."])
    use_llm(llm)
    events = parse_sse((await client.post(f"/v1/audits/{audit['id']}/explain", headers=h)).text)
    text = sse_text(events)
    assert events[0] == ("meta", {"engine": "llm"}) and events[-1] == ("done", {"engine": "llm"})
    assert "PHK" not in text and "Fokus kita" in text and "Mulai dari misi pertama." in text
    facts = llm.calls[0]["messages"][0]["content"]
    # Fakta berisi angka dan nama tugas, bukan cerita utuh pengguna
    assert "Nama: Rina" in facts and "Jam/minggu bisa dihemat" in facts and RINA_STORY not in facts


async def test_stream_failure_mid_way_resets_to_fallback(client):
    h = auth()
    await onboard(client, h)
    audit = await create_audit(client, h, story=None)
    use_llm(FakeLLM(chunks=["Kalimat pertama. ", "Kalimat kedua. ", "tidak sampai"], fail_after=2))
    events = parse_sse((await client.post(f"/v1/audits/{audit['id']}/explain", headers=h)).text)
    names = [e for e, _ in events]
    assert names[:2] == ["meta", "delta"] and "reset" in names
    assert events[-1] == ("done", {"engine": "fallback"})


async def test_boss_chat_never_leaks_answer(client):
    h = auth()
    await onboard(client, h)
    attempt = (await client.post(f"/v1/missions/{await mission_id(client, h, 'rapikan-data-penjualan')}/attempts", headers=h)).json()
    llm = FakeLLM(chunks=["Baik. Totalnya Rp20.980.000 ", "ya. Lalu sekitar 20,98 juta. ", "Cek lagi tanggalnya."])
    use_llm(llm)
    r = await client.post(f"/v1/attempts/{attempt['id']}/messages", headers=h, json={"content": "Berapa totalnya bu?"})
    events = parse_sse(r.text)
    text = sse_text(events)
    assert "20.980" not in text and "20,98" not in text and "Cek lagi tanggalnya." in text
    assert events[-1][1]["flagged"] is True
    stored = (await client.get(f"/v1/attempts/{attempt['id']}/messages", headers=h)).json()
    assert stored[-1]["flagged"] is True
    system = llm.calls[0]["system"]
    assert "Bu Ratna" in system and "20980000" not in system and "answer_key" not in system


async def test_rubric_pass_rule_and_validation(client):
    h = auth()
    await onboard(client, h)
    mid = await mission_id(client, h, "rapikan-data-penjualan")

    use_llm(FakeLLM({"RubricGrade": grade([4, 3, 3, 3])}))
    attempt = (await client.post(f"/v1/missions/{mid}/attempts", headers=h)).json()
    result = (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": M1_ANSWERS})).json()
    assert result["engine"] == "llm" and result["passed"] is True and result["rubric_avg"] == 3.25
    assert result["rubric"][0] == {"criterion": "Ketepatan angka", "score": 4, "note": "Cukup jelas."}
    assert result["next_step"] == "Tandai baris tanpa tanggal di ringkasan." and result["review_status"] == "none"

    # Rata-rata rubrik di bawah 3: tidak lulus walau semua cek benar
    use_llm(FakeLLM({"RubricGrade": grade([3, 2, 3, 3])}))
    attempt = (await client.post(f"/v1/missions/{mid}/attempts", headers=h)).json()
    result = (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": M1_ANSWERS})).json()
    assert result["passed"] is False and result["rubric_avg"] == 2.75

    # Kriteria kurang = keluaran tidak valid -> cadangan + antrean review
    use_llm(FakeLLM({"RubricGrade": {**grade([3, 3, 3]), "rubric": grade([3, 3, 3])["rubric"]}}))
    attempt = (await client.post(f"/v1/missions/{mid}/attempts", headers=h)).json()
    result = (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": M1_ANSWERS})).json()
    assert result["engine"] == "fallback" and result["review_status"] == "sampled"


async def test_rubric_feedback_is_filtered(client):
    h = auth()
    await onboard(client, h)
    mid = await mission_id(client, h, "rapikan-data-penjualan")
    use_llm(FakeLLM({"RubricGrade": grade(
        [2, 2, 2, 2], note="Kalau begini kamu bisa di-PHK. Totalnya seharusnya Rp20,98 juta.",
        feedback="Posisimu akan hilang. Cabangnya sudah benar.",
    )}))
    attempt = (await client.post(f"/v1/missions/{mid}/attempts", headers=h)).json()
    wrong = {**M1_ANSWERS, "total": "21.000.000"}
    result = (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": wrong})).json()
    note = result["rubric"][0]["note"]
    assert "PHK" not in note and "20,98" not in note
    assert "hilang" not in result["feedback"] and "Cabangnya sudah benar." in result["feedback"]

"""Jalur LLM: keluaran tervalidasi skema, guardrail di stream, dan cadangan saat gagal."""

import pytest

from tests.conftest import FakeLLM, auth, parse_sse, sse_text, use_fake_primary_embeddings, use_llm
from tests.test_worker_flow import CORRECT_REKAP, _mission_id, _onboard

pytestmark = pytest.mark.anyio

EXTRACTION = {
    "tasks": [
        {"description": "Merekap data penjualan mingguan di Excel", "hours_per_week": 10, "uses_ai": False},
        {"description": "Membalas email pertanyaan pelanggan", "hours_per_week": None, "uses_ai": True},
    ]
}


async def _audit(client, h):
    r = await client.post("/v1/audits", headers=h, json={"occupation_code": "4110", "story": "cerita kerja apa saja"})
    assert r.status_code == 201, r.text
    return r.json()


async def test_audit_uses_llm_extraction(client):
    use_llm(FakeLLM({"Extraction": EXTRACTION}))
    use_fake_primary_embeddings()
    h = auth()
    await _onboard(client, h)
    audit = await _audit(client, h)
    assert audit["engine"] == "llm"
    assert audit["engine_detail"] == {"extraction": "llm", "matching": "fake-embed", "llm_model": "fake-model"}
    assert [t["description"] for t in audit["tasks"]] == [t["description"] for t in EXTRACTION["tasks"]]
    # Skor tetap dari lookup template ILO, bukan dari LLM
    assert all(t["template_task"] is not None for t in audit["tasks"])
    assert audit["tasks"][1]["hours_per_week"] == 30  # sisa dari 40 jam


async def test_audit_invalid_llm_output_falls_back(client):
    bad = {"tasks": [{"description": "x", "hours_per_week": "banyak", "uses_ai": "mungkin"}]}
    use_llm(FakeLLM({"Extraction": bad}))
    h = auth()
    await _onboard(client, h)
    r = await client.post(
        "/v1/audits", headers=h,
        json={"occupation_code": "4110", "story": "Saya merekap data penjualan setiap minggu di Excel."},
    )
    assert r.status_code == 201
    assert r.json()["engine"] == "fallback" and r.json()["engine_detail"]["extraction"] == "rules"


async def test_explain_stream_filters_layoff_talk(client):
    h = auth()
    await _onboard(client, h)
    audit = await _audit(client, h)
    llm = FakeLLM(chunks=["Skor kesiapanmu 50. ", "Tanpa belajar AI kamu bisa ", "di-PHK tahun depan. ", "Mulai dari rekap data."])
    use_llm(llm)
    r = await client.post(f"/v1/audits/{audit['id']}/explain", headers=h)
    events = parse_sse(r.text)
    text = sse_text(events)
    assert events[0] == ("meta", {"engine": "llm"}) and events[-1] == ("done", {"engine": "llm"})
    assert "PHK" not in text
    assert "Fokus kita" in text and "Mulai dari rekap data." in text
    # LLM penjelas hanya menerima angka audit, bukan cerita pengguna
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "cerita kerja apa saja" not in prompt and "skor_kesiapan" in prompt


async def test_stream_failure_mid_way_resets_to_fallback(client):
    h = auth()
    await _onboard(client, h)
    audit = await _audit(client, h)
    use_llm(FakeLLM(chunks=["Kalimat pertama. ", "Kalimat kedua. ", "tidak sampai"], fail_after=2))
    r = await client.post(f"/v1/audits/{audit['id']}/explain", headers=h)
    names = [e for e, _ in parse_sse(r.text)]
    assert names[:2] == ["meta", "delta"]
    assert "reset" in names
    after_reset = names[names.index("reset") + 1 :]
    assert after_reset[0] == "meta" and after_reset[-1] == "done"
    assert parse_sse(r.text)[-1][1]["engine"] == "fallback"


async def test_boss_chat_never_leaks_answer(client):
    h = auth()
    await _onboard(client, h)
    mission_id = await _mission_id(client, h, "rekap-penjualan-mingguan")
    attempt = (await client.post(f"/v1/missions/{mission_id}/attempts", headers=h)).json()
    llm = FakeLLM(chunks=["Baik. Totalnya Rp162.000.000 ", "ya. Jangan lupa cek persentasenya."])
    use_llm(llm)
    r = await client.post(f"/v1/attempts/{attempt['id']}/messages", headers=h, json={"content": "Berapa totalnya bu?"})
    text = sse_text(parse_sse(r.text))
    assert "162" not in text
    assert "kerjakan sendiri" in text
    stored = (await client.get(f"/v1/attempts/{attempt['id']}/messages", headers=h)).json()
    assert stored[-1]["flagged"] is True and "162" not in stored[-1]["content"]
    # Kunci jawaban tidak pernah dikirim ke LLM atasan
    assert "answer_key" not in llm.calls[0]["system"] and "162000000" not in llm.calls[0]["system"]


async def test_rubric_llm_and_schema_validation(client):
    h = auth()
    await _onboard(client, h)
    mission_id = await _mission_id(client, h, "rekap-penjualan-mingguan")

    grade = {
        "criteria": [
            {"id": "r_verifikasi", "level": 4, "feedback": "Semua kesalahan AI ditemukan."},
            {"id": "r_ringkasan", "level": 3, "feedback": "Ringkas dan jelas."},
            {"id": "r_saran", "level": 2, "feedback": "Saran masih umum."},
        ],
        "overall_feedback": "Bagus.",
    }
    use_llm(FakeLLM({"RubricGrade": grade}))
    attempt = (await client.post(f"/v1/missions/{mission_id}/attempts", headers=h)).json()
    result = (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": CORRECT_REKAP})).json()
    assert result["engine"] == "llm" and result["review_status"] == "none"
    expected_rubric = round(100 * (2 * 1 + 1.5 * (2 / 3) + 1 * (1 / 3)) / 4.5, 1)
    assert result["rubric"]["score"] == expected_rubric
    assert result["score"] == round(0.5 * 100 + 0.5 * expected_rubric, 1)

    # Kriteria hilang = keluaran tidak valid -> cadangan + masuk antrean review
    use_llm(FakeLLM({"RubricGrade": {**grade, "criteria": grade["criteria"][:2]}}))
    mission_id = await _mission_id(client, h, "balas-keluhan-pelanggan")
    attempt = (await client.post(f"/v1/missions/{mission_id}/attempts", headers=h)).json()
    answers = {
        "kompensasi": "Voucher Rp25.000 dan refund Rp18.000",
        "jumlah_refund": 18000,
        "email_balasan": "Mohon maaf Ibu Sari atas pesanan INV-20817.",
        "prompt_ai": "Konteks: keluhan pelanggan. Kebijakan: ... Nada: empati. Format: email.",
    }
    result = (await client.post(f"/v1/attempts/{attempt['id']}/submit", headers=h, json={"answers": answers})).json()
    assert result["engine"] == "fallback" and result["review_status"] == "sampled"
    assert result["objective"]["score"] == 100

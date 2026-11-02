"""Unit test aturan yang menentukan angka dan keamanan keluaran."""

import hashlib

import pytest

from app.models import Mission
from app.services import billing, grading, scoring
from app.services.audits import rule_extract
from app.services.guardrails import SentenceGuard, answer_leak_check, layoff_check, mentions_layoff, sanitize
from app.services.textutil import extract_numbers
from scripts._common import DEMO_DIR, load_json


def demo_mission(slug: str) -> Mission:
    row = next(m for m in load_json(DEMO_DIR / "missions.json")["missions"] if m["slug"] == slug)
    return Mission(
        slug=slug,
        title=row["title"],
        brief=row["brief"],
        answer_fields=row["answer_fields"],
        answer_key=row["answer_key"],
        rubric=row["rubric"],
        pass_score=row["pass_score"],
        objective_weight=row["objective_weight"],
    )


# ---------- Skor ----------


@pytest.mark.parametrize(
    ("score", "group"),
    [(0.70, "high"), (0.95, "high"), (0.6999, "medium"), (0.40, "medium"), (0.3999, "low"), (0.0, "low")],
)
def test_group_thresholds(score, group):
    assert scoring.group_for(score) == group


def test_summarize_numbers():
    tasks = [
        scoring.ScoredTask(hours=10, score=0.8, uses_ai=True),  # high, sudah pakai AI
        scoring.ScoredTask(hours=10, score=0.5, uses_ai=False),  # medium
        scoring.ScoredTask(hours=20, score=0.2, uses_ai=False),  # low
    ]
    s = scoring.summarize(tasks, proven_skills=1)
    assert s["total_hours"] == 40
    assert s["exposure_index"] == pytest.approx((8 + 5 + 4) / 40, abs=1e-3)
    assert s["hours_saved_week"] == pytest.approx(10 * 0.5 + 10 * 0.25)
    assert s["hours_saved_now"] == pytest.approx(5.0)
    assert s["ai_adoption"] == pytest.approx(0.5)
    expected = round(100 * (0.5 * 0.5 + 0.3 * (1 - 0.425) + 0.2 * (1 / 5)))
    assert s["readiness_score"] == expected
    assert s["groups"]["low"]["share"] == 0.5


def test_fill_missing_hours():
    assert scoring.fill_missing_hours([10, None, None]) == [10, 15, 15]
    # Jam yang disebut sudah melebihi 40: tetap minimal 2 jam per tugas tanpa durasi
    assert scoring.fill_missing_hours([45, None]) == [45, 2]
    assert scoring.fill_missing_hours([]) == []


# ---------- Guardrail ----------


def test_layoff_sentences_are_replaced():
    text = "Skormu 60. Kamu bisa di-PHK kalau tidak belajar. Ayo mulai dari satu tugas."
    clean, flagged = sanitize(text)
    assert flagged
    assert "PHK" not in clean
    assert "Ayo mulai" in clean
    assert mentions_layoff("Posisimu akan hilang karena AI")
    assert not mentions_layoff("Tugas merekap data bisa dibantu AI")


def test_sentence_guard_across_chunks():
    guard = SentenceGuard([layoff_check])
    out = ""
    for chunk in ["Halo. Pekerjaan", "mu akan hi", "lang tahun depan. ", "Tetap semangat!"]:
        out += guard.feed(chunk)
    out += guard.flush()
    assert "hilang" not in out
    assert out.startswith("Halo. ")
    assert out.endswith("Tetap semangat!")
    assert guard.flagged


def test_answer_leak_check():
    mission = demo_mission("rekap-penjualan-mingguan")
    check = answer_leak_check(mission.answer_key)
    assert check("Totalnya Rp 162.000.000 ya.") is not None
    assert check("Total 162,000,000.") is not None
    assert check("Coba jumlahkan sendiri dari tabel.") is None
    # Opsi pilihan terlihat oleh pengguna, jadi menyebut "Timur" bukan bocoran
    assert check("Data wilayah Timur ada di baris ketiga.") is None


def test_leak_guard_false_skips_value_from_data():
    mission = demo_mission("balas-keluhan-pelanggan")
    check = answer_leak_check(mission.answer_key)
    assert check("Ongkirnya Rp18.000 sesuai data pesanan.") is None


def test_extract_numbers_formats():
    assert 1250000.5 in extract_numbers("Rp1.250.000,5")
    assert 1250000.5 in extract_numbers("1,250,000.5")
    assert 8.0 in extract_numbers("naik 8%")


# ---------- Penilaian objektif ----------


def test_objective_all_correct():
    mission = demo_mission("rekap-penjualan-mingguan")
    answers = {
        "total_penjualan": "Rp162.000.000",
        "wilayah_tertinggi": "Timur",
        "perubahan_persen": 8,
        "koreksi_ringkasan_ai": "Totalnya 162 juta bukan 165, naiknya 8% bukan 10%, tertinggi Timur.",
        "ringkasan": "x",
    }
    score, checks = grading.objective_check(mission, answers)
    assert score == 100
    assert all(c["passed"] for c in checks)


def test_objective_wrong_answers_give_hint_not_key():
    mission = demo_mission("rekap-penjualan-mingguan")
    answers = {"total_penjualan": 165000000, "wilayah_tertinggi": "Utara", "perubahan_persen": 10,
               "koreksi_ringkasan_ai": "tidak ada", "ringkasan": "x"}
    score, checks = grading.objective_check(mission, answers)
    assert score == 0
    for c in checks:
        assert "162" not in c["feedback"]
        assert "Timur" not in c["feedback"]


@pytest.mark.parametrize(
    ("value", "ok"),
    [("Andi, Dodi", True), ("dodi dan andi", True), ("Andi, Bunga, Dodi", False), ("Andi", False), ("Andika, Dodi", False)],
)
def test_shortlist_regex(value, ok):
    mission = demo_mission("saring-kandidat-adil")
    _, checks = grading.objective_check(mission, {"shortlist": value, "catatan_bias_ai": "", "deskripsi_lowongan": ""})
    assert next(c for c in checks if c["id"] == "k_shortlist")["passed"] is ok


def test_validate_answers_rejects_missing_and_unknown():
    mission = demo_mission("rekap-penjualan-mingguan")
    with pytest.raises(Exception) as exc:
        grading.validate_answers(mission, {"total_penjualan": "abc", "kolom_asing": 1})
    details = exc.value.details
    fields = {d["field_id"] for d in details}
    assert {"total_penjualan", "kolom_asing", "ringkasan"} <= fields


def test_heuristic_rubric_and_combine():
    mission = demo_mission("rekap-penjualan-mingguan")
    rubric = grading.heuristic_rubric(mission, {"koreksi_ringkasan_ai": "salah, seharusnya 162 dan Timur"})
    assert 0 <= rubric["score"] <= 100
    assert {c["id"] for c in rubric["criteria"]} == {r["id"] for r in mission.rubric}
    assert grading.combine(mission, 100, 50) == 75


# ---------- Ekstraksi cadangan ----------


def test_rule_extract():
    story = (
        "Setiap hari saya merekap data penjualan di Excel sekitar 2 jam. "
        "Lalu saya membalas email pelanggan pakai ChatGPT. Saya juga mengarsipkan faktur."
    )
    tasks = rule_extract(story)
    assert len(tasks) == 3
    assert tasks[0].hours_per_week == 10  # 2 jam/hari x 5 hari
    assert tasks[0].description == "Merekap data penjualan di Excel"
    assert tasks[1].uses_ai is True
    assert tasks[2].hours_per_week is None


# ---------- Pembayaran ----------


def test_midtrans_signature_and_status(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "midtrans_server_key", "SB-server-key")
    payload = {"order_id": "LVL-1", "status_code": "200", "gross_amount": "49000.00"}
    payload["signature_key"] = hashlib.sha512(b"LVL-120049000.00SB-server-key").hexdigest()
    assert billing.verify_midtrans_signature(payload)
    assert not billing.verify_midtrans_signature({**payload, "gross_amount": "1.00"})
    assert billing.map_midtrans_status("settlement", None) == "paid"
    assert billing.map_midtrans_status("capture", "challenge") == "pending"
    assert billing.map_midtrans_status("expire", None) == "expired"

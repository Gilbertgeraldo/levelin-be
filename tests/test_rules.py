"""Unit test aturan yang menentukan angka dan keamanan keluaran (sama dengan prototipe Tim Hearz)."""

import hashlib

import pytest

from app.models import Mission
from app.services import billing, grading, scoring
from app.services.audits import parse_hours, rule_extract
from app.services.guardrails import SentenceGuard, answer_leak_check, layoff_check, mentions_layoff, sanitize
from app.services.textutil import extract_numbers
from scripts._common import ROOT, load_json

RINA_STORY = (
    "Tiap Senin aku rekap absensi dari 3 cabang ke Excel lalu kirim ke HRD, kira-kira 3 jam. Setiap hari input "
    "faktur penjualan ke sistem akuntansi, sekitar 1,5 jam sehari. Akhir bulan bikin laporan piutang buat "
    "manajer. Sering juga balas email vendor soal tagihan. Seminggu sekali rapat koordinasi dengan kepala cabang."
)


def mission(slug: str) -> Mission:
    row = next(m for m in load_json(ROOT / "data" / "hearz" / "missions.json")["missions"] if m["slug"] == slug)
    return Mission(
        slug=slug,
        title=row["title"],
        brief=row["brief"],
        answer_fields=row["answer_fields"],
        answer_key=row["answer_key"],
        rubric=row["rubric"],
        simulation_data=row["simulation_data"],
    )


# ---------- Skor ----------


@pytest.mark.parametrize(
    ("score", "group"),
    [(0.70, "auto"), (0.95, "auto"), (0.6999, "assist"), (0.40, "assist"), (0.3999, "human"), (None, "none")],
)
def test_group_thresholds(score, group):
    assert scoring.group_for(score) == group


def test_manual_group_only_for_unmatched():
    assert scoring.effective_group(None, "assist") == "assist"
    assert scoring.effective_group(None, None) == "none"
    assert scoring.effective_group(0.8, "human") == "auto"


def test_summary_matches_v01_payload_example():
    tasks = [scoring.ScoredTask(7.5, "auto"), scoring.ScoredTask(13.5, "assist"), scoring.ScoredTask(2, "human")]
    s = scoring.summarize(tasks, set())
    assert s["hours_by_group"] == {"auto": 7.5, "assist": 13.5, "human": 2, "none": 0}
    assert s["exposed_hours"] == 21
    assert s["hours_saved"] == 8.55
    assert s["save_factor"] == {"auto": 0.6, "assist": 0.3}
    assert s["readiness_score"] == 0


def test_readiness_rule_from_prototype():
    tasks = [
        scoring.ScoredTask(4, "auto", kind="data"),
        scoring.ScoredTask(6, "assist", uses_ai=True, kind="dok"),
        scoring.ScoredTask(3, "human", uses_ai=True, kind="layan"),  # tidak terpapar: tidak dihitung
        scoring.ScoredTask(2, "none"),
    ]
    assert scoring.readiness(tasks, set()) == 30  # 0,5 x 6 / 10
    assert scoring.readiness(tasks, {"data"}) == 70  # (4 + 3) / 10
    assert scoring.readiness(tasks, {"data", "dok"}) == 100
    assert scoring.readiness([scoring.ScoredTask(5, "human")], set()) == 0


def test_share_by_group_sums_to_100():
    share = scoring.share_by_group({"auto": 7.5, "assist": 13.5, "human": 2, "none": 4})
    assert share == {"auto": 32, "assist": 59, "human": 9}
    assert scoring.share_by_group({}) == {"auto": 0, "assist": 0, "human": 0}


# ---------- Ekstraksi cadangan ----------


def test_parse_hours():
    assert parse_hours("Setiap hari input faktur sekitar 1,5 jam sehari") == ("harian", 7.5)
    assert parse_hours("Tiap Senin rekap absensi kira-kira 3 jam") == ("mingguan", 3)
    assert parse_hours("Akhir bulan bikin laporan 8 jam") == ("bulanan", 2)
    assert parse_hours("Balas email vendor") == ("", scoring.DEFAULT_TASK_HOURS)


def test_rule_extract_rina_story():
    tasks = rule_extract(RINA_STORY)
    assert [t.jam_per_minggu for t in tasks] == [3, 7.5, 2, 2, 2]
    assert tasks[0].nama == "Tiap Senin aku rekap absensi dari 3 cabang ke Excel lalu kirim ke HRD"
    assert tasks[0].alat == ["Excel"]
    assert all(t.ilo_id is None for t in tasks)


# ---------- Penilaian misi ----------

M1_CORRECT = {"dup": "1", "total": "20.980.000", "top": "Cikarang", "ringkasan": "x"}
M2_EMAIL = (
    "Halo semua,\nAndi: revisi anggaran, kirim Rabu 14 Okt.\nDewi: konfirmasi katering ke 2 vendor, paling "
    "lambat Jumat 16 Okt.\nRina: rekap absensi 3 cabang, Senin 19 Okt.\nRapat berikutnya Selasa 20 Okt jam 10."
)


@pytest.mark.parametrize("total", ["20.980.000", "Rp20.980.000", "20980000", "Rp20,98 jt", "20,98 juta"])
def test_m1_total_formats(total):
    checks = grading.objective_check(mission("rapikan-data-penjualan"), {**M1_CORRECT, "total": total})
    assert all(c["ok"] for c in checks)


def test_m1_wrong_answers_give_hint_not_key():
    wrong = {"dup": "2", "total": "21.000.000", "top": "Bekasi", "ringkasan": "x"}
    checks = grading.objective_check(mission("rapikan-data-penjualan"), wrong)
    assert [c["ok"] for c in checks] == [False, False, False]
    assert all(c["hint"] and "20" not in c["hint"] and "Cikarang" not in c["hint"] for c in checks)


def test_m2_email_checks_across_subject_and_body():
    m = mission("notulen-jadi-email")
    assert all(c["ok"] for c in grading.objective_check(m, {"subjek": "Tindak lanjut rapat", "isi": M2_EMAIL}))
    missing_rina = M2_EMAIL.replace("Rina: rekap absensi 3 cabang, Senin 19 Okt.\n", "")
    oks = [c["ok"] for c in grading.objective_check(m, {"subjek": "Tindak lanjut", "isi": missing_rina})]
    assert oks == [True, True, False, True]
    # Nama di subjek, tanggal di isi: tetap terhitung (gabungan kolom)
    split = {"subjek": "Untuk Andi dan Dewi", "isi": "Tenggat 14 Oktober dan 16/10"}
    assert [c["ok"] for c in grading.objective_check(m, split)][:2] == [True, True]


def test_m3_checks():
    m = mission("rekap-absensi-cabang")
    ok = grading.objective_check(m, {"telat": 10, "alpa": "Cikarang", "nama": "Fajar (Cikarang)", "catatan": "x"})
    assert all(c["ok"] for c in ok)
    bad = grading.objective_check(m, {"telat": 9, "alpa": "Bekasi", "nama": "Lina", "catatan": "x"})
    assert not any(c["ok"] for c in bad)


def test_pass_rule():
    all_ok = [{"ok": True}, {"ok": True}]
    assert grading.is_passed(all_ok, 3.25, 0)
    assert not grading.is_passed(all_ok, 2.75, 100)
    assert not grading.is_passed([{"ok": True}, {"ok": False}], 4.0, 100)
    assert grading.is_passed(all_ok, None, 25)  # tanpa LLM: cukup 25 kata
    assert not grading.is_passed(all_ok, None, 24)


def test_validate_answers_rejects_missing_and_unknown():
    with pytest.raises(Exception) as exc:
        grading.validate_answers(mission("rapikan-data-penjualan"), {"dup": "abc", "kolom_asing": 1})
    fields = {d["field_id"] for d in exc.value.details}
    assert {"dup", "kolom_asing", "total", "top", "ringkasan"} <= fields


def test_data_text_for_table_and_notes():
    assert grading.data_text(mission("rapikan-data-penjualan")).splitlines()[0] == "No | Tanggal | Cabang | Pelanggan | Nilai | Status"
    assert "Rapat koordinasi cabang" in grading.data_text(mission("notulen-jadi-email"))


# ---------- Guardrail ----------


def test_layoff_sentences_are_replaced():
    clean, flagged = sanitize("Skormu 60. Kamu bisa di-PHK kalau tidak belajar. Ayo mulai dari satu tugas.")
    assert flagged and "PHK" not in clean and "Ayo mulai" in clean
    assert mentions_layoff("Posisimu akan hilang karena AI")
    assert not mentions_layoff("Tugas merekap data bisa dibantu AI")


def test_sentence_guard_across_chunks():
    guard = SentenceGuard([layoff_check])
    out = "".join(guard.feed(c) for c in ["Halo. Pekerjaan", "mu akan hi", "lang tahun depan. ", "Tetap semangat!"])
    out += guard.flush()
    assert "hilang" not in out and out.startswith("Halo. ") and out.endswith("Tetap semangat!")


def test_answer_leak_check_m1():
    check = answer_leak_check(mission("rapikan-data-penjualan").answer_key)
    assert check("Totalnya Rp 20.980.000 ya.") is not None
    assert check("Totalnya sekitar Rp20,98 juta.") is not None
    assert check("Coba jumlahkan sendiri dari tabel.") is None
    assert check("Data Cikarang ada di baris ketiga.") is None  # opsi pilihan memang terlihat


def test_extract_numbers_formats():
    assert 1250000.5 in extract_numbers("Rp1.250.000,5")
    assert 1250000.5 in extract_numbers("1,250,000.5")
    assert 20980000 in extract_numbers("Rp20,98 jt")


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

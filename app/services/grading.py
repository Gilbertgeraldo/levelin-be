"""Penilaian misi, sama dengan prototipe Tim Hearz.

1. Cek objektif oleh skrip; kunci jawaban tidak pernah keluar server.
2. Rubrik 4 kriteria (skala 1-4) oleh LLM dengan keluaran JSON tervalidasi.
3. Lulus = semua cek objektif benar DAN rata-rata rubrik >= 3. Tanpa LLM (cadangan): semua cek benar
   dan jawaban tertulis minimal 25 kata; hasil cadangan otomatis masuk antrean review manusia.
"""

import json
import re

from pydantic import BaseModel, Field

from app.core.errors import ApiError
from app.models import Mission
from app.services import prompts
from app.services.guardrails import answer_leak_check, layoff_check, sanitize
from app.services.llm import LLMCallInfo, LLMError, LLMService
from app.services.textutil import extract_numbers, normalize, to_text

MAX_SCORE = 4
PASS_RUBRIC_AVG = 3.0
MIN_WORDS_WITHOUT_RUBRIC = 25
FEEDBACK_LEAK_REPLACEMENT = "Periksa lagi bagian ini pada jawabanmu. "


# ---------- Validasi jawaban ----------


def validate_answers(mission: Mission, answers: dict) -> dict:
    fields = {f["id"]: f for f in mission.answer_fields or []}
    errors = []
    clean: dict = {}
    for fid, value in answers.items():
        if fid not in fields:
            errors.append({"field_id": fid, "message": "Kolom tidak dikenal."})
            continue
        clean[fid] = value
    for fid, field in fields.items():
        value = clean.get(fid)
        empty = value is None or (isinstance(value, str) and not value.strip())
        if empty:
            if field.get("required", True):
                errors.append({"field_id": fid, "message": f"Kolom \"{field.get('label', fid)}\" wajib diisi."})
            continue
        kind = field.get("type", "text")
        if kind == "number" and not isinstance(value, int | float) and not extract_numbers(str(value)):
            errors.append({"field_id": fid, "message": "Isi dengan angka."})
        elif kind == "choice" and value not in (field.get("options") or []):
            errors.append({"field_id": fid, "message": "Pilihan tidak tersedia."})
        elif isinstance(value, str) and len(value) > 20_000:
            errors.append({"field_id": fid, "message": "Jawaban terlalu panjang."})
    if errors:
        raise ApiError(400, "invalid_answers", "Jawaban belum lengkap atau tidak valid.", details=errors)
    return clean


def written_words(answers: dict) -> int:
    return len(to_text(answers).split())


# ---------- Cek objektif (skrip) ----------


def _check_value(check: dict, answers: dict):
    """Nilai yang diperiksa: satu kolom, atau beberapa kolom digabung (mis. subjek + isi email)."""
    if check.get("field_ids"):
        return "\n".join(to_text(answers.get(f)) for f in check["field_ids"])
    return answers.get(check["field_id"])


def _check_one(check: dict, value) -> bool:
    kind = check.get("type")
    expected = check.get("expected")
    if value is None or (isinstance(value, str) and not value.strip()):
        return False
    if kind == "number":
        candidates = {float(value)} if isinstance(value, int | float) else extract_numbers(str(value))
        target = float(expected)
        tolerance = float(check.get("tolerance") or 0)
        if check.get("tolerance_pct"):
            tolerance = max(tolerance, abs(target) * float(check["tolerance_pct"]) / 100)
        return any(abs(c - target) <= tolerance + 1e-9 for c in candidates)
    text = normalize(to_text(value))
    if kind == "exact":
        options = expected if isinstance(expected, list) else [expected]
        return any(text == normalize(str(o)) for o in options)
    if kind == "choice":
        options = expected if isinstance(expected, list) else [expected]
        return value in options
    if kind == "contains":
        keywords = expected if isinstance(expected, list) else [expected]
        hits = sum(1 for k in keywords if normalize(str(k)) in text)
        return hits >= int(check.get("min_matches") or len(keywords))
    if kind == "regex":
        return re.search(str(expected), to_text(value), re.IGNORECASE) is not None
    return False


def objective_check(mission: Mission, answers: dict) -> list[dict]:
    """Hasil per cek: {id, label, ok, hint}. Hint hanya untuk cek yang gagal dan tidak membocorkan jawaban."""
    labels = {f["id"]: f.get("label", f["id"]) for f in mission.answer_fields or []}
    results = []
    for check in mission.answer_key or []:
        ok = _check_one(check, _check_value(check, answers))
        results.append(
            {
                "id": check["id"],
                "label": check.get("label") or labels.get(check["field_id"], check["field_id"]),
                "ok": ok,
                "hint": None if ok else check.get("hint", "Periksa lagi bagian ini."),
            }
        )
    return results


# ---------- Rubrik (LLM) ----------


class RubricItem(BaseModel):
    id: str
    score: int = Field(ge=1, le=MAX_SCORE)
    note: str


class RubricGrade(BaseModel):
    rubric: list[RubricItem]
    feedback: str
    next_step: str


def data_text(mission: Mission) -> str:
    data = mission.simulation_data or {}
    if "table" in data:
        table = data["table"]
        rows = [" | ".join(table.get("columns", []))] + [" | ".join(map(str, r)) for r in table.get("rows", [])]
        return "\n".join(rows)
    if "notes" in data:
        return str(data["notes"])
    return json.dumps(data, ensure_ascii=False, indent=1)


def answers_text(mission: Mission, answers: dict) -> str:
    return "\n\n".join(
        f"{f.get('label', f['id'])}:\n{to_text(answers.get(f['id']))}" for f in mission.answer_fields or []
    )


async def llm_rubric(
    llm: LLMService,
    mission: Mission,
    answers: dict,
    ai_usage: str | None,
    checks: list[dict],
    info: LLMCallInfo,
) -> dict:
    rubric = mission.rubric or []
    prompt = (
        f"Misi: {mission.title}\nBrief: {mission.brief}\n\nData misi:\n{data_text(mission)}\n\n"
        "Hasil cek otomatis dari aplikasi (sudah final, jangan diubah):\n"
        + "\n".join(f"- {c['label']}: {'lulus' if c['ok'] else 'tidak lulus'}" for c in checks)
        + "\n\nRubrik:\n"
        + "\n".join(f"- {r['id']}: {r['criterion']}" for r in rubric)
        + f"\n\n<jawaban>\n{answers_text(mission, answers)[:4000]}\n</jawaban>"
        + (f"\n\nCara staf memakai AI: <jawaban>{ai_usage[:1000]}</jawaban>" if ai_usage else "")
    )
    grade = await llm.generate_json(
        tier="main",
        system=prompts.RUBRIC_SYSTEM,
        prompt=prompt,
        schema=prompts.RUBRIC_SCHEMA,
        model_cls=RubricGrade,
        max_tokens=8000,
        effort="medium",
        info=info,
    )
    by_id = {item.id: item for item in grade.rubric}
    if sorted(by_id) != sorted(r["id"] for r in rubric) or len(grade.rubric) != len(rubric):
        raise LLMError("Kriteria rubrik dari LLM tidak lengkap atau ganda")

    # Jawaban yang sudah benar boleh disebut; yang masih salah tidak boleh terbongkar
    failed = {c["id"] for c in checks if not c["ok"]}
    unrevealed = [k for k in mission.answer_key or [] if k["id"] in failed]
    guards = [layoff_check, answer_leak_check(unrevealed, FEEDBACK_LEAK_REPLACEMENT)]

    def clean(text: str) -> str:
        return sanitize(text.strip(), guards)[0].strip()

    items = [
        {"criterion": r["criterion"], "score": by_id[r["id"]].score, "note": clean(by_id[r["id"]].note)}
        for r in rubric
    ]
    return {
        "rubric": items,
        "rubric_avg": round(sum(i["score"] for i in items) / len(items), 2) if items else None,
        "feedback": clean(grade.feedback),
        "next_step": clean(grade.next_step),
    }


def fallback_feedback(checks: list[dict], words: int) -> dict:
    """Umpan balik tanpa LLM, sama dengan mode demo prototipe."""
    all_ok = all(c["ok"] for c in checks)
    if not all_ok:
        feedback = (
            "Masih ada cek otomatis yang belum lulus. Lihat petunjuk di bawah tiap cek, perbaiki, "
            "lalu kumpulkan lagi."
        )
        next_step = "Perbaiki bagian yang belum lulus sesuai petunjuknya."
    elif words < MIN_WORDS_WITHOUT_RUBRIC:
        feedback = (
            "Angkanya tepat. Lengkapi bagian tulisan (ringkasan atau catatan) minimal beberapa kalimat, "
            "lalu kumpulkan lagi."
        )
        next_step = "Tambahkan ringkasan atau catatan beberapa kalimat."
    else:
        feedback = (
            "Angkanya tepat dan jawabanmu lengkap. Penilai AI sedang tidak tersedia, jadi kualitas "
            "tulisanmu akan ditinjau tim Levelin."
        )
        next_step = "Tunggu hasil tinjauan untuk umpan balik tulisan."
    return {"rubric": None, "rubric_avg": None, "feedback": feedback, "next_step": next_step}


def is_passed(checks: list[dict], rubric_avg: float | None, words: int) -> bool:
    if not all(c["ok"] for c in checks):
        return False
    if rubric_avg is not None:
        return rubric_avg >= PASS_RUBRIC_AVG
    return words >= MIN_WORDS_WITHOUT_RUBRIC


def display_score(checks: list[dict], rubric_avg: float | None) -> float:
    """Nilai 0-100 untuk Paspor dan peninjau: rubrik bila ada, selain itu porsi cek yang benar."""
    if rubric_avg is not None:
        return round(100 * rubric_avg / MAX_SCORE, 1)
    return round(100 * sum(c["ok"] for c in checks) / len(checks), 1) if checks else 0.0

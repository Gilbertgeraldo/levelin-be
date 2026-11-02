"""Penilaian misi: cek objektif oleh skrip (kunci jawaban tidak pernah keluar server),
lalu rubrik oleh LLM dengan keluaran JSON tervalidasi. Bila LLM gagal, dipakai penilaian
heuristik dan hasilnya otomatis masuk antrean review manusia.
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

MAX_LEVEL = 4
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


# ---------- Cek objektif (skrip) ----------


def _check_one(check: dict, value) -> bool:
    kind = check.get("type")
    expected = check.get("expected")
    if value is None:
        return False
    if kind == "number":
        candidates = {float(value)} if isinstance(value, int | float) else extract_numbers(str(value))
        target = float(expected)
        tolerance = float(check.get("tolerance", 0))
        if "tolerance_pct" in check:
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
        return hits >= int(check.get("min_matches", len(keywords)))
    if kind == "regex":
        return re.search(str(expected), to_text(value), re.IGNORECASE) is not None
    return False


def objective_check(mission: Mission, answers: dict) -> tuple[float | None, list[dict]]:
    key = mission.answer_key or []
    if not key:
        return None, []
    labels = {f["id"]: f.get("label", f["id"]) for f in mission.answer_fields or []}
    total = got = 0.0
    results = []
    for check in key:
        weight = float(check.get("weight", 1))
        ok = _check_one(check, answers.get(check["field_id"]))
        total += weight
        got += weight if ok else 0
        results.append(
            {
                "id": check["id"],
                "field_id": check["field_id"],
                "label": check.get("label") or labels.get(check["field_id"], check["field_id"]),
                "passed": ok,
                "weight": weight,
                # hint ditulis tanpa membocorkan jawaban
                "feedback": "Tepat." if ok else check.get("hint", "Periksa lagi bagian ini."),
            }
        )
    return round(100 * got / total, 1) if total else None, results


# ---------- Rubrik (LLM) ----------


class RubricCriterion(BaseModel):
    id: str
    level: int = Field(ge=1, le=MAX_LEVEL)
    feedback: str


class RubricGrade(BaseModel):
    criteria: list[RubricCriterion]
    overall_feedback: str


def _rubric_score(rubric: list[dict], levels: dict[str, int]) -> float:
    total = sum(float(r.get("weight", 1)) for r in rubric)
    if not total:
        return 0.0
    got = sum(float(r.get("weight", 1)) * (levels[r["id"]] - 1) / (MAX_LEVEL - 1) for r in rubric)
    return round(100 * got / total, 1)


def _criteria_view(rubric: list[dict], levels: dict[str, int], feedback: dict[str, str]) -> list[dict]:
    return [
        {
            "id": r["id"],
            "criterion": r.get("criterion", r["id"]),
            "weight": float(r.get("weight", 1)),
            "level": levels[r["id"]],
            "max_level": MAX_LEVEL,
            "feedback": feedback.get(r["id"], ""),
        }
        for r in rubric
    ]


async def llm_rubric(
    llm: LLMService, mission: Mission, answers: dict, objective: list[dict], info: LLMCallInfo
) -> dict:
    rubric = mission.rubric or []
    payload = {
        "brief": mission.brief,
        "rubrik": [
            {"id": r["id"], "kriteria": r.get("criterion"), "deskripsi": r.get("description"), "bobot": r.get("weight", 1)}
            for r in rubric
        ],
        "kolom_jawaban": [{"id": f["id"], "label": f.get("label")} for f in mission.answer_fields or []],
        "hasil_cek_objektif": [{"kolom": o["label"], "benar": o["passed"]} for o in objective],
    }
    prompt = (
        "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```\n\n"
        "<jawaban>\n" + json.dumps(answers, ensure_ascii=False, indent=1) + "\n</jawaban>"
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
    expected_ids = [r["id"] for r in rubric]
    got_ids = [c.id for c in grade.criteria]
    if sorted(got_ids) != sorted(expected_ids):
        raise LLMError("Kriteria rubrik dari LLM tidak lengkap atau ganda")
    # Jawaban yang sudah benar boleh disebut; yang masih salah tidak boleh terbongkar
    failed = {o["id"] for o in objective if not o["passed"]}
    unrevealed = [k for k in mission.answer_key or [] if k["id"] in failed]
    checks = [layoff_check, answer_leak_check(unrevealed, FEEDBACK_LEAK_REPLACEMENT)]

    def clean(text: str) -> str:
        return sanitize(text.strip(), checks)[0].strip()

    levels = {c.id: c.level for c in grade.criteria}
    feedback = {c.id: clean(c.feedback) for c in grade.criteria}
    return {
        "score": _rubric_score(rubric, levels),
        "criteria": _criteria_view(rubric, levels, feedback),
        "overall_feedback": clean(grade.overall_feedback),
    }


def heuristic_rubric(mission: Mission, answers: dict) -> dict:
    """Penilaian sementara tanpa LLM: cakupan kata kunci rubrik dan kelengkapan jawaban."""
    rubric = mission.rubric or []
    text = normalize(to_text(answers))
    words = len(text.split())
    levels: dict[str, int] = {}
    feedback: dict[str, str] = {}
    for r in rubric:
        keywords = r.get("keywords") or []
        if keywords:
            ratio = sum(1 for k in keywords if normalize(k) in text) / len(keywords)
            levels[r["id"]] = 1 + round(ratio * (MAX_LEVEL - 1))
        else:
            levels[r["id"]] = 3 if words >= 60 else 2 if words >= 15 else 1
        feedback[r["id"]] = "Penilaian otomatis sementara; akan ditinjau tim Levelin."
    return {
        "score": _rubric_score(rubric, levels),
        "criteria": _criteria_view(rubric, levels, feedback),
        "overall_feedback": (
            "Nilai ini sementara karena penilai AI sedang tidak tersedia. Tim peninjau akan "
            "memeriksa jawabanmu dan nilai bisa berubah."
        ),
    }


def combine(mission: Mission, objective_score: float | None, rubric_score: float) -> float:
    if objective_score is None:
        return rubric_score
    w = mission.objective_weight
    return round(w * objective_score + (1 - w) * rubric_score, 1)

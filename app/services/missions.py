"""Misi latihan kerja: rekomendasi, percobaan, atasan AI, dan penilaian (logika prototipe Tim Hearz)."""

import random
import re
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError, not_found
from app.db.base import utcnow
from app.models import Attempt, AttemptMessage, Audit, Mission, PassportEntry, Review, TaskKind, User
from app.services import grading, prompts, scoring
from app.services.audits import latest_audit, refresh_readiness
from app.services.billing import mission_quota
from app.services.llm import LLMCallInfo, LLMError, LLMService
from app.services.textutil import to_text

HISTORY_LIMIT = 20
EXCERPT_CHARS = 280


def visible_filter(user: User):
    return (
        Mission.is_published.is_(True),
        or_(Mission.org_id.is_(None), Mission.org_id == user.org_id),
    )


async def get_visible_mission(db: AsyncSession, user: User, mission_id: uuid.UUID) -> Mission:
    mission = await db.get(Mission, mission_id)
    if mission is None or not mission.is_published or mission.org_id not in (None, user.org_id):
        raise not_found("Misi")
    return mission


async def mission_statuses(db: AsyncSession, user_id: uuid.UUID, mission_ids: list[uuid.UUID]) -> dict:
    """Status per misi: passed > under_review > in_progress > failed > not_started."""
    if not mission_ids:
        return {}
    attempts = (
        await db.scalars(
            select(Attempt).where(Attempt.user_id == user_id, Attempt.mission_id.in_(mission_ids))
        )
    ).all()
    rank = {"not_started": 0, "failed": 1, "in_progress": 2, "under_review": 3, "passed": 4}
    out: dict = {}
    for a in attempts:
        if a.passed:
            status = "passed"
        elif a.review_status == "appealed":
            status = "under_review"
        elif a.status == "in_progress":
            status = "in_progress"
        else:
            status = "failed"
        if rank[status] > rank[out.get(a.mission_id, "not_started")]:
            out[a.mission_id] = status
    return out


# ---------- Rekomendasi ----------


def exposed_kinds(audit: Audit | None) -> list[tuple[str, float]]:
    """Jenis pekerjaan dengan jam terpapar AI (auto + assist) terbesar lebih dulu."""
    if audit is None:
        return []
    hours: dict[str, float] = {}
    for t in audit.tasks:
        kind = t.template.kind if t.template else None
        if kind and t.group in scoring.EXPOSED:
            hours[kind] = hours.get(kind, 0) + t.hours_per_week
    return sorted(hours.items(), key=lambda kv: kv[1], reverse=True)


async def recommended_order(db: AsyncSession, user: User) -> tuple[list[Mission], dict[uuid.UUID, str]]:
    """Urutan misi seperti prototipe: misi untuk jenis tugas paling terpapar dulu, lalu sisanya.

    Mengembalikan (semua misi terurut, alasan rekomendasi per misi yang direkomendasikan).
    """
    missions = list((await db.scalars(select(Mission).where(*visible_filter(user)).order_by(Mission.title))).all())
    by_slug = {m.slug: m for m in missions}
    kinds = {k.code: k for k in (await db.scalars(select(TaskKind))).all()}
    audit = await latest_audit(db, user.id)

    ordered: list[Mission] = []
    reasons: dict[uuid.UUID, str] = {}
    for kind_code, _ in exposed_kinds(audit):
        kind = kinds.get(kind_code)
        mission = by_slug.get(kind.mission_slug) if kind and kind.mission_slug else None
        if mission is not None and mission not in ordered:
            ordered.append(mission)
            reasons[mission.id] = f"Melatih tugas “{kind.label}” yang paling banyak bergeser di Peta Tugasmu"
    # Misi kustom organisasi lalu bank misi umum
    for mission in sorted(missions, key=lambda m: (m.org_id is None, m.title)):
        if mission not in ordered:
            ordered.append(mission)
    return ordered, reasons


async def first_recommended_title(db: AsyncSession, user: User) -> str | None:
    ordered, _ = await recommended_order(db, user)
    return ordered[0].title if ordered else None


# ---------- Percobaan ----------


async def start_attempt(db: AsyncSession, user: User, mission: Mission) -> tuple[Attempt, bool, dict]:
    existing = await db.scalar(
        select(Attempt)
        .where(Attempt.user_id == user.id, Attempt.mission_id == mission.id, Attempt.status == "in_progress")
        .order_by(Attempt.created_at.desc())
        .limit(1)
    )
    quota = await mission_quota(db, user)
    if existing is not None:
        return existing, False, quota
    if quota["remaining"] <= 0:
        raise ApiError(
            429,
            "quota_exceeded",
            "Kuota misi periode ini sudah habis. Upgrade paket atau tunggu periode berikutnya.",
            details={k: quota[k] for k in ("plan", "mission_quota", "used")},
        )
    attempt = Attempt(user_id=user.id, mission_id=mission.id, status="in_progress", draft={})
    db.add(attempt)
    await db.flush()
    quota = {**quota, "used": quota["used"] + 1, "remaining": quota["remaining"] - 1}
    return attempt, True, quota


async def get_owned_attempt(db: AsyncSession, user: User, attempt_id: uuid.UUID) -> Attempt:
    attempt = await db.get(Attempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:
        raise not_found("Percobaan")
    return attempt


def require_in_progress(attempt: Attempt) -> None:
    if attempt.status != "in_progress":
        raise ApiError(409, "attempt_closed", "Percobaan ini sudah dikumpulkan.")


# ---------- Atasan AI ----------


def boss_system(mission: Mission) -> str:
    persona = mission.boss_persona or {}
    return prompts.BOSS_SYSTEM_TEMPLATE.format(
        name=persona.get("name", "Bu Ratna"),
        role=persona.get("role", "atasanmu"),
        style=persona.get("style", "wajar seperti atasan di kantor"),
        title=mission.title,
        brief=mission.brief,
        data=grading.data_text(mission),
        standards="; ".join(r["criterion"] for r in mission.rubric or []),
    )


async def chat_history(db: AsyncSession, attempt_id: uuid.UUID) -> list[AttemptMessage]:
    rows = (
        await db.scalars(
            select(AttemptMessage)
            .where(AttemptMessage.attempt_id == attempt_id)
            .order_by(AttemptMessage.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
    ).all()
    return list(reversed(rows))


def to_llm_messages(history: list[AttemptMessage]) -> list[dict]:
    """Gabungkan pesan berurutan dengan peran sama dan pastikan diawali pesan pengguna."""
    messages: list[dict] = []
    for m in history:
        if messages and messages[-1]["role"] == m.role:
            messages[-1]["content"] += "\n\n" + m.content
        else:
            messages.append({"role": m.role, "content": m.content})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


_ASKS_FOR_ANSWER = re.compile(
    r"\b(jawab(an|annya)?|kerjakan|kerjain|hitung(kan|in)?|buat(kan|in)|tulis(kan|in)|isi(kan)?|rangkum(kan)?|berapa (total|hasil))\b",
    re.IGNORECASE,
)


def boss_fallback_reply(mission: Mission, message: str) -> str:
    if _ASKS_FOR_ANSWER.search(message):
        return (
            "Itu bagian kamu ya. Coba olah sendiri dari data misi, boleh dibantu AI asal hasilnya kamu "
            "periksa lagi. Kalau ada yang ambigu, sebutkan asumsimu di jawaban."
        )
    if re.search(r"\b(format|bentuk|panjang|deadline|kapan)\b", message, re.IGNORECASE):
        return "Ikuti saja yang diminta di brief, ringkas dan jelas. Yang penting angkanya bisa dipertanggungjawabkan."
    return (
        "Pertanyaan bagus. Semua yang kamu perlukan ada di data misi. Fokus ke yang diminta di brief, "
        "lalu kumpulkan kalau sudah yakin."
    )


# ---------- Penilaian ----------


def excerpt_of(mission: Mission, answers: dict) -> str | None:
    for field in mission.answer_fields or []:
        if field.get("type") == "long_text" and to_text(answers.get(field["id"])).strip():
            return to_text(answers[field["id"]]).strip()[:EXCERPT_CHARS]
    return None


async def award_passport(
    db: AsyncSession, attempt: Attempt, mission: Mission, checks: list[dict] | None = None
) -> list[PassportEntry]:
    checks = checks if checks is not None else (attempt.result or {}).get("checks", [])
    entries = []
    for skill in mission.skills:
        entry = PassportEntry(
            user_id=attempt.user_id,
            skill_id=skill.id,
            attempt_id=attempt.id,
            mission_id=mission.id,
            score=attempt.score or 0,
            rubric_avg=attempt.rubric_avg,
            checks_passed=sum(1 for c in checks if c["ok"]),
            checks_total=len(checks),
            excerpt=excerpt_of(mission, attempt.answers or {}),
        )
        db.add(entry)
        entries.append(entry)
    await db.flush()
    return entries


async def revoke_passport(db: AsyncSession, attempt: Attempt) -> None:
    entries = (await db.scalars(select(PassportEntry).where(PassportEntry.attempt_id == attempt.id))).all()
    for entry in entries:
        await db.delete(entry)


async def update_passport_score(db: AsyncSession, attempt: Attempt) -> None:
    entries = (await db.scalars(select(PassportEntry).where(PassportEntry.attempt_id == attempt.id))).all()
    for entry in entries:
        entry.score = attempt.score or 0


async def submit_attempt(
    db: AsyncSession,
    attempt: Attempt,
    mission: Mission,
    answers: dict,
    ai_usage: str | None,
    llm: LLMService,
    info: LLMCallInfo,
) -> None:
    clean = grading.validate_answers(mission, answers)
    checks = grading.objective_check(mission, clean)
    words = grading.written_words(clean)
    try:
        graded = await grading.llm_rubric(llm, mission, clean, ai_usage, checks, info)
        engine = "llm"
    except LLMError:
        graded = grading.fallback_feedback(checks, words)
        engine = "fallback"

    now = utcnow()
    attempt.answers = clean
    attempt.ai_usage = (ai_usage or "").strip() or None
    attempt.draft = {}
    attempt.status = "graded"
    attempt.submitted_at = now
    attempt.objective_score = round(100 * sum(c["ok"] for c in checks) / len(checks), 1) if checks else None
    attempt.rubric_avg = graded["rubric_avg"]
    attempt.rubric_score = (
        round(100 * graded["rubric_avg"] / grading.MAX_SCORE, 1) if graded["rubric_avg"] is not None else None
    )
    attempt.score = grading.display_score(checks, graded["rubric_avg"])
    attempt.passed = grading.is_passed(checks, graded["rubric_avg"], words)
    attempt.next_step = graded["next_step"]
    attempt.engine = engine

    entries = await award_passport(db, attempt, mission, checks) if attempt.passed else []

    # Penilaian cadangan selalu ditinjau; penilaian LLM disampel acak untuk kontrol mutu
    if engine == "fallback" or random.random() < get_settings().review_sample_rate:
        attempt.review_status = "sampled"
        db.add(Review(attempt_id=attempt.id, kind="sample", status="pending", original_score=attempt.score))

    attempt.result = {
        "checks": checks,
        "rubric": graded["rubric"],
        "rubric_avg": graded["rubric_avg"],
        "feedback": graded["feedback"],
        "next_step": graded["next_step"],
        "skills_earned": [{"id": s.id, "code": s.code, "label": s.label} for s in mission.skills] if entries else [],
        "passport_evidence_id": str(entries[0].id) if entries else None,
        "graded_at": now.isoformat(),
    }
    await db.flush()
    if attempt.passed:
        await refresh_readiness(db, attempt.user_id)


async def result_view(db: AsyncSession, attempt: Attempt, mission: Mission) -> dict:
    """Nota penilaian sesuai payload v0.1, ditambah info percobaan."""
    result = attempt.result or {}
    audit = await latest_audit(db, attempt.user_id)
    return {
        "attempt_id": attempt.id,
        "mission": {"id": mission.id, "title": mission.title},
        "status": attempt.status,
        "passed": attempt.passed,
        "checks": [{"label": c["label"], "ok": c["ok"], "hint": c.get("hint")} for c in result.get("checks", [])],
        "rubric": result.get("rubric"),
        "rubric_avg": result.get("rubric_avg"),
        "feedback": result.get("feedback"),
        "next_step": result.get("next_step"),
        "passport_evidence_id": result.get("passport_evidence_id"),
        "readiness_score": audit.readiness_score if audit else None,
        "skills_earned": result.get("skills_earned", []),
        "engine": attempt.engine,
        "review_status": attempt.review_status,
        "submitted_at": attempt.submitted_at,
    }

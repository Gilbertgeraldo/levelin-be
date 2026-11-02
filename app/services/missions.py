import json
import random
import re
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError, not_found
from app.db.base import utcnow
from app.models import (
    Attempt,
    AttemptMessage,
    Mission,
    Occupation,
    PassportEntry,
    Plan,
    Profile,
    Review,
    User,
)
from app.services import grading, prompts
from app.services.audits import latest_audit, refresh_readiness
from app.services.billing import mission_quota
from app.services.llm import LLMCallInfo, LLMError, LLMService
from app.services.passport import proven_skill_ids

HISTORY_LIMIT = 20


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


async def recommendation_context(db: AsyncSession, user: User) -> tuple[set[int], str | None]:
    """Skill gap dari rencana aktif dan kode ISCO pengguna (dari profil atau audit terbaru)."""
    plan = await db.scalar(
        select(Plan).where(Plan.user_id == user.id, Plan.status == "active").order_by(Plan.created_at.desc()).limit(1)
    )
    proven = await proven_skill_ids(db, user.id)
    gap = {sid for sid in (plan.target_skill_ids if plan else [])} - proven
    isco = None
    audit = await latest_audit(db, user.id)
    if audit is not None:
        isco = audit.isco_code
    else:
        profile = await db.get(Profile, user.id)
        if profile and profile.occupation_code:
            occ = await db.get(Occupation, profile.occupation_code)
            isco = occ.isco_code if occ else None
    return gap, isco


def relevance(mission: Mission, gap: set[int], isco: str | None) -> tuple[float, str | None]:
    overlap = [s for s in mission.skills if s.id in gap]
    score = 2.0 * len(overlap)
    reason = None
    if overlap:
        reason = "Melatih skill di rencanamu: " + ", ".join(s.label for s in overlap[:3])
    if isco and isco in (mission.occupation_isco_codes or []):
        score += 1.0
        reason = reason or "Sesuai dengan jabatanmu"
    return score, reason


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
        name=persona.get("name", "Bu Rina"),
        role=persona.get("role", "atasanmu"),
        style=persona.get("style", "ramah, lugas, dan fokus pada hasil"),
        brief=mission.brief,
        data=json.dumps(mission.simulation_data or {}, ensure_ascii=False, indent=1),
        rules=prompts.GUARDRAIL_RULES,
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
            "Itu bagian kamu ya. Coba olah sendiri dari data simulasi yang ada. Kalau ada yang "
            "ambigu, sebutkan asumsimu di jawaban supaya saya bisa memahaminya."
        )
    if re.search(r"\b(format|bentuk|panjang|deadline|kapan)\b", message, re.IGNORECASE):
        return (
            "Ikuti saja yang diminta di brief, ringkas dan jelas. Yang penting angkanya bisa "
            "dipertanggungjawabkan dan rekomendasimu masuk akal."
        )
    return (
        "Terima kasih sudah bertanya. Semua data yang kamu perlukan ada di data simulasi. Fokus ke yang diminta di brief, lalu "
        "kumpulkan jawabanmu kalau sudah yakin."
    )


# ---------- Penilaian ----------


async def award_passport(db: AsyncSession, attempt: Attempt, mission: Mission) -> list[dict]:
    earned = []
    for skill in mission.skills:
        db.add(
            PassportEntry(
                user_id=attempt.user_id,
                skill_id=skill.id,
                attempt_id=attempt.id,
                mission_id=mission.id,
                score=attempt.score or 0,
            )
        )
        earned.append({"id": skill.id, "label": skill.label})
    return earned


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
    llm: LLMService,
    info: LLMCallInfo,
) -> dict:
    clean = grading.validate_answers(mission, answers)
    objective_score, objective = grading.objective_check(mission, clean)
    try:
        rubric = await grading.llm_rubric(llm, mission, clean, objective, info)
        engine = "llm"
    except LLMError:
        rubric = grading.heuristic_rubric(mission, clean)
        engine = "fallback"

    score = grading.combine(mission, objective_score, rubric["score"])
    now = utcnow()
    attempt.answers = clean
    attempt.draft = {}
    attempt.status = "graded"
    attempt.submitted_at = now
    attempt.objective_score = objective_score
    attempt.rubric_score = rubric["score"]
    attempt.score = score
    attempt.passed = score >= mission.pass_score
    attempt.engine = engine

    skills = await award_passport(db, attempt, mission) if attempt.passed else []

    # Penilaian cadangan selalu ditinjau; penilaian LLM disampel acak untuk kontrol mutu
    if engine == "fallback" or random.random() < get_settings().review_sample_rate:
        attempt.review_status = "sampled"
        db.add(Review(attempt_id=attempt.id, kind="sample", status="pending", original_score=score))

    attempt.result = {
        "objective": {"score": objective_score, "checks": objective},
        "rubric": rubric,
        "skills_earned": skills,
        "graded_at": now.isoformat(),
    }
    await db.flush()
    if attempt.passed:
        await refresh_readiness(db, attempt.user_id)
    return attempt.result


def result_view(attempt: Attempt, mission: Mission) -> dict:
    result = attempt.result or {}
    return {
        "attempt_id": attempt.id,
        "mission": {"id": mission.id, "title": mission.title},
        "status": attempt.status,
        "score": attempt.score,
        "passed": attempt.passed,
        "pass_score": mission.pass_score,
        "objective": result.get("objective"),
        "rubric": result.get("rubric"),
        "skills_earned": result.get("skills_earned", []),
        "engine": attempt.engine,
        "review_status": attempt.review_status,
        "submitted_at": attempt.submitted_at,
    }

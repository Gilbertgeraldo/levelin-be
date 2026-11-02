"""Audit tugas: cerita kerja -> daftar tugas -> padanan tugas ILO -> Peta Tugas.

Alur sama dengan prototipe: tugas yang dicentang dari template + tugas dari cerita. LLM hanya memecah
cerita dan memilih id tugas ILO dari daftar; skor selalu diambil dari indeks ILO oleh server. Bila LLM
gagal, cerita dipecah dengan aturan dan dicocokkan lewat kemiripan teks.
"""

import re
import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, not_found
from app.db.base import utcnow
from app.models import Attempt, Audit, AuditTask, Mission, Occupation, OccupationTask, ReadinessSnapshot, User
from app.services import prompts, scoring
from app.services.embeddings import EmbeddingService, cosine
from app.services.llm import LLMCallInfo, LLMError, LLMService
from app.services.textutil import fmt_num

MAX_TASKS = 8
Confidence = Literal["tinggi", "sedang", "rendah"]


class ExtractedTask(BaseModel):
    nama: str = Field(min_length=1)
    frekuensi: str = ""
    jam_per_minggu: float = scoring.DEFAULT_TASK_HOURS
    alat: list[str] = Field(default_factory=list)
    ilo_id: str | None = None
    keyakinan: Confidence = "rendah"
    alasan: str = ""

    @field_validator("nama")
    @classmethod
    def _trim_name(cls, v: str) -> str:
        return v.strip()[:90] or "Tugas tanpa nama"

    @field_validator("jam_per_minggu")
    @classmethod
    def _clamp_hours(cls, v: float) -> float:
        return max(0.5, min(40.0, round(float(v) * 2) / 2))


class Extraction(BaseModel):
    tasks: list[ExtractedTask]


# ---------- Daftar tugas ILO ----------


async def candidate_tasks(db: AsyncSession) -> list[OccupationTask]:
    """Tugas ILO yang boleh dicocokkan dengan cerita (semua jabatan, bukan ringkasan jabatan tujuan)."""
    rows = await db.scalars(
        select(OccupationTask)
        .where(OccupationTask.is_candidate.is_(True), OccupationTask.code.is_not(None))
        .order_by(OccupationTask.id)
    )
    return list(rows.all())


def candidate_list(tasks: list[OccupationTask], titles: dict[str, str]) -> str:
    return "\n".join(f"{t.code}: {t.description} ({titles.get(t.isco_code, t.isco_code)})" for t in tasks)


# ---------- Ekstraksi cadangan berbasis aturan ----------

_SPLIT = re.compile(r"[.!?\n]+")
_HOURS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:jam|j)\b", re.IGNORECASE)
_DURATION = re.compile(
    r"\s*,?\s*(?:sekitar|kira-kira|kurang lebih|selama)?\s*\d+(?:[.,]\d+)?\s*(?:jam|j)\b"
    r"(?:\s*(?:per|/|tiap|setiap)?\s*(?:hari|sehari|minggu|seminggu|bulan|sebulan)\b)?",
    re.IGNORECASE,
)
_LEADING = re.compile(
    r"^(saya|aku|sehari-hari|biasanya|setiap hari|tiap hari|juga|dan|lalu|kemudian|sering juga)\s+",
    re.IGNORECASE,
)


def parse_hours(text: str) -> tuple[str, float]:
    """Frekuensi dan jam per minggu dari satu kalimat; harian x5, bulanan /4."""
    freq = ""
    if re.search(r"\b(setiap hari|tiap hari|sehari|harian|per hari)\b", text, re.IGNORECASE):
        freq = "harian"
    elif re.search(r"\b(bulan|bulanan|sebulan)\b", text, re.IGNORECASE):
        freq = "bulanan"
    elif re.search(r"\b(minggu|mingguan|seminggu|senin|selasa|rabu|kamis|jumat)\b", text, re.IGNORECASE):
        freq = "mingguan"
    m = _HOURS.search(text)
    if not m:
        return freq, scoring.DEFAULT_TASK_HOURS
    value = float(m.group(1).replace(",", "."))
    if freq == "harian":
        value *= 5
    elif freq == "bulanan":
        value /= 4
    return freq, value


def rule_extract(story: str) -> list[ExtractedTask]:
    tasks: list[ExtractedTask] = []
    for raw in _SPLIT.split(story):
        part = raw.strip(" ,-")
        if len(part.split()) < 3:
            continue
        freq, hours = parse_hours(part)
        name = _DURATION.sub("", part).strip(" ,-")
        while (stripped := _LEADING.sub("", name)) != name:
            name = stripped
        tasks.append(
            ExtractedTask(
                nama=(name[:1].upper() + name[1:])[:90],
                frekuensi=freq,
                jam_per_minggu=hours,
                alat=["Excel"] if re.search(r"excel|spreadsheet", part, re.IGNORECASE) else [],
            )
        )
        if len(tasks) >= MAX_TASKS:
            break
    return tasks


async def match_to_templates(
    embeddings: EmbeddingService, descriptions: list[str], templates: list[OccupationTask]
) -> tuple[list[tuple[OccupationTask | None, float | None]], str]:
    """Cocokkan tiap deskripsi ke tugas ILO terdekat (kemiripan kosinus)."""
    if not descriptions:
        return [], embeddings.primary_model
    if not templates:
        return [(None, None)] * len(descriptions), embeddings.primary_model

    stored = templates[0].embedding_model
    has_stored = stored is not None and all(
        t.embedding is not None and t.embedding_model == stored for t in templates
    )
    query = await embeddings.embed(descriptions, "query", model=stored if has_stored else None)
    if has_stored and query.model == stored:
        template_vecs = [t.embedding for t in templates]
    else:
        template_vecs = (
            await embeddings.embed([t.description for t in templates], "document", model=query.model)
        ).vectors

    threshold = embeddings.min_similarity(query.model)
    results: list[tuple[OccupationTask | None, float | None]] = []
    for vec in query.vectors:
        sims = [cosine(vec, tv) for tv in template_vecs]
        best = max(range(len(sims)), key=sims.__getitem__)
        sim = round(sims[best], 3)
        results.append((templates[best], sim) if sim >= threshold else (None, sim))
    return results, query.model


def confidence_for(similarity: float | None, threshold: float) -> Confidence:
    if similarity is None or similarity < threshold + 0.1:
        return "rendah"
    return "tinggi" if similarity >= threshold + 0.2 else "sedang"


async def fallback_extract(
    embeddings: EmbeddingService, story: str, candidates: list[OccupationTask]
) -> list[ExtractedTask]:
    tasks = rule_extract(story)
    matches, model = await match_to_templates(embeddings, [t.nama for t in tasks], candidates)
    threshold = embeddings.min_similarity(model)
    for task, (template, sim) in zip(tasks, matches, strict=True):
        task.ilo_id = template.code if template else None
        task.keyakinan = confidence_for(sim, threshold) if template else "rendah"
        task.alasan = "kata kunci cocok dengan tugas ILO" if template else "tidak ada tugas ILO yang cocok"
    return tasks


# ---------- Skor & ringkasan ----------


async def passed_mission_kinds(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    rows = await db.scalars(
        select(Mission.kind)
        .join(Attempt, Attempt.mission_id == Mission.id)
        .where(Attempt.user_id == user_id, Attempt.passed.is_(True), Mission.kind.is_not(None))
        .distinct()
    )
    return set(rows.all())


def scored_tasks(audit: Audit) -> list[scoring.ScoredTask]:
    return [
        scoring.ScoredTask(
            hours=t.hours_per_week,
            group=t.group,
            uses_ai=t.uses_ai,
            kind=t.template.kind if t.template else None,
        )
        for t in audit.tasks
    ]


async def save_snapshot(db: AsyncSession, user_id: uuid.UUID, score: int) -> None:
    month = utcnow().strftime("%Y-%m")
    snapshot = await db.get(ReadinessSnapshot, (user_id, month))
    if snapshot is None:
        db.add(ReadinessSnapshot(user_id=user_id, month=month, score=score))
    else:
        snapshot.score = score


async def recompute(db: AsyncSession, audit: Audit) -> Audit:
    for task in audit.tasks:
        task.group = scoring.effective_group(task.ilo_score, task.manual_group)
    summary = scoring.summarize(scored_tasks(audit), await passed_mission_kinds(db, audit.user_id))
    matched = [t for t in audit.tasks if t.ilo_score is not None]
    matched_hours = sum(t.hours_per_week for t in matched)
    audit.summary = summary
    audit.total_hours = round(sum(t.hours_per_week for t in audit.tasks), 2)
    audit.exposure_index = (
        round(sum(t.hours_per_week * t.ilo_score for t in matched) / matched_hours, 3) if matched_hours else 0.0
    )
    audit.hours_saved_week = summary["hours_saved"]
    audit.readiness_score = summary["readiness_score"]
    await save_snapshot(db, audit.user_id, audit.readiness_score)
    return audit


async def refresh_readiness(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Hitung ulang audit terbaru setelah misi lulus/batal. Perubahan Attempt harus sudah di-flush."""
    audit = await latest_audit(db, user_id)
    if audit is not None:
        await recompute(db, audit)


# ---------- Pembuatan audit ----------


def _new_task(
    position: int,
    name: str,
    hours: float,
    template: OccupationTask | None,
    source: str,
    confidence: str,
) -> AuditTask:
    return AuditTask(
        position=position,
        description=name,
        hours_per_week=hours,
        uses_ai=False,
        template_task_id=template.id if template else None,
        template=template,
        ilo_score=template.ilo_score if template else None,
        source=source,
        confidence=confidence,
    )


async def create_audit(
    db: AsyncSession,
    user: User,
    *,
    occupation: Occupation,
    story: str | None,
    template_tasks: list[dict],
    llm: LLMService,
    embeddings: EmbeddingService,
    info: LLMCallInfo,
) -> Audit:
    candidates = await candidate_tasks(db)
    by_code = {t.code: t for t in candidates}
    titles = dict((await db.execute(select(Occupation.code, Occupation.title))).all())

    tasks: list[AuditTask] = []
    for item in template_tasks:
        template = by_code.get(item["ilo_task_id"])
        if template is None:
            raise ApiError(400, "invalid_template_task", f"Tugas ILO {item['ilo_task_id']} tidak dikenal.")
        tasks.append(_new_task(len(tasks), template.description, item["hours_per_week"], template, "template", "template"))

    engine = "llm"
    story = (story or "").strip()
    if story:
        checked = [item["ilo_task_id"] for item in template_tasks]
        # Lepas koneksi DB selama panggilan LLM yang bisa makan waktu puluhan detik
        await db.commit()
        try:
            extraction = await llm.generate_json(
                tier="main",
                system=prompts.EXTRACTION_SYSTEM,
                prompt=(
                    f"Jabatan pengguna: {occupation.title} (ISCO-08 {occupation.isco_code}).\n\n"
                    f"Daftar tugas standar (id: teks):\n{candidate_list(candidates, titles)}\n\n"
                    f"Tugas yang sudah dicentang pengguna dari template: {', '.join(checked) or 'tidak ada'}\n\n"
                    f"<cerita>\n{story[:3000]}\n</cerita>"
                ),
                schema=prompts.EXTRACTION_SCHEMA,
                model_cls=Extraction,
                max_tokens=6000,
                effort="low",
                info=info,
            )
            extracted = extraction.tasks[:MAX_TASKS]
            for task in extracted:
                if task.ilo_id not in by_code:
                    task.ilo_id = None
        except LLMError:
            extracted = await fallback_extract(embeddings, story, candidates)
            engine = "fallback"

        for found in extracted:
            template = by_code.get(found.ilo_id) if found.ilo_id else None
            duplicate = next((t for t in tasks if template and t.template_task_id == template.id), None)
            if duplicate is not None:
                duplicate.description = found.nama
                duplicate.source = "template+story"
                continue
            tasks.append(_new_task(len(tasks), found.nama, found.jam_per_minggu, template, "story", found.keyakinan))

    if not tasks:
        raise ApiError(400, "no_tasks", "Ceritakan pekerjaanmu atau pilih minimal satu tugas dari daftar.")

    audit = Audit(
        user_id=user.id,
        occupation_code=occupation.code,
        isco_code=occupation.isco_code,
        story=story or None,
        engine=engine,
        engine_detail={"extraction": "none" if not story else engine, "llm_model": info.model},
        tasks=tasks,
    )
    db.add(audit)
    await recompute(db, audit)
    await db.flush()
    return audit


async def get_owned_audit(db: AsyncSession, user: User, audit_id: uuid.UUID) -> Audit:
    audit = await db.get(Audit, audit_id)
    if audit is None or audit.user_id != user.id:
        raise not_found("Audit")
    return audit


async def latest_audit(db: AsyncSession, user_id: uuid.UUID) -> Audit | None:
    return await db.scalar(
        select(Audit).where(Audit.user_id == user_id).order_by(Audit.created_at.desc()).limit(1)
    )


def audit_view(audit: Audit, occupation: Occupation | None) -> dict:
    """Bentuk respons audit sesuai payload v0.1."""
    return {
        "id": audit.id,
        "occupation": {
            "code": audit.occupation_code,
            "name": occupation.title if occupation else audit.occupation_code,
        },
        "story": audit.story,
        "tasks": [
            {
                "id": t.id,
                "name": t.description,
                "hours_per_week": t.hours_per_week,
                "ilo_task_id": t.template.code if t.template else None,
                "ilo_text": t.template.description if t.template else None,
                "ilo_score": t.ilo_score,
                "group": t.group,
                "confidence": t.confidence,
                "source": t.source,
                "uses_ai": t.uses_ai,
            }
            for t in audit.tasks
        ],
        "summary": audit.summary,
        "engine": audit.engine,
        "created_at": audit.created_at,
        "updated_at": audit.updated_at,
    }


# ---------- Penjelasan peta ----------


def first_name(user: User) -> str:
    return (user.full_name or "").split()[0] if (user.full_name or "").strip() else "Kamu"


def explain_facts(audit: Audit, occupation: Occupation | None, user: User, first_mission: str | None) -> str:
    """Fakta untuk LLM penjelas, sama dengan prototipe. Tanpa teks cerita pengguna."""
    s = audit.summary or {}
    hb = s.get("hours_by_group") or {}
    lines = "\n".join(
        f"- {t.description}: {fmt_num(t.hours_per_week)} jam/minggu, kelompok {scoring.GROUP_LABELS[t.group]}"
        + (f", skor ILO {t.ilo_score}" if t.ilo_score is not None else "")
        for t in audit.tasks
    )
    facts = [
        f"Nama: {first_name(user)}",
        f"Jabatan: {occupation.title if occupation else audit.occupation_code}",
        f"Skor Kesiapan: {s.get('readiness_score', 0)}",
        f"Jam/minggu bisa dihemat: {fmt_num(s.get('hours_saved'))} jam",
        "Jam per kelompok: "
        f"Diotomasi {fmt_num(hb.get('auto'))}, Dibantu AI {fmt_num(hb.get('assist'))}, "
        f"Tetap Manusia {fmt_num(hb.get('human'))}",
        f"Tugas:\n{lines}",
    ]
    if first_mission:
        facts.append(f"Misi pertama yang disarankan: {first_mission}")
    return "Fakta:\n" + "\n".join(facts)


def explain_fallback(audit: Audit, user: User, first_mission: str | None) -> str:
    s = audit.summary or {}
    hb = s.get("hours_by_group") or {}
    total = sum(hb.get(g, 0) for g in scoring.GROUPS)
    autos = sorted((t for t in audit.tasks if t.group == "auto"), key=lambda t: t.hours_per_week, reverse=True)
    text = (
        f"{first_name(user)}, dari {fmt_num(total)} jam kerjamu per minggu, {fmt_num(hb.get('auto'))} jam ada di "
        f"tugas yang bisa dikerjakan AI hampir penuh dan {fmt_num(hb.get('assist'))} jam di tugas yang bisa "
        "dipercepat AI. "
    )
    if autos:
        text += f"Yang paling bergeser adalah “{autos[0].description}”. "
    text += (
        "Artinya pekerjaanmu berubah bentuk: kamu bisa menghemat sekitar "
        f"{fmt_num(s.get('hours_saved'))} jam per minggu dan memakainya untuk tugas yang lebih bernilai."
    )
    if first_mission:
        text += f" Mulai dari misi “{first_mission}” supaya Skor Kesiapanmu naik."
    return text

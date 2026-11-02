import json
import re
import uuid

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, not_found
from app.models import Audit, AuditTask, Occupation, OccupationTask, User
from app.services import prompts, scoring
from app.services.embeddings import HASHING_MODEL, EmbeddingService, cosine
from app.services.llm import LLMCallInfo, LLMError, LLMService
from app.services.passport import proven_skill_ids
from app.services.textutil import fmt_num

MAX_TASKS = 12


class ExtractedTask(BaseModel):
    description: str = Field(min_length=2)
    hours_per_week: float | None = None
    uses_ai: bool = False

    @field_validator("description")
    @classmethod
    def _trim(cls, v: str) -> str:
        return v.strip()[:300]

    @field_validator("hours_per_week")
    @classmethod
    def _clamp(cls, v: float | None) -> float | None:
        if v is None:
            return None
        return max(0.5, min(60.0, float(v)))


class Extraction(BaseModel):
    tasks: list[ExtractedTask]


# ---------- Ekstraksi cadangan berbasis aturan ----------

_SPLIT = re.compile(
    r"[.\n;]+|,\s*(?:lalu|kemudian|terus|dan juga|serta)\s+|\s+(?:lalu|kemudian|setelah itu|selain itu)\s+",
    re.IGNORECASE,
)
_HOURS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:jam|j)\b", re.IGNORECASE)
_AI_WORDS = re.compile(r"\b(ai|chatgpt|gpt|copilot|gemini|claude|bard|meta ai)\b", re.IGNORECASE)
_DURATION = re.compile(
    r"\s*(?:sekitar|kira-kira|kurang lebih|selama)?\s*\d+(?:[.,]\d+)?\s*(?:jam|j)\b"
    r"(?:\s*(?:per|/|tiap|setiap)?\s*(?:hari|sehari|minggu|seminggu)\b)?",
    re.IGNORECASE,
)
_LEADING = re.compile(
    r"^(saya|aku|sehari-hari|biasanya|setiap hari|tiap hari|juga|dan|lalu|kemudian)\s+", re.IGNORECASE
)


def rule_extract(story: str) -> list[ExtractedTask]:
    tasks: list[ExtractedTask] = []
    for raw in _SPLIT.split(story):
        part = (raw or "").strip(" ,-")
        if len(part.split()) < 3:
            continue
        hours = None
        if m := _HOURS.search(part):
            value = float(m.group(1).replace(",", "."))
            per_day = re.search(r"\b(hari|harian|sehari)\b", part, re.IGNORECASE)
            hours = value * 5 if per_day else value
        desc = _DURATION.sub("", part).strip(" ,-")
        while (stripped := _LEADING.sub("", desc)) != desc:
            desc = stripped
        desc = desc[:1].upper() + desc[1:]
        tasks.append(
            ExtractedTask(description=desc[:160], hours_per_week=hours, uses_ai=bool(_AI_WORDS.search(part)))
        )
        if len(tasks) >= MAX_TASKS:
            break
    return tasks


# ---------- Pencocokan ke template ILO ----------


async def match_to_templates(
    embeddings: EmbeddingService, descriptions: list[str], templates: list[OccupationTask]
) -> tuple[list[tuple[OccupationTask | None, float | None]], str]:
    """Cocokkan tiap deskripsi ke template tugas terdekat (kemiripan kosinus)."""
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


# ---------- Pembuatan & perhitungan ulang audit ----------


def fallback_score(occupation: Occupation, templates: list[OccupationTask]) -> float:
    if occupation.avg_exposure is not None:
        return occupation.avg_exposure
    if templates:
        return sum(t.ilo_score for t in templates) / len(templates)
    return scoring.MEDIUM_THRESHOLD


async def recompute(db: AsyncSession, audit: Audit) -> Audit:
    proven = len(await proven_skill_ids(db, audit.user_id))
    scored = [
        scoring.ScoredTask(hours=t.hours_per_week, score=t.ilo_score, uses_ai=t.uses_ai)
        for t in audit.tasks
    ]
    for task in audit.tasks:
        task.group = scoring.group_for(task.ilo_score)
    summary = scoring.summarize(scored, proven)
    audit.summary = summary
    audit.total_hours = summary["total_hours"]
    audit.exposure_index = summary["exposure_index"]
    audit.hours_saved_week = summary["hours_saved_week"]
    audit.readiness_score = summary["readiness_score"]
    return audit


async def create_audit(
    db: AsyncSession,
    user: User,
    *,
    occupation_code: str,
    story: str | None,
    selected: list[dict],
    llm: LLMService,
    embeddings: EmbeddingService,
    info: LLMCallInfo,
) -> Audit:
    occupation = await db.get(Occupation, occupation_code)
    if occupation is None:
        raise not_found("Jabatan")
    templates = list(
        (
            await db.scalars(
                select(OccupationTask)
                .where(OccupationTask.isco_code == occupation.isco_code)
                .order_by(OccupationTask.id)
            )
        ).all()
    )
    by_id = {t.id: t for t in templates}
    # Lepas koneksi DB selama panggilan LLM yang bisa makan waktu puluhan detik
    await db.commit()

    # item: description, hours, uses_ai, source, template, similarity
    items: list[dict] = []
    for sel in selected:
        template = None
        if sel.get("template_task_id") is not None:
            template = by_id.get(sel["template_task_id"])
            if template is None:
                raise ApiError(400, "invalid_template_task", "Template tugas tidak cocok dengan jabatan ini.")
        description = (sel.get("description") or (template.description if template else "")).strip()
        if not description:
            raise ApiError(400, "missing_description", "Tugas manual wajib punya deskripsi.")
        items.append(
            {
                "description": description,
                "hours": sel.get("hours_per_week"),
                "uses_ai": bool(sel.get("uses_ai")),
                "source": "template" if template else "manual",
                "template": template,
                "similarity": 1.0 if template else None,
            }
        )

    extraction_engine = "none"
    if story and story.strip():
        try:
            extraction = await llm.generate_json(
                tier="main",
                system=prompts.EXTRACTION_SYSTEM,
                prompt=(
                    f"Jabatan: {occupation.title} (ISCO {occupation.isco_code}).\n\n"
                    f"<cerita>\n{story.strip()}\n</cerita>"
                ),
                schema=prompts.EXTRACTION_SCHEMA,
                model_cls=Extraction,
                max_tokens=6000,
                effort="low",
                info=info,
            )
            extracted = extraction.tasks[:MAX_TASKS]
            extraction_engine = "llm"
        except LLMError:
            extracted = rule_extract(story)
            extraction_engine = "rules"
        for task in extracted:
            items.append(
                {
                    "description": task.description,
                    "hours": task.hours_per_week,
                    "uses_ai": task.uses_ai,
                    "source": "story",
                    "template": None,
                    "similarity": None,
                }
            )

    if not items:
        raise ApiError(
            400, "no_tasks", "Ceritakan pekerjaanmu atau pilih minimal satu tugas dari daftar."
        )

    to_match = [i for i in items if i["template"] is None]
    matches, match_model = await match_to_templates(
        embeddings, [i["description"] for i in to_match], templates
    )
    for item, (template, sim) in zip(to_match, matches, strict=True):
        item["template"], item["similarity"] = template, sim

    # Tugas cerita yang cocok dengan template yang sudah dipilih eksplisit tidak dihitung dua kali
    explicit = {i["template"].id: i for i in items if i["source"] == "template"}
    merged: list[dict] = []
    for item in items:
        tpl = item["template"]
        if item["source"] == "story" and tpl is not None and tpl.id in explicit:
            target = explicit[tpl.id]
            if target["hours"] is None:
                target["hours"] = item["hours"]
            target["uses_ai"] = target["uses_ai"] or item["uses_ai"]
            continue
        merged.append(item)

    hours = scoring.fill_missing_hours([i["hours"] for i in merged])
    default_score = fallback_score(occupation, templates)

    matching_engine = match_model if to_match else "none"
    degraded = extraction_engine == "rules" or matching_engine == HASHING_MODEL
    audit = Audit(
        user_id=user.id,
        occupation_code=occupation.code,
        isco_code=occupation.isco_code,
        story=story,
        engine="fallback" if degraded else "llm",
        engine_detail={
            "extraction": extraction_engine,
            "matching": matching_engine,
            "llm_model": info.model,
        },
    )
    audit.tasks = [
        AuditTask(
            position=pos,
            description=item["description"],
            hours_per_week=h,
            uses_ai=item["uses_ai"],
            template_task_id=item["template"].id if item["template"] else None,
            match_similarity=item["similarity"],
            ilo_score=item["template"].ilo_score if item["template"] else default_score,
            source=item["source"],
        )
        for pos, (item, h) in enumerate(zip(merged, hours, strict=True))
    ]
    db.add(audit)
    await recompute(db, audit)
    await db.flush()
    return audit


async def refresh_readiness(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Hitung ulang audit terbaru setelah skill terbukti berubah. Perubahan Paspor harus sudah di-flush."""
    audit = await latest_audit(db, user_id)
    if audit is not None:
        await recompute(db, audit)


async def get_owned_audit(db: AsyncSession, user: User, audit_id: uuid.UUID) -> Audit:
    audit = await db.get(Audit, audit_id)
    if audit is None or audit.user_id != user.id:
        raise not_found("Audit")
    return audit


async def latest_audit(db: AsyncSession, user_id: uuid.UUID) -> Audit | None:
    return await db.scalar(
        select(Audit).where(Audit.user_id == user_id).order_by(Audit.created_at.desc()).limit(1)
    )


def audit_numbers(audit: Audit, occupation: Occupation | None) -> dict:
    """Ringkasan angka audit untuk LLM penjelas (tanpa teks cerita pengguna)."""
    s = audit.summary or {}
    return {
        "jabatan": occupation.title if occupation else audit.occupation_code,
        "skor_kesiapan": s.get("readiness_score"),
        "total_jam_per_minggu": s.get("total_hours"),
        "indeks_paparan_ai": s.get("exposure_index"),
        "jam_bisa_dihemat_per_minggu": s.get("hours_saved_week"),
        "jam_sudah_dihemat_per_minggu": s.get("hours_saved_now"),
        "porsi_adopsi_ai": s.get("ai_adoption"),
        "kelompok": {
            k: {"label": v["label"], "jumlah_tugas": v["task_count"], "jam": v["hours"]}
            for k, v in (s.get("groups") or {}).items()
        },
        "tugas": [
            {
                "tugas": t.description,
                "jam_per_minggu": t.hours_per_week,
                "skor_ilo": round(t.ilo_score, 2),
                "kelompok": scoring.GROUP_LABELS[t.group],
                "sudah_pakai_ai": t.uses_ai,
            }
            for t in audit.tasks
        ],
    }


def explain_fallback(audit: Audit) -> str:
    s = audit.summary or {}
    groups = s.get("groups") or {}
    tasks = sorted(audit.tasks, key=lambda t: t.ilo_score, reverse=True)
    high = [t for t in tasks if t.group == "high"] or [t for t in tasks if t.group == "medium"]
    low = [t for t in reversed(tasks) if t.group == "low"]
    lines = [
        f"Skor Kesiapan kamu {s.get('readiness_score', 0)} dari 100. Skor ini naik kalau kamu "
        "makin sering memakai AI di tugas yang cocok dan menambah skill terbukti di Paspor."
    ]
    if high:
        lines.append(
            f"Tugas yang paling bisa dibantu AI adalah \"{high[0].description}\". "
            f"Kalau tugas-tugas di kelompok ini dibantu AI, kamu berpotensi menghemat sekitar "
            f"{fmt_num(s.get('hours_saved_week'))} jam per minggu."
        )
    if low:
        lines.append(
            f"Sekitar {fmt_num(groups.get('low', {}).get('hours'))} jam per minggu ada di tugas yang tetap "
            f"mengandalkan keunggulan manusia, seperti \"{low[0].description}\". Itu modal pentingmu."
        )
    lines.append(
        "Langkah berikutnya: pilih satu tugas yang bisa dibantu AI, coba kerjakan dengan AI minggu "
        "ini, lalu tandai \"sudah pakai AI\" di peta supaya skormu diperbarui."
    )
    return "\n\n".join(lines)


def numbers_prompt(data: dict) -> str:
    return "Data audit:\n```json\n" + json.dumps(data, ensure_ascii=False, indent=1) + "\n```"

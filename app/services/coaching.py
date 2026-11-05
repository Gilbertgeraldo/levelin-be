"""Layanan AI Skill Coach, Evaluasi Latihan Mikro (Micro-Drill), dan Generator Misi Korporat (SOP-to-Mission).

Setiap fungsi memiliki jalur utama LLM terstruktur (generate_json) dan jalur cadangan (fallback)
deterministik berbasis konteks jabatan, kategori skill ESCO, dan aturan SOP perusahaan.
"""

import re
import uuid
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CustomMissionRequest, Mission, Occupation, Profile, Skill, User, mission_skills
from app.schemas.admin import MissionGenerateIn
from app.schemas.reference import SkillDrillCheckIn
from app.services.llm import LLMCallInfo, LLMError
from app.services.missions import visible_filter

# ---------- Skema & Prompt AI Skill Coach ----------

COACH_SYSTEM = """\
Kamu adalah AI Skill Coach profesional di platform pengembangan kompetensi perusahaan Levelin.
Tugasmu menyusun modul belajar singkat (3 menit), rumus prompt AI kerja, dan 1 studi kasus latihan mikro \
untuk membantu karyawan menguasai skill yang ditargetkan dalam pekerjaan sehari-hari.

Aturan wajib:
- Gunakan bahasa Indonesia profesional, praktis, dan membumi untuk konteks tempat kerja di perusahaan.
- Jangan menyinggung PHK atau ancaman kehilangan pekerjaan; tekankan bagaimana AI mempercepat dan meningkatkan mutu kerja.
- "overview": 2-3 kalimat ringkas mengapa skill ini krusial di kantor dan bagaimana AI membantu.
- "key_steps": tepat 3 langkah konkret menerapkan skill ini pada tugas nyata.
- "prompt_templates": tepat 2 rumus prompt AI siap salin yang bisa dipakai karyawan saat bekerja.
- "micro_drill": 1 skenario kasus kantor singkat (2-3 kalimat), 1 pertanyaan tindakan konkret, 3 kriteria evaluasi, dan 1 petunjuk singkat."""

COACH_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "key_steps": {"type": "array", "items": {"type": "string"}},
        "prompt_templates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "when_to_use": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["title", "when_to_use", "prompt"],
                "additionalProperties": False,
            },
        },
        "micro_drill": {
            "type": "object",
            "properties": {
                "scenario": {"type": "string"},
                "question": {"type": "string"},
                "evaluation_criteria": {"type": "array", "items": {"type": "string"}},
                "hint": {"type": "string"},
            },
            "required": ["scenario", "question", "evaluation_criteria", "hint"],
            "additionalProperties": False,
        },
    },
    "required": ["overview", "key_steps", "prompt_templates", "micro_drill"],
    "additionalProperties": False,
}


class _PromptTemplateLLM(BaseModel):
    title: str
    when_to_use: str
    prompt: str


class _MicroDrillLLM(BaseModel):
    scenario: str
    question: str
    evaluation_criteria: list[str]
    hint: str


class _CoachLLM(BaseModel):
    overview: str
    key_steps: list[str]
    prompt_templates: list[_PromptTemplateLLM]
    micro_drill: _MicroDrillLLM


# ---------- Skema & Prompt Evaluasi Micro-Drill ----------

DRILL_SYSTEM = """\
Kamu adalah Evaluator Pelatihan Kompetensi di Levelin. Nilai jawaban latihan mikro karyawan terhadap \
skenario dan pertanyaan skill yang diberikan.

Aturan wajib:
- Berikan "score" (0-100): >= 70 berarti karyawan sudah siap mengambil ujian Misi Pembuktian ("ready_for_mission": true).
- "feedback": 2-3 kalimat umpan balik yang membangun dan spesifik merujuk isi jawaban karyawan.
- "strengths": 1-3 poin kekuatan jawaban karyawan.
- "improvements": 1-3 poin saran perbaikan konkret agar jawaban lebih tajam dan terukur.
- "model_answer": contoh jawaban ideal (2-4 kalimat) yang memenuhi seluruh standar perusahaan."""

DRILL_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "ready_for_mission": {"type": "boolean"},
        "feedback": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "improvements": {"type": "array", "items": {"type": "string"}},
        "model_answer": {"type": "string"},
    },
    "required": ["score", "ready_for_mission", "feedback", "strengths", "improvements", "model_answer"],
    "additionalProperties": False,
}


class _DrillCheckLLM(BaseModel):
    score: int
    ready_for_mission: bool
    feedback: str
    strengths: list[str]
    improvements: list[str]
    model_answer: str


# ---------- Skema & Prompt Generator Misi SOP Korporat ----------

MISSION_GEN_SYSTEM = """\
Kamu adalah Arsitek Simulasi Pelatihan Korporat di Levelin. Tugasmu mengubah judul dan deskripsi SOP / \
kebutuhan pelatihan perusahaan menjadi draf Misi Simulasi Kerja yang lengkap dan siap diujikan.

Aturan wajib:
- Bahasa Indonesia profesional sesuai budaya kerja korporat.
- "summary": 1 kalimat ringkasan misi.
- "brief": 2-3 paragraf skenario kerja nyata yang meminta peserta menganalisis data dan mengambil keputusan sesuai SOP.
- "boss_name", "boss_role", "boss_style": identitas atasan AI dalam simulasi.
- "table_headers": 3-4 nama kolom tabel simulasi.
- "table_rows": 3-4 baris data tabel (setiap baris memiliki jumlah sel yang sama dengan table_headers).
- "notes": 2-3 catatan kebijakan/SOP penting yang harus dipatuhi peserta.
- "expected_keyword": 1 kata kunci penting dari data/SOP untuk pemeriksaan objektif otomatis."""

MISSION_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "brief": {"type": "string"},
        "boss_name": {"type": "string"},
        "boss_role": {"type": "string"},
        "boss_style": {"type": "string"},
        "table_headers": {"type": "array", "items": {"type": "string"}},
        "table_rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        "notes": {"type": "array", "items": {"type": "string"}},
        "expected_keyword": {"type": "string"},
    },
    "required": [
        "summary",
        "brief",
        "boss_name",
        "boss_role",
        "boss_style",
        "table_headers",
        "table_rows",
        "notes",
        "expected_keyword",
    ],
    "additionalProperties": False,
}


class _MissionGenLLM(BaseModel):
    summary: str
    brief: str
    boss_name: str
    boss_role: str
    boss_style: str
    table_headers: list[str]
    table_rows: list[list[str]]
    notes: list[str]
    expected_keyword: str


# ---------- Implementasi AI Skill Coach ----------


async def _user_role_context(db: AsyncSession, user: User) -> tuple[str, str]:
    profile = await db.get(Profile, user.id)
    if profile is None:
        return "Staf Profesional", "Perusahaan"
    occ = await db.get(Occupation, profile.occupation_code) if profile.occupation_code else None
    role_title = (profile.job_title or (occ.title if occ else None) or "Staf Profesional").strip()
    industry = (profile.industry or "Operasional Perusahaan").strip()
    return role_title, industry


async def _related_missions_for_skill(db: AsyncSession, user: User, skill_id: int) -> list[dict]:
    stmt = (
        select(Mission)
        .join(mission_skills, mission_skills.c.mission_id == Mission.id)
        .where(mission_skills.c.skill_id == skill_id, *visible_filter(user))
        .order_by(Mission.difficulty, Mission.title)
        .limit(3)
    )
    rows = (await db.scalars(stmt)).all()
    if not rows:
        # Jika belum ada misi spesifik untuk skill_id ini, tampilkan misi utama yang tersedia
        fallback_stmt = select(Mission).where(*visible_filter(user)).order_by(Mission.title).limit(2)
        rows = (await db.scalars(fallback_stmt)).all()
    return [
        {
            "id": m.id,
            "slug": m.slug,
            "title": m.title,
            "est_minutes": m.est_minutes,
        }
        for m in rows
    ]


def _coach_fallback(skill_label: str, category: str, role_title: str, industry: str) -> dict:
    return {
        "overview": (
            f"Kompetensi “{skill_label}” membantu {role_title} di bidang {industry} mengambil keputusan "
            f"lebih akurat dan terstruktur. Dengan menggabungkan pemahaman standar operasional (SOP) dan "
            f"bantuan AI secara kritis, kamu dapat menyelesaikan pekerjaan lebih cepat sekaligus meminimalkan risiko kesalahan."
        ),
        "key_steps": [
            f"Identifikasi data utama, batasan SOP, dan target hasil sebelum menerapkan {skill_label}.",
            f"Gunakan AI untuk menyusun draf awal atau memetakan anomali data terkait {skill_label}, lalu verifikasi angkanya secara mandiri.",
            "Sajikan temuan beserta rekomendasi tindakan yang jelas, terukur, dan mencantumkan mitigasi risiko bagi tim/atasan.",
        ],
        "prompt_templates": [
            {
                "title": f"Pemetaan Masalah & Prioritas ({skill_label})",
                "when_to_use": "Saat menerima data mentah atau laporan kasus baru dari atasan/klien.",
                "prompt": (
                    f"Saya bekerja sebagai {role_title} di bidang {industry}. Bantu saya menerapkan prinsip "
                    f"'{skill_label}' untuk menganalisis data berikut: [Tempel Data/Kasus]. "
                    f"Tolong kelompokkan menjadi: (1) Temuan Kritis, (2) Akar Masalah, dan (3) 3 Opsi Solusi beserta risikonya."
                ),
            },
            {
                "title": f"Audit Kualitas & Kepatuhan SOP ({skill_label})",
                "when_to_use": "Sebelum mengirimkan draf laporan atau rekomendasi keputusan ke manajemen.",
                "prompt": (
                    f"Periksa draf rekomendasi saya berikut dari sudut pandang '{skill_label}': [Tempel Draf]. "
                    f"Tunjukkan bagian mana yang datanya masih kurang kuat, asumsi mana yang perlu diperjelas, "
                    f"dan bagaimana memperbaikinya agar ringkas serta siap dieksekusi."
                ),
            },
        ],
        "micro_drill": {
            "scenario": (
                f"Sebagai {role_title} di {industry}, kamu menemukan selisih data sebesar 14% antara laporan "
                f"sistem otomatis dan catatan aktual lapangan menjelang rapat evaluasi mingguan. Atasan meminta "
                f"kamu menerapkan kemampuan {skill_label} dalam 15 menit untuk memberikan rekomendasi tindak lanjut."
            ),
            "question": (
                f"Tuliskan 2–4 kalimat langkah konkret yang akan kamu ambil menggunakan pendekatan {skill_label}: "
                f"data apa yang kamu verifikasi lebih dulu, bagaimana kamu memanfaatkan AI tanpa membocorkan data rahasia, "
                f"dan rekomendasi apa yang kamu sampaikan ke atasan?"
            ),
            "evaluation_criteria": [
                "Menyebutkan langkah verifikasi silang antara sumber data dan standar SOP.",
                "Menjelaskan pemanfaatan AI secara aman dan pemeriksaan ulang hasil (human-in-the-loop).",
                "Memberikan rekomendasi tindakan yang spesifik, terukur, dan berorientasi solusi.",
            ],
            "hint": "Pastikan jawabanmu memuat: (1) apa yang dicek, (2) bagaimana memvalidasi data, dan (3) usulan solusi konkret ke atasan.",
        },
    }


async def generate_skill_coach(
    db: AsyncSession, user: User, skill: Skill, llm, info: LLMCallInfo
) -> dict:
    skill_label = skill.label_id or skill.label
    role_title, industry = await _user_role_context(db, user)
    related_missions = await _related_missions_for_skill(db, user, skill.id)

    prompt = (
        f"<konteks>\n"
        f"Skill: {skill_label} (Kategori: {skill.category})\n"
        f"Jabatan Karyawan: {role_title}\n"
        f"Industri/Bidang: {industry}\n"
        f"</konteks>\n"
        f"Susun modul belajar praktis, 2 template prompt AI kerja, dan 1 latihan mikro untuk skill tersebut."
    )

    data: dict | None = None
    engine = "fallback"
    if llm.available:
        try:
            res = await llm.generate_json(
                tier="light",
                system=COACH_SYSTEM,
                prompt=prompt,
                schema=COACH_SCHEMA,
                model_cls=_CoachLLM,
                max_tokens=2200,
                effort="low",
                info=info,
            )
            data = res.model_dump()
            engine = "llm"
        except LLMError:
            data = None

    if data is None:
        data = _coach_fallback(skill_label, skill.category, role_title, industry)

    return {
        "skill": {"id": skill.id, "code": skill.code, "label": skill_label},
        "category": skill.category,
        "engine": engine,
        "overview": data["overview"],
        "key_steps": data["key_steps"][:4],
        "prompt_templates": data["prompt_templates"][:3],
        "micro_drill": data["micro_drill"],
        "related_missions": related_missions,
    }


# ---------- Implementasi Evaluasi Micro-Drill ----------


def _drill_fallback_eval(skill_label: str, body: SkillDrillCheckIn) -> dict:
    answer = body.answer.strip()
    words = re.findall(r"\w+", answer.lower())
    word_count = len(words)

    has_verification = bool(
        re.search(r"\b(cek|periksa|verifikasi|validasi|bandingkan|audit|data|bukti|sop|standar)\b", answer, re.I)
    )
    has_action = bool(
        re.search(
            r"\b(rekomendasi|solusi|langkah|tindak|lapor|saran|prioritas|perbaiki|usulan|keputusan|mitigasi)\b",
            answer,
            re.I,
        )
    )
    has_ai_check = bool(
        re.search(r"\b(ai|prompt|draf|ringkas|klasifikasi|pola|otomatis|koreksi|review)\b", answer, re.I)
    )

    score = 45
    if word_count >= 18:
        score += 15
    if word_count >= 35:
        score += 10
    if has_verification:
        score += 12
    if has_action:
        score += 12
    if has_ai_check:
        score += 6
    score = max(25, min(96, score))
    ready = score >= 70

    strengths: list[str] = []
    improvements: list[str] = []

    if has_verification:
        strengths.append("Sudah menekankan langkah verifikasi data/bukti sebelum mengambil kesimpulan.")
    else:
        improvements.append("Tambahkan langkah verifikasi silang terhadap sumber data utama atau SOP perusahaan.")

    if has_action:
        strengths.append("Memuat rencana tindakan atau rekomendasi solusi yang jelas kepada pemangku kepentingan.")
    else:
        improvements.append("Sebutkan rekomendasi tindakan konkret beserta prioritas eksekusinya.")

    if has_ai_check:
        strengths.append("Menunjukkan kesadaran kolaborasi dengan AI secara terarah (human-in-the-loop).")
    else:
        improvements.append("Jelaskan secara singkat bagaimana kamu memanfaatkan AI untuk mempercepat pemetaan awal lalu memvalidasi hasilnya.")

    if word_count < 20:
        improvements.append("Uraikan jawaban sedikit lebih detail (minimal 2–3 kalimat utuh) agar konteks keputusan terlihat jelas.")
    elif not strengths:
        strengths.append("Jawaban sudah terstruktur dan menjawab inti permasalahan pada skenario.")

    if not improvements:
        improvements.append("Tambahkan indikator keberhasilan (KPI/target waktu) agar rekomendasi semakin siap dieksekusi manajemen.")

    feedback = (
        f"Latihan yang sangat baik untuk kompetensi “{skill_label}”! Jawabanmu sudah mencakup fondasi analisis dan siap diuji pada simulasi Misi sesungguhnya."
        if ready
        else f"Langkah awal yang baik untuk melatih “{skill_label}”. Lengkapi jawabanmu dengan verifikasi data dan rekomendasi yang lebih spesifik agar mencapai standar lulus."
    )

    model_answer = (
        f"Pertama, saya melakukan verifikasi silang antara data laporan dan bukti transaksi utama sesuai SOP "
        f"untuk mengisolasi sumber selisih. Kedua, saya menggunakan AI dengan data yang telah dianonimkan untuk "
        f"mengelompokkan pola anomali dan menyusun draf opsi mitigasi, lalu memeriksa kembali akurasi angkanya. "
        f"Ketiga, saya menyampaikan laporan ringkas kepada atasan yang memuat akar masalah, dampak operasional, "
        f"serta 2 opsi solusi prioritas beserta tenggat waktu perbaikannya."
    )

    return {
        "score": score,
        "ready_for_mission": ready,
        "feedback": feedback,
        "strengths": strengths,
        "improvements": improvements,
        "model_answer": model_answer,
    }


async def evaluate_skill_drill(
    db: AsyncSession, user: User, skill: Skill, body: SkillDrillCheckIn, llm, info: LLMCallInfo
) -> dict:
    skill_label = skill.label_id or skill.label
    prompt = (
        f"<latihan>\n"
        f"Skill: {skill_label}\n"
        f"Skenario: {body.scenario}\n"
        f"Pertanyaan: {body.question}\n"
        f"Jawaban Karyawan: {body.answer}\n"
        f"</latihan>\n"
        f"Evaluasi jawaban karyawan secara objektif dan berikan contoh jawaban ideal."
    )

    data: dict | None = None
    engine = "fallback"
    if llm.available:
        try:
            res = await llm.generate_json(
                tier="light",
                system=DRILL_SYSTEM,
                prompt=prompt,
                schema=DRILL_SCHEMA,
                model_cls=_DrillCheckLLM,
                max_tokens=1800,
                effort="low",
                info=info,
            )
            data = res.model_dump()
            data["score"] = max(0, min(100, int(data["score"])))
            data["ready_for_mission"] = data["score"] >= 70
            engine = "llm"
        except LLMError:
            data = None

    if data is None:
        data = _drill_fallback_eval(skill_label, body)

    return {
        "skill_id": skill.id,
        "score": data["score"],
        "ready_for_mission": data["ready_for_mission"],
        "feedback": data["feedback"],
        "strengths": data["strengths"][:3],
        "improvements": data["improvements"][:3],
        "model_answer": data["model_answer"],
        "engine": engine,
    }


# ---------- Implementasi AI Corporate SOP-to-Mission Generator ----------


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug[:72].rstrip("-") or "misi-pelatihan-korporat")


async def _match_skills_for_mission(
    db: AsyncSession, title: str, description: str, target_role: str | None
) -> list[Skill]:
    tokens = [
        t
        for t in re.findall(r"[a-zA-Z0-9]{4,}", f"{title} {description} {target_role or ''}".lower())
        if t not in {"misi", "untuk", "dengan", "pada", "dari", "yang", "dalam", "perusahaan", "karyawan"}
    ]
    all_skills = list((await db.scalars(select(Skill).order_by(Skill.id).limit(250))).all())
    if not all_skills:
        return []

    scored: list[tuple[int, Skill]] = []
    for s in all_skills:
        haystack = f"{s.label_id or ''} {s.label or ''} {s.label_en or ''} {s.category or ''}".lower()
        hits = sum(2 if tok in haystack else 0 for tok in tokens[:15])
        scored.append((hits, s))
    scored.sort(key=lambda item: (-item[0], item[1].id))
    chosen = [s for hits, s in scored[:3] if hits > 0]
    if not chosen:
        chosen = [s for _, s in scored[:2]]
    return chosen


def _mission_gen_fallback(title: str, description: str, target_role: str) -> dict:
    desc_clean = description.strip() or (
        f"Standar Operasional Prosedur (SOP) terkait {title} untuk memastikan ketepatan data, "
        f"kepatuhan prosedur internal, dan kecepatan respons layanan."
    )
    return {
        "summary": f"Simulasi studi kasus {title} bagi {target_role} berbasis standar operasional perusahaan.",
        "brief": (
            f"Tim {target_role} sedang menghadapi situasi operasional penting terkait **{title}**. "
            f"Berdasarkan pedoman internal perusahaan: {desc_clean}\n\n"
            f"Perhatikan tabel data simulasi di bawah ini. Terdapat temuan selisih dan antrean prioritas yang "
            f"membutuhkan keputusan cepat sebelum laporan diserahkan ke manajemen pukul 16.00. "
            f"Susun analisis temuan utama beserta rencana tindakan korektif yang mematuhi SOP perusahaan."
        ),
        "boss_name": "Pak Hendra Wijaya",
        "boss_role": f"Kepala Divisi ({target_role})",
        "boss_style": "tegas, fokus pada kepatuhan SOP, ketepatan angka, dan solusi yang langsung bisa dijalankan",
        "table_headers": ["Kode Unit / Kasus", "Indikator Utama", "Status Saat Ini", "Catatan SOP"],
        "table_rows": [
            ["KASUS-01", "Selisih rekonsiliasi 12% dari ambang batas", "Perlu Verifikasi Segera", "Wajib cek bukti primer maksimal 2 jam"],
            ["KASUS-02", "Dokumen pendukung belum lengkap (2 lampiran)", "Tertunda di Antrean", "Konfirmasi ke unit terkait sebelum persetujuan"],
            ["KASUS-03", "Permintaan eskalasi prioritas dari klien utama", "Menunggu Keputusan", "Sertakan mitigasi risiko dan jadwal tindak lanjut"],
        ],
        "notes": [
            f"Konteks SOP: {desc_clean[:240]}",
            "Setiap rekomendasi wajib mencantumkan dasar verifikasi data dan penanggung jawab tindak lanjut.",
            "Hindari persetujuan langsung pada kasus yang melampaui toleransi SOP tanpa eskalasi tertulis.",
        ],
        "expected_keyword": "verifikasi",
    }


async def generate_mission_draft(
    db: AsyncSession, body: MissionGenerateIn, llm, info: LLMCallInfo
) -> dict:
    title = body.title.strip()
    description = body.description.strip()
    target_role = (body.target_role or "").strip()

    if body.custom_request_id is not None:
        req = await db.get(CustomMissionRequest, body.custom_request_id)
        if req is not None:
            if not title:
                title = req.title
            if not description and req.description:
                description = req.description
            if not target_role and req.target_role:
                target_role = req.target_role

    target_role = target_role or "Staf Operasional & Administrasi"
    matched_skills = await _match_skills_for_mission(db, title, description, target_role)

    prompt = (
        f"<sop_perusahaan>\n"
        f"Judul Pelatihan/SOP: {title}\n"
        f"Target Peran: {target_role}\n"
        f"Deskripsi SOP / Kasus: {description or 'Susun simulasi kerja standar korporat yang realistis.'}\n"
        f"</sop_perusahaan>\n"
        f"Buat skenario simulasi misi pelatihan kerja korporat lengkap dengan tabel data dan catatan SOP."
    )

    gen: dict | None = None
    engine = "fallback"
    if llm.available:
        try:
            res = await llm.generate_json(
                tier="main",
                system=MISSION_GEN_SYSTEM,
                prompt=prompt,
                schema=MISSION_GEN_SCHEMA,
                model_cls=_MissionGenLLM,
                max_tokens=3000,
                effort="low",
                info=info,
            )
            gen = res.model_dump()
            engine = "llm"
        except LLMError:
            gen = None

    if gen is None:
        gen = _mission_gen_fallback(title, description, target_role)

    headers = gen["table_headers"] or ["Item", "Data", "Status", "Catatan"]
    col_count = len(headers)
    normalized_rows: list[list[str]] = []
    for row in gen["table_rows"][:6]:
        if len(row) < col_count:
            row = list(row) + ["-"] * (col_count - len(row))
        normalized_rows.append([str(c) for c in row[:col_count]])
    if not normalized_rows:
        normalized_rows = [["KASUS-01", "Data memerlukan verifikasi SOP", "Prioritas Tinggi", "Sesuai prosedur internal"]]

    base_slug = _slugify(title)
    existing = await db.scalar(select(Mission.id).where(Mission.slug == base_slug))
    slug = f"{base_slug}-{uuid.uuid4().hex[:4]}" if existing else base_slug

    keyword = (gen.get("expected_keyword") or "verifikasi").strip().lower() or "verifikasi"

    answer_fields = [
        {
            "id": "analisis_temuan",
            "label": "1. Analisis Temuan & Prioritas Masalah",
            "type": "long_text",
            "required": True,
            "placeholder": "Jelaskan anomali/masalah utama dari tabel simulasi dan mana yang harus ditangani lebih dulu sesuai SOP...",
            "rows": 4,
        },
        {
            "id": "rencana_tindakan",
            "label": "2. Rencana Tindakan Korektif & Mitigasi Risiko",
            "type": "long_text",
            "required": True,
            "placeholder": "Uraikan langkah konkret penyelesaian, pihak yang dilibatkan, serta cara mencegah masalah berulang...",
            "rows": 5,
        },
    ]

    rubric = [
        {
            "id": "ketepatan_analisis",
            "criterion": "Ketepatan identifikasi masalah dan prioritas data simulasi",
            "description": "Peserta mampu memetakan kasus paling kritis beserta angka/fakta dari tabel simulasi.",
            "weight": 0.4,
            "keywords": ["prioritas", "data", "selisih", keyword],
        },
        {
            "id": "kepatuhan_sop",
            "criterion": "Kepatuhan terhadap prosedur operasional standar (SOP)",
            "description": "Langkah verifikasi dan eskalasi sesuai aturan perusahaan tanpa melompati prosedur kontrol.",
            "weight": 0.3,
            "keywords": ["sop", "verifikasi", "prosedur", "bukti", "konfirmasi"],
        },
        {
            "id": "kejelasan_solusi",
            "criterion": "Kejelasan rekomendasi tindakan dan mitigasi risiko",
            "description": "Rencana tindak lanjut spesifik, realistis, dan mencantumkan langkah pencegahan.",
            "weight": 0.3,
            "keywords": ["solusi", "tindak lanjut", "mitigasi", "risiko", "jadwal"],
        },
    ]

    answer_key = [
        {
            "id": "cek_kata_kunci_sop",
            "field_id": "analisis_temuan",
            "field_ids": ["analisis_temuan", "rencana_tindakan"],
            "type": "contains",
            "expected": keyword,
            "weight": 1.0,
            "label": f"Mencantumkan aspek kunci '{keyword}' sesuai SOP",
            "hint": f"Pastikan analisis atau rencana tindakanmu membahas aspek '{keyword}' berdasarkan data simulasi.",
            "leak_guard": False,
        }
    ]

    est_minutes = 20 if body.difficulty == 1 else (30 if body.difficulty == 2 else 45)

    return {
        "slug": slug,
        "title": title,
        "summary": gen["summary"],
        "brief": gen["brief"],
        "boss_persona": {
            "name": gen["boss_name"],
            "role": gen["boss_role"],
            "style": gen["boss_style"],
        },
        "simulation_data": {
            "headers": headers,
            "rows": normalized_rows,
            "notes": gen["notes"][:4],
        },
        "answer_fields": answer_fields,
        "rubric": rubric,
        "answer_key": answer_key,
        "skill_ids": [s.id for s in matched_skills],
        "skills": [{"id": s.id, "code": s.code, "label": s.label_id or s.label} for s in matched_skills],
        "kind": body.kind,
        "difficulty": body.difficulty,
        "est_minutes": est_minutes,
        "duration_label": f"{est_minutes} menit",
        "pass_score": 70,
        "objective_weight": 0.3,
        "engine": engine,
    }

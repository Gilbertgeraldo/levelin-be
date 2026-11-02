from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.db.base import Base
from app.db.types import JSONType, Vector

EMBEDDING_DIM = get_settings().embedding_dim


class Occupation(Base):
    """Jabatan KBJI (BPS) yang dipetakan ke ISCO-08."""

    __tablename__ = "occupations"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # kode KBJI
    isco_code: Mapped[str] = mapped_column(String(8), index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    title_en: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list] = mapped_column(JSONType, default=list)
    # Rata-rata skor paparan tugas (0-1) dan gradien ILO (0 = tidak terpapar, 1-4)
    avg_exposure: Mapped[float | None] = mapped_column(Float)
    exposure_gradient: Mapped[int | None] = mapped_column(Integer)
    # Kategori asli indeks ILO, mis. "Exposed: Gradient 4" atau "Minimal Exposure"
    gradient_cat: Mapped[str | None] = mapped_column(String(40))
    # Jabatan tujuan untuk arah rencana "naik" dan "pindah"
    naik_code: Mapped[str | None] = mapped_column(String(16))
    pindah_code: Mapped[str | None] = mapped_column(String(16))
    # True = hanya muncul sebagai jabatan tujuan, bukan pilihan onboarding
    is_target: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(80))


class OccupationTask(Base):
    """Template tugas per jabatan ISCO beserta skor paparan ILO."""

    __tablename__ = "occupation_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ID tugas yang dipakai aplikasi, mis. "4311-1"
    code: Mapped[str | None] = mapped_column(String(16), unique=True)
    isco_code: Mapped[str] = mapped_column(String(8), index=True)
    description: Mapped[str] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)
    ilo_score: Mapped[float] = mapped_column(Float)
    # Jenis pekerjaan (data, dok, hr, layan, koord, fisik) untuk mencocokkan misi
    kind: Mapped[str | None] = mapped_column(String(16))
    # False untuk ringkasan tugas jabatan tujuan: hanya ditampilkan, tidak dipakai mencocokkan cerita
    is_candidate: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(80), default="ILO")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(80))


class TaskKind(Base):
    """Jenis pekerjaan: menghubungkan tugas ke skill dan misi latihan yang relevan."""

    __tablename__ = "task_kinds"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    skill_codes: Mapped[list] = mapped_column(JSONType, default=list)
    mission_slug: Mapped[str | None] = mapped_column(String(120))


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Kode pendek yang dipakai aturan, mis. "excel" atau "ai"
    code: Mapped[str | None] = mapped_column(String(40), unique=True)
    esco_uri: Mapped[str | None] = mapped_column(String(255), unique=True)
    label: Mapped[str] = mapped_column(String(255), index=True)
    label_en: Mapped[str | None] = mapped_column(String(255))
    # ai | digital | teknis | transversal
    category: Mapped[str] = mapped_column(String(40), default="teknis")
    description: Mapped[str | None] = mapped_column(Text)


class OccupationSkill(Base):
    __tablename__ = "occupation_skills"

    isco_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    skill_id: Mapped[int] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True
    )
    # essential | optional | base (skill dasar yang dianggap sudah dimiliki pemegang jabatan)
    relation: Mapped[str] = mapped_column(String(16), default="essential")

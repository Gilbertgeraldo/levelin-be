from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.db.base import Base
from app.db.types import JSONType, Vector

EMBEDDING_DIM = get_settings().embedding_dim


class Occupation(Base):
    """Jabatan KBJI 2014 yang dipetakan ke ISCO-08."""

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
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(80))


class OccupationTask(Base):
    """Template tugas per jabatan ISCO beserta skor paparan ILO."""

    __tablename__ = "occupation_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    isco_code: Mapped[str] = mapped_column(String(8), index=True)
    description: Mapped[str] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)
    ilo_score: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(80), default="ILO")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(80))


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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
    relation: Mapped[str] = mapped_column(String(16), default="essential")  # essential | optional
